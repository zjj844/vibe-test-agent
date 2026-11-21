"""需求知识库（kb）+ 用例自评（critic）的离线测试。

这两个模块都不需要浏览器、不需要被测服务：
- kb：读本地需求文档 → 分块 → 检索（纯本地计算）
- critic：打分逻辑用 stub 替换模型调用，验证"取中位"和"JSON 解析"这两件确定性的事
"""
from __future__ import annotations

import json

import pytest

from selfheal import critic
from selfheal.kb import RequirementKB


# ---------------------------------------------------------------------------
# 需求知识库
# ---------------------------------------------------------------------------
def test_kb_hierarchical_split():
    """需求必须按 H2/H3 切成模块，而不是按固定长度硬切。"""
    kb = RequirementKB()
    assert kb.doc_count == 3
    assert kb.chunk_count > kb.doc_count
    titles = [s.title for s in kb.sections]
    assert any(t.startswith("FR-2") for t in titles), "没切出 FR-2 小节"
    assert any("NFR" in t for t in titles)


def test_kb_tree_is_readable():
    """模块结构树要能给人核对（这是"AI 切得对不对"的唯一人工把关点）。"""
    tree = RequirementKB().tree()
    assert "REQ-001" in tree and "FR-1" in tree
    assert tree.count("📄") == 3


def test_kb_search_returns_scored_chunks():
    hits = RequirementKB().search("提问与出处", top_k=3)
    assert len(hits) == 3
    assert hits[0]["score"] >= hits[-1]["score"]
    assert all(h["chunk_id"].startswith("REQ-") for h in hits)


# ---------------------------------------------------------------------------
# 用例自评
# ---------------------------------------------------------------------------
def test_extract_json_handles_noise():
    """模型回复里常带解释文字和代码围栏，解析要能容忍。"""
    text = '这是我的评分：\n```json\n{"可执行性": 4, "断言明确性": 3}\n```\n以上。'
    data = critic._extract_json(text)
    assert data["可执行性"] == 4


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        critic._extract_json("我觉得挺好的，没有分数。")


def _stub_chat_factory(payloads: list[dict]):
    """造一个假的 chat_for：按顺序返回预设的评分 JSON。"""
    calls = {"n": 0}

    def _fake(role, messages, max_tokens=8000):
        idx = min(calls["n"], len(payloads) - 1)
        calls["n"] += 1
        return json.dumps(payloads[idx], ensure_ascii=False)

    return _fake


def test_score_takes_median_of_two_samples(monkeypatch):
    """两次采样分数不同时，必须逐维度取中位数（而不是取第一次）。"""
    payloads = [
        {k: 1 for k in critic.RUBRIC} | {"评语": "第一次"},   # 全 1 分
        {k: 5 for k in critic.RUBRIC} | {"评语": "第二次"},   # 全 5 分
    ]
    monkeypatch.setattr(critic.llm_client, "chat_for", _stub_chat_factory(payloads))

    result = critic.score("def test_x(): pass", samples=2)
    assert result.samples == 2
    for dim in critic.RUBRIC:
        assert result.scores[dim] == 3.0        # median(1, 5) = 3
    assert result.total == 3.0
    assert result.comment == "第一次"            # 评语取第一次出现的


def test_score_survives_one_bad_sample(monkeypatch):
    """一次采样返回废话时，另一次仍然有效 —— 这就是多次采样的容错价值。"""
    calls = {"n": 0}

    def _fake(role, messages, max_tokens=8000):
        calls["n"] += 1
        if calls["n"] == 1:
            return "抱歉，我无法评分。"           # 没有 JSON
        return json.dumps({k: 4 for k in critic.RUBRIC} | {"评语": "OK"}, ensure_ascii=False)

    monkeypatch.setattr(critic.llm_client, "chat_for", _fake)
    result = critic.score("def test_y(): pass", samples=2)
    assert result.samples == 1                   # 只有一次成功
    assert result.total == 4.0


def test_score_raises_when_all_samples_fail(monkeypatch):
    monkeypatch.setattr(critic.llm_client, "chat_for", lambda *a, **k: "没有分数")
    with pytest.raises(RuntimeError):
        critic.score("def test_z(): pass", samples=2)
