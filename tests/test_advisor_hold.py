"""advisor.py（AI 辅助归因）+ HOLD 动作 + 嵌套期望值的离线测试。

这一组测试都在守护同一个原则：

    规则能判的，绝不让模型出场；模型出场也只给建议，不给动作权限。

所以测试里最要紧的两条是：
  · `should_consult` 只对 MANUAL / HOLD 为真（FIX / REGENERATE / STOP 一律不咨询）
  · 就算模型给了答案，`Advice` 里也没有任何可执行的字段（它只是文字）
"""
from __future__ import annotations

import json

import pytest

from selfheal import advisor, http, triage
from selfheal.runner import CaseResult
from selfheal.triage import Action, FailureKind, TriageReport, Verdict


def _case(message: str = "", detail: str = "") -> CaseResult:
    return CaseResult(
        nodeid="test_x.py::test_y", name="test_y", status="failed",
        duration=0.1, message=message, detail=detail,
    )


def _verdict(kind: FailureKind, action: Action) -> Verdict:
    return Verdict(nodeid="test_x.py::test_y", kind=kind, action=action, reason="…")


# ---------------------------------------------------------------------------
# 1. 什么时候才允许问模型
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,action,expected",
    [
        (FailureKind.CONTRACT_MISMATCH, Action.HOLD, True),
        (FailureKind.UNKNOWN, Action.MANUAL, True),
        (FailureKind.SYSTEM_BUG, Action.REPORT_DEFECT, False),
        (FailureKind.SCRIPT, Action.FIX, False),
        (FailureKind.GENERATED_CODE, Action.REGENERATE, False),
        (FailureKind.SERVER_UNREACHABLE, Action.STOP, False),
    ],
)
def test_should_consult_only_for_undecidable(kind, action, expected):
    assert advisor.should_consult(_verdict(kind, action)) is expected


# ---------------------------------------------------------------------------
# 2. advise()：正常 / 坏 JSON / 非法取值 / 网络失败
# ---------------------------------------------------------------------------
def _stub_llm(monkeypatch, reply: str):
    monkeypatch.setattr(advisor.llm_client, "chat_for", lambda *a, **k: reply)


def test_advise_returns_structured_advice(monkeypatch):
    _stub_llm(monkeypatch, json.dumps({
        "responsibility": "服务端缺陷",
        "reason": "契约要求 total=798，服务返回 399",
        "suggestion": "按缺陷提单，不要改测试",
    }, ensure_ascii=False))

    advice = advisor.advise(_case("响应字段值与契约不符"), _verdict(FailureKind.CONTRACT_MISMATCH, Action.HOLD))

    assert advice.ok is True
    assert advice.responsibility == "服务端缺陷"
    assert "798" in advice.reason
    assert "改测试" in advice.suggestion


def test_advise_handles_bad_json(monkeypatch):
    _stub_llm(monkeypatch, "我觉得是服务的问题")          # 没有 JSON
    advice = advisor.advise(_case(), _verdict(FailureKind.UNKNOWN, Action.MANUAL))

    assert advice.ok is False
    assert "解析失败" in advice.error


def test_advise_normalizes_unknown_responsibility(monkeypatch):
    """模型给了不在选项里的词 → 归到"无法判断"，别让报告出现奇怪的措辞。"""
    _stub_llm(monkeypatch, json.dumps({"responsibility": "大概是玄学", "reason": "x"}))
    advice = advisor.advise(_case(), _verdict(FailureKind.UNKNOWN, Action.MANUAL))

    assert advice.ok is True
    assert advice.responsibility == "无法判断"


def test_advise_swallows_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("连不上模型")

    monkeypatch.setattr(advisor.llm_client, "chat_for", boom)
    advice = advisor.advise(_case(), _verdict(FailureKind.UNKNOWN, Action.MANUAL))

    assert advice.ok is False
    assert "连不上模型" in advice.error


# ---------------------------------------------------------------------------
# 3. 新的分流规则：契约与实现不一致 → HOLD
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "message",
    [
        "AssertionError: 响应字段值与契约不符：['data.total: 期望 798，实际 399']",
        "AssertionError: 响应缺字段 ['data']",
        "AssertionError: 契约里没有接口 order-total",
    ],
)
def test_contract_mismatch_message_maps_to_hold(message):
    verdict = triage.classify_case(_case(message))

    assert verdict.kind is FailureKind.CONTRACT_MISMATCH
    assert verdict.action is Action.HOLD
    assert "只有人能分清" in verdict.reason


def test_500_still_maps_to_defect_not_hold():
    """确保新规则没有把 5xx 抢走：5xx 必须是 DEFECT。"""
    verdict = triage.classify_case(_case("assert 500 == 200"))
    assert verdict.kind is FailureKind.SYSTEM_BUG
    assert verdict.action is Action.REPORT_DEFECT


def test_generated_code_error_still_fixable():
    verdict = triage.classify_case(_case("AttributeError: 'HttpResult' object has no attribute 'json'"))
    assert verdict.kind is FailureKind.GENERATED_CODE
    assert verdict.action is Action.REGENERATE


def test_report_held_property_and_conclusion():
    report = TriageReport(
        verdicts=(
            Verdict("a", FailureKind.CONTRACT_MISMATCH, Action.HOLD, "值不符"),
            Verdict("b", FailureKind.SCRIPT, Action.FIX, "类型错"),
        )
    )
    assert len(report.held) == 1
    assert report.held[0].nodeid == "a"
    assert "契约与实现不一致" in report.conclusion()
    assert "需人工确认" in report.conclusion()


# ---------------------------------------------------------------------------
# 4. 嵌套期望值（"data.total": 798）
# ---------------------------------------------------------------------------
def test_json_mismatches_supports_dotted_path():
    result = http.HttpResult(
        status=200,
        json_body={"code": 200, "data": {"order_id": 41, "total": 399}},
        text="", latency_ms=1.0,
    )

    assert result.json_mismatches({"data.total": 399}) == []
    bad = result.json_mismatches({"data.total": 798})
    assert len(bad) == 1
    assert "data.total" in bad[0] and "798" in bad[0] and "399" in bad[0]

    # 路径不存在 → 期望值对不上（不是崩掉）
    missing = result.json_mismatches({"data.nope": 1})
    assert len(missing) == 1
    assert "None" in missing[0]
