"""推理模型"思考把 token 吃光"的兜底（真实事故回归）。

事故：第 2 轮生成的提示词更长（system 带需求+规范片段，再加一轮"上一版代码+失败摘要"），
推理模型的思考长度随输入复杂度上升 → max_tokens=8000 被 reasoning 吃光 → 正文为空
→ 整个循环以"生成失败：模型正文为空"结束。

这是**可以救**的失败：
  1. 放大 token 预算重试一次（chat_for 内置）
  2. 还不行就精简历史消息再试（generator 内置）
"""
from __future__ import annotations

import pytest

from selfheal import generator, llm_client


def test_empty_content_error_is_its_own_type(monkeypatch):
    """空正文必须是专门异常，上层才能针对性重试。"""
    def boom(*a, **k):
        raise llm_client.EmptyContentError("预算被思考吃光")

    monkeypatch.setattr(llm_client, "chat", boom)
    with pytest.raises(llm_client.EmptyContentError):
        llm_client.chat([{"role": "user", "content": "hi"}])


def test_chat_for_retries_with_bigger_budget(monkeypatch):
    calls: list[int] = []

    def fake(messages, model=None, max_tokens=0, **kw):
        calls.append(max_tokens)
        if len(calls) == 1:
            raise llm_client.EmptyContentError("吃光")
        return "ok"

    monkeypatch.setattr(llm_client, "chat", fake)

    assert llm_client.chat_for("gen", [{"role": "user", "content": "hi"}]) == "ok"
    assert len(calls) == 2
    assert calls[1] > calls[0], "第二次必须放大预算"


def test_chat_for_budget_is_capped(monkeypatch):
    calls: list[int] = []

    def always_empty(messages, model=None, max_tokens=0, **kw):
        calls.append(max_tokens)
        raise llm_client.EmptyContentError("吃光")

    monkeypatch.setattr(llm_client, "chat", always_empty)

    with pytest.raises(llm_client.EmptyContentError):
        llm_client.chat_for("gen", [{"role": "user", "content": "hi"}])
    assert max(calls) <= llm_client.GEN_MAX_TOKENS_RETRY


def test_generator_slims_history_and_recovers(monkeypatch):
    """第一次空正文 → 精简历史 + 再试 → 拿到合法代码。"""
    from selfheal import targets

    ep = targets.Endpoint(id="health", method="GET", path="/api/health")
    target = targets.Target(name="t", base_url="http://127.0.0.1:9",
                            endpoints=(ep,), health=ep)

    long_feedback = "上一轮失败：" + "很长的报错" * 500
    seen_lengths: list[int] = []

    def fake_chat_for(role, messages, max_tokens=None):
        seen_lengths.append(sum(len(m["content"]) for m in messages))
        if len(seen_lengths) == 1:
            raise llm_client.EmptyContentError("吃光")
        return '```python\ndef test_health():\n    e, r = api("health")\n```'

    monkeypatch.setattr(llm_client, "chat_for", fake_chat_for)

    result = generator.generate(target, [], n_cases=1, feedback=long_feedback)

    assert result.ok is True
    assert result.case_count == 1
    assert len(seen_lengths) == 2
    assert seen_lengths[1] < seen_lengths[0], "第二次必须更短（精简历史）"


def test_slim_messages_truncates_but_keeps_roles():
    messages = [
        {"role": "system", "content": "s" * 5000},
        {"role": "assistant", "content": "a" * 5000},
        {"role": "user", "content": "u" * 5000},
    ]
    slim = generator._slim_messages(messages)

    assert [m["role"] for m in slim] == ["system", "assistant", "user"]
    assert all(len(m["content"]) < 3000 for m in slim)
    assert all("截断" in m["content"] for m in slim)


def test_generate_still_raises_when_recovery_fails(monkeypatch):
    from selfheal import targets

    ep = targets.Endpoint(id="health", method="GET", path="/api/health")
    target = targets.Target(name="t", base_url="http://127.0.0.1:9",
                            endpoints=(ep,), health=ep)

    def always_empty(role, messages, max_tokens=None):
        raise llm_client.EmptyContentError("吃光")

    monkeypatch.setattr(llm_client, "chat_for", always_empty)

    with pytest.raises(llm_client.EmptyContentError):
        generator.generate(target, [], n_cases=1)
