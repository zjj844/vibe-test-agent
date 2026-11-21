"""向量库：内存版，够 20-200 个块用。

真实项目里替换成 Chroma / Milvus，但接口只有 add / search 两个方法，
所以上层（retriever、pipeline、测试）都不用改——这是可测试性的前提。
"""
from __future__ import annotations

from .chunking import Chunk


class VectorStore:
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []

    def __len__(self) -> int:
        return len(self._chunks)

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量必须一致")
        for c, v in zip(chunks, vectors):
            self._chunks.append(c)
            self._vectors.append(v)

    def search(self, query_vector: list[float], top_k: int = 5) -> list[tuple[Chunk, float]]:
        """余弦相似度：向量已归一化，所以点积即余弦。"""
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if not self._chunks:
            return []
        scored = [
            (chunk, sum(a * b for a, b in zip(query_vector, vec)))
            for chunk, vec in zip(self._chunks, self._vectors)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(c, round(s, 4)) for c, s in scored[:top_k]]
