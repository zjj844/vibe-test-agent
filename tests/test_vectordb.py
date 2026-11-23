"""vectordb.py 的离线测试（SQLite 向量索引 + BM25 + 混合检索 + 评测）。

替身策略：语料用极小的人造文本（3 篇），断言只针对"能不能正确排序/命中"，
不追求语义效果 —— 语义效果是 `scripts/eval_retrieval.py` 的活，需要真语料。
"""
from __future__ import annotations

import pytest

from selfheal import vectordb
from selfheal.retrieval.embedding import HashingEmbedder

DOCS = [
    {"doc_id": "线程规范", "section": "并发", "text": "线程池不允许使用 Executors 创建，必须用 ThreadPoolExecutor。"},
    {"doc_id": "集合规范", "section": "集合", "text": "集合初始化时指定初始值大小，避免扩容。"},
    {"doc_id": "日志规范", "section": "日志", "text": "日志文件至少保存 15 天，禁止使用 System.out。"},
]


@pytest.fixture()
def index(ws_tmp):
    idx = vectordb.VectorIndex(ws_tmp / "index.sqlite3", embedder=HashingEmbedder(dim=512), dim=512)
    idx.rebuild(DOCS)
    return idx


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------
def test_tokenize_keeps_english_words_and_cjk_bigrams():
    toks = vectordb.tokenize("线程池 Executors 创建")
    assert "executors" in toks
    assert "线程" in toks and "程池" in toks

    # 中文字符也被单独保留（单字 + 二元组）
    assert "线" in toks


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------
def test_bm25_ranks_keyword_document_first():
    bm = vectordb.BM25()
    for doc in DOCS:
        bm.add(doc["text"])
    bm.finalize()

    ranked = bm.search("Executors 线程池", k=3)
    assert ranked, "应该至少命中一条"
    assert ranked[0][0] == 0                      # 第 0 篇是线程规范


def test_bm25_empty_index_returns_empty():
    bm = vectordb.BM25()
    bm.finalize()
    assert bm.search("任意") == []


# ---------------------------------------------------------------------------
# 索引与持久化
# ---------------------------------------------------------------------------
def test_rebuild_and_stats(index):
    stats = index.stats()

    assert stats["chunks"] == 3
    assert stats["docs"] == 3
    assert stats["dim"] == 512
    assert stats["db_bytes"] > 0


def test_index_persists_across_instances(index, ws_tmp):
    """关键：换一个实例（新进程的等价物）不用重建就能搜。"""
    again = vectordb.VectorIndex(index.db_path, embedder=HashingEmbedder(dim=512), dim=512)

    assert again.stats()["chunks"] == 3
    hits = again.search("Executors", k=1, mode="bm25")
    assert hits and hits[0].doc_id == "线程规范"


# ---------------------------------------------------------------------------
# 三种检索模式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_all_modes_return_hits_with_source(index, mode):
    hits = index.search("Executors 线程池", k=2, mode=mode)

    assert hits, f"{mode} 应该命中"
    assert all(h.doc_id and h.text for h in hits)
    if mode != "hybrid":
        assert hits[0].source == mode
    else:
        assert hits[0].source == "hybrid"


def test_hybrid_records_both_ranks(index):
    hits = index.search("日志 15 天", k=3, mode="hybrid")

    top = hits[0]
    assert top.doc_id == "日志规范"
    assert top.ranks, "混合检索必须记录两路排名（便于解释为什么这条排第一）"


def test_unknown_mode_raises(index):
    with pytest.raises(ValueError):
        index.search("x", mode="magic")


def test_search_is_deterministic(index):
    first = [h.chunk_id for h in index.search("线程池", k=3, mode="hybrid")]
    second = [h.chunk_id for h in index.search("线程池", k=3, mode="hybrid")]
    assert first == second


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------
def test_evaluate_computes_recall_and_mrr(index):
    cases = [
        {"query": "Executors 线程池", "gold": ["线程规范"]},
        {"query": "日志保存 15 天", "gold": ["日志规范"]},
        {"query": "完全不相关的词 zzzz", "gold": ["线程规范"]},
    ]

    result = vectordb.evaluate(index, cases, k=3, modes=("bm25",))

    data = result["bm25"]
    assert 0.0 <= data["recall"] <= 1.0
    assert 0.0 <= data["mrr"] <= 1.0
    assert len(data["details"]) == 3
    # 前两条应该命中（第三条是非相关查询，命中与否都不该影响前两条的正确性）
    assert data["details"][0]["rank"] == 1
    assert data["details"][1]["rank"] == 1


def test_evaluate_on_empty_cases(index):
    result = vectordb.evaluate(index, [], k=3, modes=("hybrid",))
    assert result["hybrid"]["recall"] == 0.0
    assert result["hybrid"]["mrr"] == 0.0


def test_rebuild_is_idempotent(index):
    """重复 rebuild 不能让块数翻倍（这是索引最容易写成 bug 的地方）。"""
    index.rebuild(DOCS)
    assert index.stats()["chunks"] == 3
