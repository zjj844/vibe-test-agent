"""补充测试：HttpResult 成员白名单（第四道静态检查）。

为什么单开一个文件：这是**实测踩到**的一类错误——
模型写 `r.json()`（requests 的写法），而我们的 `HttpResult` 字段叫 `json_body`。
语法完全合法，只有跑起来才炸。这类"凭空发明 API"必须用规则拦。
"""
from __future__ import annotations

import pytest

from selfheal import generator
from selfheal.targets import Endpoint, Target


@pytest.fixture()
def target() -> Target:
    ep = Endpoint(id="health", method="GET", path="/api/health", expect_status=200,
                  expect_fields=("status",), notes="健康检查")
    return Target(name="fake", base_url="http://127.0.0.1:9", endpoints=(ep,), health=ep, timeout=5)


def test_rejects_requests_style_json_call(target):
    """真实踩坑：模型写 r.json()，但 HttpResult 里是 json_body。"""
    code = '''
def test_x():
    endpoint, r = api("health")
    assert r.json()["status"] == "ok"
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("r.json()" in v for v in violations)


def test_rejects_unknown_httresult_member(target):
    code = '''
def test_x():
    endpoint, r = api("health")
    assert r.status_code == 200
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("HttpResult 没有这些成员" in v for v in violations)


def test_allows_only_whitelisted_members(target):
    code = '''
def test_x():
    endpoint, r = api("health")
    assert r.status == endpoint.expect_status
    assert r.json_body["status"] == "ok"
    assert r.network_failed is False
    assert not r.missing_fields(endpoint.expect_fields)
    assert r.latency_ms >= 0
'''
    ok, violations, count = generator.validate_code(code, target)
    assert ok, violations
    assert count == 1
