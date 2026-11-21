"""M1.5 · HTTP 小工具（http.py）

=====================================================================
本模块职责
=====================================================================
按"契约里的一个接口"发一次真实 HTTP 请求，把结果包成一个对象返回。

=====================================================================
在整个系统里的位置
=====================================================================
    main → loop → {generator, runner, triage} → targets.py
                                        ↘
                                    【http.py】→ 真实 HTTP

它是"真正发出请求"的那一层。**探活（targets.probe）也走同一套逻辑**，
只是探活只要一个结果，这里要带上响应体。

=====================================================================
三条设计纪律（每条都是踩过坑才写的）
=====================================================================
1. **网络错误不抛异常**：连接失败/超时是"预期内"的情况（服务没起、端口错），
   要把它们变成 `HttpResult.error` 里的一句话，而不是让程序崩。
   —— 崩掉的程序没法给你出报告，只会给你一堆红色 traceback。

2. **本机地址不走代理**：见 targets.make_session 的注释。
   本机开着 Clash 时，127.0.0.1 会被塞进代理，导致"连接被拒"变成 502。

3. **只做一件事**：发请求 + 收集响应。
   这里**不做断言**——断言属于测试用例，不属于工具。
   （工具如果替用例做判断，就等于"自己出题自己批改"，那是自证循环。）
=====================================================================
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .targets import Endpoint, Target, make_session

# ---------------------------------------------------------------------------
# 登录态缓存：一个进程内每个 target 只登录一次
# ---------------------------------------------------------------------------
# 为什么缓存：登录接口要查库、算 token，每条用例都登一次既慢又脏。
# 为什么用「模块级 dict」而不是挂在 Target 上：Target 是 frozen（不可变）的，
# 而登录态是**运行期状态**，不是配置。配置和状态混在一起是 bug 的源头。
_TOKEN_CACHE: dict[str, str] = {}


def reset_token_cache() -> None:
    """清空登录态（测试用，或在 token 过期后强制重新登录）。"""
    _TOKEN_CACHE.clear()


def _dig(obj, dotted: str):
    """按 "data.token" 这种点路径从 JSON 里取值，取不到返回 None。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def acquire_token(target: Target, session=None) -> str:
    """用契约里的登录接口换一个 token。失败返回 ""（调用方决定怎么处理）。

    注意这里**故意不抛异常**：登录失败是"可预期的情况"（账号密码改了、
    seed 数据没导入、token 过期），应该体现成"用例失败 + 能看懂的报错"，
    而不是让整个程序崩掉。
    """
    if target.auth is None:
        return ""
    login_ep = target.endpoint(target.auth.endpoint)
    if login_ep is None:
        return ""

    result = request_json(target, login_ep, session=session)
    token = _dig(result.json_body, target.auth.token_path)
    if isinstance(token, str) and token:
        _TOKEN_CACHE[target.name] = token
        return token
    _TOKEN_CACHE.pop(target.name, None)
    return ""


def _headers_for(target: Target, endpoint: Endpoint, session=None) -> dict[str, str]:
    """算出这次请求要带的请求头（登录态注入点）。

    三种情况：
      1. 项目没配 auth          → 不带任何头
      2. 接口标了 no_auth=True  → **故意不带**（用于验证"未登录应被拒绝"）
      3. 其它                   → 带上 Bearer token（没有就先登录）
    """
    if target.auth is None or endpoint.id == target.auth.endpoint or endpoint.no_auth:
        return {}
    token = _TOKEN_CACHE.get(target.name) or acquire_token(target, session=session)
    if not token:
        return {}
    return {target.auth.header: f"{target.auth.prefix}{token}"}


@dataclass(frozen=True)
class HttpResult:
    """一次请求的全部结果。注意它**永远不抛异常**，只是可能带着 error。"""

    status: int | None          # HTTP 状态码；网络层失败时为 None
    json_body: dict | None      # 解析出来的 JSON（不是 JSON 就是 None）
    text: str                   # 原始响应文本（截断到 2000 字符，防止刷屏）
    latency_ms: float
    error: str = ""             # 网络层错误描述；空字符串表示"请求确实发出去了"

    # ---- 给用例/报告用的小工具 ----
    @property
    def network_failed(self) -> bool:
        return self.status is None

    def field_names(self) -> set[str]:
        """响应 JSON 的顶层字段名。契约比对用。"""
        return set(self.json_body.keys()) if isinstance(self.json_body, dict) else set()

    def missing_fields(self, expect_fields: tuple[str, ...]) -> list[str]:
        """契约要求有、但响应里缺的字段。"""
        return [f for f in expect_fields if f not in self.field_names()]

    def json_mismatches(self, expect_json: dict | None) -> list[str]:
        """契约要求"某字段等于某值"时的比对结果。

        为什么有 `expect_fields` 还要这个：
        Java CMS 的 401 是 **HTTP 200 + body 里 code=401**（拦截器直接写 JSON，
        没改状态码）。只查"字段存在"是完全测不出鉴权的 ——
        必须能断言 `body["code"] == 401`，否则"未登录应该被拒绝"这条用例形同虚设。
        """
        if not expect_json:
            return []
        body = self.json_body or {}
        return [
            f"{k}: 期望 {v!r}，实际 {body.get(k)!r}"
            for k, v in expect_json.items()
            if body.get(k) != v
        ]

    def summary(self) -> str:
        if self.network_failed:
            return f"网络失败：{self.error}"
        return f"{self.status} · {self.latency_ms:.0f}ms · 字段={sorted(self.field_names())}"


def request_json(target: Target, endpoint: Endpoint, session=None) -> HttpResult:
    """按契约发一次请求。

    为什么参数是 (target, endpoint) 而不是一个完整 url：
    因为调用方（用例、runner）永远从契约里拿接口，不自己拼 URL。
    **URL 只有一个来源**，这样换被测项目时不用改任何测试代码。
    """
    url = endpoint.url(target.base_url)
    started = time.perf_counter()
    session = session or make_session(url)
    headers = _headers_for(target, endpoint, session=session)

    try:
        resp = session.request(
            endpoint.method,
            url,
            params=endpoint.params,      # 分页参数之类
            json=endpoint.body,
            headers=headers or None,
            timeout=target.timeout,
        )
    except requests.exceptions.ConnectionError:
        return HttpResult(
            status=None, json_body=None, text="",
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"连接被拒：{endpoint.method} {url} —— 被测服务没启动？",
        )
    except requests.exceptions.Timeout:
        return HttpResult(
            status=None, json_body=None, text="",
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"请求超时（>{target.timeout:.0f}s）：{endpoint.method} {url}",
        )
    except requests.exceptions.RequestException as exc:  # 兜底：其它网络类异常
        return HttpResult(
            status=None, json_body=None, text="",
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"请求异常：{type(exc).__name__}: {exc}",
        )

    latency = (time.perf_counter() - started) * 1000
    body: dict | None
    try:
        parsed = resp.json()
        body = parsed if isinstance(parsed, dict) else None
    except ValueError:      # 不是 JSON（比如返回 HTML 错误页）
        body = None

    # token 失效（401）→ 丢掉缓存，下次调用会重新登录。
    # 为什么在这里做：这是"业务码/状态码"和"登录态"唯一该耦合的地方。
    if resp.status_code == 401 or (isinstance(body, dict) and body.get("code") == 401):
        _TOKEN_CACHE.pop(target.name, None)

    return HttpResult(
        status=resp.status_code,
        json_body=body,
        text=resp.text[:2000],
        latency_ms=latency,
        error="",
    )
