"""鉴权推断 + 契约体检的测试（对应真实踩过的坑）。

真实事故复盘：
    用户用 AI 起草了一份 18 接口的契约 → 没有 auth、login 没 body →
    跑出来 0% 通过率 + 一条被误判成"系统缺陷"的假缺陷 + 最终 gave_up。
所以这里钉死三件事：
  1. 源码里有鉴权痕迹 + 契约里有登录接口 → 草案必须自动补 auth
  2. 保存前的体检要能把"需要登录但没 auth""login 缺 body"指出来
  3. check_contract 的失败消息必须带 HTTP 状态码和响应体 code（否则归因会错）
"""
from __future__ import annotations

import json

import pytest

from selfheal import bootstrap, generator, targets
from selfheal.http import HttpResult

JAVA_SOURCE = """
@RestController
@RequestMapping("/api/article")
public class ArticleController {
    // 需要登录，请求头 Authorization: Bearer <token>
    @GetMapping("/page")
    public Result<PageResult<ArticleVO>> page(ArticleQueryDTO query) { ... }
}
"""

LOGIN_EP = {
    "id": "auth-login", "method": "POST", "path": "/api/auth/login",
    "expect_status": 200, "expect_fields": ["code", "data"], "notes": "用户登录，需要登录",
}
ARTICLE_EP = {
    "id": "article-page", "method": "GET", "path": "/api/article/page",
    "expect_status": 200, "expect_fields": ["code", "data"], "notes": "文章分页，需要登录",
}


def _contract(**over) -> dict:
    data = {
        "name": "cms", "base_url": "http://127.0.0.1:8080/cms",
        "health": {"id": "article-page"},
        "endpoints": [dict(LOGIN_EP), dict(ARTICLE_EP)],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# 鉴权推断
# ---------------------------------------------------------------------------
def test_detect_auth_finds_login_endpoint():
    auth = bootstrap.detect_auth(JAVA_SOURCE, _contract())

    assert auth is not None
    assert auth["endpoint"] == "auth-login"
    assert auth["token_path"] == "data.token"
    assert auth["prefix"] == "Bearer "


def test_detect_auth_none_when_source_has_no_hint():
    assert bootstrap.detect_auth("public class Foo {}", _contract()) is None


def test_detect_auth_none_when_no_login_endpoint():
    contract = _contract(endpoints=[dict(ARTICLE_EP)])
    assert bootstrap.detect_auth(JAVA_SOURCE, contract) is None


def test_draft_injects_auth_automatically(monkeypatch):
    """核心回归：起草时必须把"需要登录"翻译成 auth 配置。"""
    reply = json.dumps(_contract(), ensure_ascii=False)
    monkeypatch.setattr(bootstrap.llm_client, "chat_for", lambda *a, **k: reply)

    result = bootstrap.draft("cms", "http://127.0.0.1:8080/cms",
                             [{"path": "ArticleController.java", "content": JAVA_SOURCE}])

    assert result.ok is True
    assert result.contract.get("auth"), "起草结果必须自动带 auth"
    assert result.contract["auth"]["endpoint"] == "auth-login"
    # 推断是"建议"，要留痕让人核对
    assert "自动推断" in result.contract.get("_auth_note", "")


def test_draft_keeps_model_provided_auth(monkeypatch):
    """模型自己给了 auth 就不要覆盖。"""
    contract = _contract(auth={"endpoint": "auth-login", "token_path": "data.t"})
    monkeypatch.setattr(
        bootstrap.llm_client, "chat_for", lambda *a, **k: json.dumps(contract, ensure_ascii=False)
    )

    result = bootstrap.draft("cms", "http://127.0.0.1:8080/cms",
                             [{"path": "A.java", "content": JAVA_SOURCE}])

    assert result.contract["auth"]["token_path"] == "data.t"


# ---------------------------------------------------------------------------
# 契约体检
# ---------------------------------------------------------------------------
def test_warning_when_need_login_but_no_auth():
    warns = bootstrap.contract_warnings(_contract())
    assert any("需要登录" in w and "没有 auth" in w for w in warns)


def test_warning_when_login_has_no_body():
    contract = _contract(auth={"endpoint": "auth-login", "token_path": "data.token"})
    warns = bootstrap.contract_warnings(contract)
    assert any("没有请求体" in w for w in warns)


def test_warning_when_auth_endpoint_missing():
    contract = _contract(auth={"endpoint": "nope", "token_path": "data.token"})
    warns = bootstrap.contract_warnings(contract)
    assert any("不在接口列表里" in w for w in warns)


def test_warning_when_health_is_post():
    contract = _contract(health={"id": "auth-login"},
                         auth={"endpoint": "auth-login", "token_path": "data.token"})
    warns = bootstrap.contract_warnings(contract)
    assert any("探活最好用" in w for w in warns)


def test_no_warning_when_contract_is_sane():
    contract = _contract(
        health={"id": "article-page"},
        auth={"endpoint": "auth-login", "token_path": "data.token"},
    )
    contract["endpoints"][0]["body"] = {"username": "admin", "password": "123456"}
    assert bootstrap.contract_warnings(contract) == []


# ---------------------------------------------------------------------------
# 模板断言消息：必须带 HTTP 状态码 + 响应体 code
# ---------------------------------------------------------------------------
def test_template_assert_message_includes_http_and_body_code():
    """否则 `assert 500 == 200` 会被分流误判成 HTTP 5xx 系统缺陷。"""
    assert "响应体 code=" in generator.TEMPLATE_HEADER
    assert "HTTP {{r.status}}" in generator.TEMPLATE_HEADER or "HTTP " in generator.TEMPLATE_HEADER


def test_business_500_is_triaged_as_defect_but_with_actionable_reason():
    from selfheal import triage
    from selfheal.triage import Action, FailureKind

    case = triage.classify_case(
        _case("状态码 200 != 契约 200（HTTP 200 · 响应体 code=500 · msg='系统繁忙')")
    )
    assert case.kind is FailureKind.SYSTEM_BUG
    assert case.action is Action.REPORT_DEFECT
    assert "请求参数" in case.reason        # 提示先核对参数/契约，而不是直接甩给服务端


def test_http_500_still_reports_as_server_defect():
    from selfheal import triage
    from selfheal.triage import FailureKind

    case = triage.classify_case(_case("状态码 500 != 契约 200（HTTP 500 · 响应体 code=None)"))
    assert case.kind is FailureKind.SYSTEM_BUG
    assert "接口返回 5xx" in case.reason


def _case(message: str):
    from selfheal.runner import CaseResult

    return CaseResult(
        nodeid="t.py::test_x", name="test_x", status="failed", duration=0.1, message=message
    )
