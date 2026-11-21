"""契约增强（params / expect_json / no_auth）+ 登录态注入的离线测试。

为什么这些功能是「必须的」而不是「锦上添花」：
我去测第二个项目（Java CMS）时才发现，真实的项目几乎都有两件事：
  1. **要分页参数**（`pageNum`/`pageSize`）
  2. **要登录**（`Authorization: Bearer <token>`）
没有这两样，工具就只能测我自己写的那个"不需要登录的 demo"，
"能测别人的项目"这句话立刻缩水。

于是加了三个契约字段（`params` / `expect_json` / `no_auth`）+ 一个 `auth` 块。
设计上最要紧的一条：**登录对模型透明** —— 生成用例的模型只写 `api("me")`，
它不知道 token 从哪来，也没机会把 token 硬编码进用例。
"""
from __future__ import annotations

import json

import pytest

from selfheal import http, targets
from selfheal.targets import TargetConfigError


# ---------------------------------------------------------------------------
# 假 HTTP：记录每一次请求，按剧本返回
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """够用的假 session：把每次调用的参数都记下来。"""

    def __init__(self, router):
        self.router = router
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json,
             "headers": headers or {}, "timeout": timeout}
        )
        return self.router(method, url, json, self.calls)


@pytest.fixture(autouse=True)
def clean_token_cache():
    """每个用例都从"没登录过"开始，否则前一个用例的 token 会串味。"""
    http.reset_token_cache()
    yield
    http.reset_token_cache()


def _target(**overrides) -> targets.Target:
    """构造一个最小的、带登录态的契约。"""
    login = targets.Endpoint(
        id="login", method="POST", path="/api/auth/login",
        expect_status=200, expect_fields=("code", "data"),
        body={"username": "admin", "password": "123456"},
        notes="登录",
    )
    me = targets.Endpoint(
        id="me", method="GET", path="/api/auth/info",
        expect_status=200, expect_fields=("code", "data"),
        expect_json={"code": 200}, notes="当前用户",
    )
    anon = targets.Endpoint(
        id="anon", method="GET", path="/api/category/tree",
        expect_status=200, expect_json={"code": 401}, no_auth=True,
        notes="未登录应被拒绝",
    )
    kw = dict(
        name="fake-cms", base_url="http://127.0.0.1:9",
        endpoints=(login, me, anon), health=login,
        auth=targets.AuthConfig(endpoint="login", token_path="data.token"),
    )
    kw.update(overrides)
    return targets.Target(**kw)


# ---------------------------------------------------------------------------
# 契约解析
# ---------------------------------------------------------------------------
def test_java_cms_config_parses_auth_and_new_fields():
    """真实的 configs/java-cms.json 必须能被解析（防止手滑写坏 json）。"""
    target = targets.load_target("java-cms")

    assert target.base_url.endswith("/cms")
    assert target.auth is not None
    assert target.auth.endpoint == "login"
    assert target.auth.token_path == "data.token"

    anon = target.endpoint("category-tree-anon")
    assert anon is not None and anon.no_auth is True
    assert anon.expect_json == {"code": 401}

    page = target.endpoint("article-page")
    assert page.params == {"pageNum": 1, "pageSize": 10}

    # 每个被测项目要有**自己的**需求文档，否则检索到的是别人的需求
    assert target.requirements_dir is not None
    assert target.requirements_dir.name == "java-cms"
    assert target.requirements_dir.is_dir()


