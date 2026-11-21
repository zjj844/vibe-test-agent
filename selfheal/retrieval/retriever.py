"""检索器：把"问题 → 若干块"这一步单独抽出来。

为什么要单独一层：
评估里最值钱的指标（Recall@5 / MRR）测的就是这一层，
如果它和生成混在一起，你就没法说清"是检索没找到，还是模型答歪了"。
"""
from __future__ import annotations

from .embedding import HashingEmbedder
from .store import VectorStore


class Retriever:
    def __init__(self, store: VectorStore, embedder: HashingEmbedder, top_k: int = 5) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = top_k

    def search(self, question: str, top_k: int | None = None) -> list[dict]:
        """返回 [{doc_id, chunk_id, text, score}, ...]，按分数降序。"""
        if not question or not question.strip():
            raise ValueError("question 不能为空")
        k = self.top_k if top_k is None else top_k
        qv = self.embedder.embed(question)
        hits = []
        for chunk, score in self.store.search(qv, top_k=k):
            hits.append(
                {
                    "doc_id": chunk.doc_id,
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "score": score,
                }
            )
        return hits
