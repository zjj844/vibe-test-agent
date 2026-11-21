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

    def summary(self) -> str:
        if self.network_failed:
            return f"网络失败：{self.error}"
        return f"{self.status} · {self.latency_ms:.0f}ms · 字段={sorted(self.field_names())}"


def request_json(target: Target, endpoint: Endpoint) -> HttpResult:
    """按契约发一次请求。

    为什么参数是 (target, endpoint) 而不是一个完整 url：
    因为调用方（用例、runner）永远从契约里拿接口，不自己拼 URL。
    **URL 只有一个来源**，这样换被测项目时不用改任何测试代码。
    """
    url = endpoint.url(target.base_url)
    started = time.perf_counter()
    session = make_session(url)

    try:
        resp = session.request(
            endpoint.method,
            url,
            json=endpoint.body,
            timeout=target.timeout,
        )
    except requests.exceptions.ConnectionError as exc:
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

    return HttpResult(
        status=resp.status_code,
        json_body=body,
        text=resp.text[:2000],
        latency_ms=latency,
        error="",
    )