def test_missing_requirements_dir_fails_loudly(ws_tmp):
    """指向不存在的需求目录 → 报错，而不是悄悄检索到 0 条。"""
    bad = ws_tmp / "bad2.json"
    bad.write_text(json.dumps({
        "name": "x", "base_url": "http://127.0.0.1:9",
        "requirements_dir": "selfheal/requirements_docs/nope",
        "endpoints": [{"id": "a", "method": "GET", "path": "/a"}],
    }, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TargetConfigError) as exc:
        targets.load_target(bad)
    assert "requirements_dir" in str(exc.value)


def test_auth_endpoint_must_exist(monkeypatch, ws_tmp):
    """auth 指向不存在的接口 → 早失败，并说清有哪些接口可用。"""
    bad = ws_tmp / "bad.json"
    bad.write_text(json.dumps({
        "name": "x", "base_url": "http://127.0.0.1:9",
        "auth": {"endpoint": "no-such-login"},
        "endpoints": [{"id": "a", "method": "GET", "path": "/a"}],
    }, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TargetConfigError) as exc:
        targets.load_target(bad)
    assert "no-such-login" in str(exc.value)


# ---------------------------------------------------------------------------
# 登录态注入
# ---------------------------------------------------------------------------
def _install_fake(monkeypatch, router):
    session = FakeSession(router)
    monkeypatch.setattr(http, "make_session", lambda url, **kw: session)
    return session


def test_token_is_injected_into_headers(monkeypatch):
    def router(method, url, body, calls):
        if url.endswith("/api/auth/login"):
            return FakeResponse(200, {"code": 200, "data": {"token": "T0KEN"}})
        return FakeResponse(200, {"code": 200, "data": {}})

    session = _install_fake(monkeypatch, router)
    target = _target()

    http.request_json(target, target.endpoint("me"))

    # 第一次是登录，第二次才是业务请求
    assert [c["url"].rsplit("/", 1)[-1] for c in session.calls] == ["login", "info"]
    assert session.calls[0]["headers"] == {}                       # 登录请求本身不带 token
    assert session.calls[1]["headers"] == {"Authorization": "Bearer T0KEN"}


def test_token_is_cached_between_requests(monkeypatch):
    def router(method, url, body, calls):
        if url.endswith("/login"):
            return FakeResponse(200, {"data": {"token": "T0KEN"}})
        return FakeResponse(200, {"code": 200})

    session = _install_fake(monkeypatch, router)
    target = _target()

    http.request_json(target, target.endpoint("me"))
    http.request_json(target, target.endpoint("me"))

    logins = [c for c in session.calls if c["url"].endswith("/login")]
    assert len(logins) == 1          # 只登录一次（缓存生效）


def test_no_auth_endpoint_sends_no_token(monkeypatch):
    def router(method, url, body, calls):
        if url.endswith("/login"):
            pytest.fail("no_auth 接口不该触发登录")
        return FakeResponse(200, {"code": 401, "msg": "未登录或登录已过期"})

    session = _install_fake(monkeypatch, router)
    target = _target()

    result = http.request_json(target, target.endpoint("anon"))

    assert session.calls[0]["headers"] == {}
    assert result.json_body["code"] == 401
    assert result.json_mismatches({"code": 401}) == []


def test_login_failure_does_not_crash(monkeypatch):
    """登录接口本身出错 → 不带 token 继续发请求（让用例自己失败并说清原因）。"""
    def router(method, url, body, calls):
        if url.endswith("/login"):
            return FakeResponse(500, {"code": 500, "msg": "数据库挂了"})
        return FakeResponse(200, {"code": 401})

    session = _install_fake(monkeypatch, router)
    target = _target()

    result = http.request_json(target, target.endpoint("me"))

    assert session.calls[-1]["headers"] == {}      # 没 token 就不硬塞
    assert result.status == 200


def test_401_clears_cached_token(monkeypatch):
    """token 过期（业务码 401）→ 丢缓存，下次自动重新登录。"""
    state = {"n": 0}

    def router(method, url, body, calls):
        if url.endswith("/login"):
            state["n"] += 1
            return FakeResponse(200, {"data": {"token": f"T{state['n']}"}})
        if state["n"] == 1 and len([c for c in calls if c["url"].endswith("/info")]) == 1:
            return FakeResponse(200, {"code": 401})     # 第一次业务请求：token 过期
        return FakeResponse(200, {"code": 200})

    session = _install_fake(monkeypatch, router)
    target = _target()

    http.request_json(target, target.endpoint("me"))     # 拿到 T1 → 401 → 清缓存
    http.request_json(target, target.endpoint("me"))     # 重新登录拿 T2

    logins = [c for c in session.calls if c["url"].endswith("/login")]
    assert len(logins) == 2
    assert session.calls[-1]["headers"] == {"Authorization": "Bearer T2"}


# ---------------------------------------------------------------------------
# params / expect_json
# ---------------------------------------------------------------------------
def test_params_are_passed_through(monkeypatch):
    def router(method, url, body, calls):
        return FakeResponse(200, {"code": 200})

    session = _install_fake(monkeypatch, router)
    target = targets.Target(
        name="p", base_url="http://127.0.0.1:9",
        endpoints=(targets.Endpoint(id="page", method="GET", path="/api/article/page",
                                    params={"pageNum": 1, "pageSize": 10}),),
        health=targets.Endpoint(id="page", method="GET", path="/api/article/page"),
    )

    http.request_json(target, target.endpoint("page"))

    assert session.calls[0]["params"] == {"pageNum": 1, "pageSize": 10}


def test_json_mismatches_reports_actual_value():
    r = http.HttpResult(status=200, json_body={"code": 401, "msg": "未登录"}, text="", latency_ms=1.0)

    assert r.json_mismatches({"code": 401}) == []
    bad = r.json_mismatches({"code": 200, "msg": "操作成功"})
    assert len(bad) == 2
    assert "期望 200" in bad[0] and "实际 401" in bad[0]
    assert r.json_mismatches(None) == []
