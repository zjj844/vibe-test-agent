"""generator.py 的离线测试。

关键：**不调真模型**。用一个 stub 替换 llm_client.chat_for，
这样"生成失败 → 重试 → 仍失败"这条路径也能确定性复现。

测的重点是**静态检查那三道关**——那是规则，不是模型的判断，必须可复现。
"""
from __future__ import annotations

import json

import pytest

from selfheal import generator
from selfheal.targets import Endpoint, Target


@pytest.fixture()
def target() -> Target:
    """一个最小契约：两个接口，足够验证"调用了不存在的接口"这类问题。"""
    eps = (
        Endpoint(id="health", method="GET", path="/api/health", expect_status=200,
                 expect_fields=("status",), notes="健康检查"),
        Endpoint(id="ask", method="POST", path="/api/ask", expect_status=200,
                 expect_fields=("answer",), body={"question": "x"}, notes="问答"),
    )
    return Target(name="fake", base_url="http://127.0.0.1:9", endpoints=eps, health=eps[0], timeout=5)


# ---------------------------------------------------------------------------
# 提示词：契约与需求必须真的进到提示词里
# ---------------------------------------------------------------------------
def test_prompt_contains_all_endpoints(target):
    msgs = generator.build_messages(target, [], n_cases=3)
    system = msgs[0]["content"]
    assert "id=health" in system and "id=ask" in system
    assert "/api/health" in system
    assert "健康检查" in system          # 业务含义也要带上
    assert "禁止" in system              # 约束必须写明


def test_prompt_includes_requirement_evidence(target):
    evidence = [{"chunk_id": "REQ-001#s02#c00", "text": "FR-2 提问后必须展示出处"}]
    system = generator.build_messages(target, evidence, n_cases=2)[0]["content"]
    assert "REQ-001#s02#c00" in system
    assert "必须展示出处" in system


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_extract_code_handles_fence_and_noise():
    raw = '这是代码：\n```python\ndef test_a():\n    assert 1\n```\n希望有帮助'
    assert generator.extract_code(raw).startswith("def test_a()")


def test_used_endpoint_ids_order_and_dedup():
    code = 'api("ask")\napi("health")\napi("ask")'
    assert generator.used_endpoint_ids(code) == ["ask", "health"]


# ---------------------------------------------------------------------------
# 三道静态检查
# ---------------------------------------------------------------------------
def test_accepts_valid_code(target):
    code = '''
def test_health_ok():
    """FR-1：健康检查返回 ok。"""
    endpoint, r = api("health")
    assert r.status == endpoint.expect_status
    assert r.json_body["status"] == "ok"
'''
    ok, violations, count = generator.validate_code(code, target)
    assert ok, violations
    assert count == 1


def test_rejects_unknown_endpoint(target):
    code = '''
def test_x():
    endpoint, r = api("delete_user")
    assert r.status == 200
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("不存在的接口" in v for v in violations)


def test_rejects_hardcoded_url(target):
    code = '''
def test_x():
    endpoint, r = api("health")
    assert "http://127.0.0.1:8000" in endpoint.path or True
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("硬编码 URL" in v for v in violations)


def test_rejects_try_except(target):
    code = '''
def test_x():
    try:
        endpoint, r = api("health")
    except Exception:
        pass
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("try/except" in v for v in violations)


def test_rejects_skip(target):
    code = '''
def test_x():
    pytest.mark.skip("暂时不跑")
    endpoint, r = api("health")
'''
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("skip" in v for v in violations)


def test_rejects_no_test_function(target):
    ok, violations, count = generator.validate_code("x = 1\n", target)
    assert not ok and count == 0
    assert any("test_" in v for v in violations)


def test_rejects_syntax_error(target):
    ok, violations, _ = generator.validate_code("def test_x(:\n", target)
    assert not ok
    assert any("语法错误" in v for v in violations)


def test_rejects_code_without_api_call(target):
    code = 'def test_x():\n    assert 1 == 1\n'
    ok, violations, _ = generator.validate_code(code, target)
    assert not ok
    assert any("没有真正发请求" in v for v in violations)


# ---------------------------------------------------------------------------
# 生成流程：第一次违规 → 重试一次 → 成功 / 仍失败
# ---------------------------------------------------------------------------
_GOOD = '''def test_health_ok():
    """FR-1：健康检查。"""
    endpoint, r = api("health")
    assert r.status == endpoint.expect_status
    assert r.json_body["status"] == "ok"
'''

_BAD = '''def test_health_ok():
    endpoint, r = api("not_exist")
    assert r.status == 200
'''


def _stub(responses: list[str]):
    calls = {"n": 0}

    def _fake(role, messages, max_tokens=8000):
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]

    return _fake, calls


def test_generate_succeeds_first_try(target, monkeypatch):
    fake, calls = _stub([f"```python\n{_GOOD}```"])
    monkeypatch.setattr(generator.llm_client, "chat_for", fake)

    result = generator.generate(target, [], n_cases=1)
    assert result.ok, result.violations
    assert result.case_count == 1
    assert result.repaired is False
    assert calls["n"] == 1


def test_generate_repairs_once_after_violation(target, monkeypatch):
    """第一次用了不存在的接口 → 回喂违规原因 → 第二次改对。"""
    fake, calls = _stub([f"```python\n{_BAD}```", f"```python\n{_GOOD}```"])
    monkeypatch.setattr(generator.llm_client, "chat_for", fake)

    result = generator.generate(target, [], n_cases=1)
    assert result.ok, result.violations
    assert result.repaired is True
    assert calls["n"] == 2                 # 只重试一次


def test_generate_gives_up_after_max_repair(target, monkeypatch):
    """两次都错 → 直接失败，不无限重试（有界自愈）。"""
    fake, calls = _stub([f"```python\n{_BAD}```"])
    monkeypatch.setattr(generator.llm_client, "chat_for", fake)

    result = generator.generate(target, [], n_cases=1, max_repair=1)
    assert result.ok is False
    assert result.violations
    assert calls["n"] == 2                 # 1 次初试 + 1 次重试，就停


def test_save_writes_runnable_file(target, ws_tmp):
    """落盘文件必须自带模板头（import + api 助手），否则跑不起来。"""
    code = _GOOD
    result = generator.GenerationResult(ok=True, code=code, case_count=1, endpoint_ids=["health"])
    path = generator.save(result, target, out_dir=ws_tmp, tag="unit-test")

    text = path.read_text(encoding="utf-8")
    assert "from selfheal.http import request_json" in text
    assert "def api(endpoint_id)" in text
    assert "def test_health_ok()" in text
    assert "覆盖接口：health" in text
    # 生成的文件自己也能通过语法检查
    compile(text, str(path), "exec")
