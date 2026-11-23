"""M13 · 检索层升级（vectordb.py）—— 真·持久化索引 + 混合检索

=====================================================================
为什么需要这个模块
=====================================================================
原来的检索是"每次启动把十几块文本重新算一遍哈希向量，全放内存"。
语料一变大（比如把《阿里巴巴 Java 开发手册》当"外部规范"引进来），
三个问题立刻暴露：

  1. **重复计算**：每次运行都重新分块 + 重新向量化
  2. **语义不够**：纯哈希向量抓的是"字面重叠"，关键词精确匹配反而不如
     BM25 这类经典倒排检索
  3. **说不清效果**：没评测，就不知道换方案到底好不好

所以这个模块干三件事：
  · **持久化**：块 + 向量 + 元数据落到 SQLite（标准库，零依赖），启动直接读
  · **混合检索**：向量检索 + BM25 检索，用 RRF 融合
  · **可评测**：接口固定（`search(query, k)`），方便拿标注查询算 recall@k / MRR

=====================================================================
为什么用 SQLite，而不是 Chroma / FAISS
=====================================================================
判据只有一条：**当前数据量配不上那个复杂度**。
  · 本项目的语料是"十几篇规范 + 几篇需求"，几百到几千块
  · SQLite 一个文件、零依赖、跨机器可拷贝、可 `sqlite3` 直接查
  · 检索是暴力余弦（O(N·d)），几千块耗时毫秒级，够用

**边界要讲清**：块数到 10 万级、要毫秒响应时，暴力检索会线性变慢，
那时候该换上 HNSW/FAISS + 真正的 embedding 服务 —— 这是 v2 的事，
不是"现在偷懒"。`VectorIndex` 的接口就是为将来替换留的缝。

=====================================================================
RRF（Reciprocal Rank Fusion）是什么
=====================================================================
两路检索的分数不可比（余弦相似度 vs BM25 分数，量纲完全不同），
直接加权平均得先归一化，而归一化又会引入新假设。
RRF 只看**排名**：`score = Σ 1/(k + rank)`，简单、无需归一化、对异常分数稳健。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from array import array
from dataclasses import dataclass, field
from pathlib import Path

# 中文按字 + 二元组切，英文/数字按词切 —— 不引分词器（jieba 是额外依赖）
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")

DEFAULT_DB = Path(__file__).resolve().parent.parent / ".index" / "corpus.sqlite3"


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    """中文单字 + 相邻二元组 + 英文/数字词。

    为什么不用 jieba：本项目语料是技术文档，加上二元组之后"索引规约""线程池"
    这类术语已经能被精确命中；而多引一个分词器就多一份安装与版本负担。
    """
    raw = _TOKEN_RE.findall(text.lower())
    tokens: list[str] = []
    cjk_run: list[str] = []
    for tok in raw:
        if len(tok) == 1 and not tok.isascii():          # 中文字符
            cjk_run.append(tok)
            tokens.append(tok)
        else:
            if cjk_run:
                tokens.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))
                cjk_run = []
            tokens.append(tok)
    if cjk_run:
        tokens.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))
    return tokens


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------
class BM25:
    """经典 BM25 倒排检索。参数用默认值（k1=1.5, b=0.75）。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_tokens: list[list[str]] = []
        self.doc_len: list[int] = []
        self.avg_len = 0.0
        self.df: dict[str, int] = {}
        self.postings: dict[str, dict[int, int]] = {}     # token -> {doc_idx: tf}

    def add(self, text: str) -> int:
        idx = len(self.doc_tokens)
        toks = tokenize(text)
        self.doc_tokens.append(toks)
        self.doc_len.append(len(toks))
        for tok in toks:
            self.postings.setdefault(tok, {})
            self.postings[tok][idx] = self.postings[tok].get(idx, 0) + 1
        for tok in set(toks):
            self.df[tok] = self.df.get(tok, 0) + 1
        return idx

    def finalize(self) -> None:
        n = len(self.doc_len)
        self.avg_len = (sum(self.doc_len) / n) if n else 0.0

    def search(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        n = len(self.doc_len)
        if not n:
            return []
        q_tokens = [t for t in tokenize(query) if t in self.postings]
        scores: dict[int, float] = {}
        for tok in q_tokens:
            idf = math.log(1 + (n - self.df.get(tok, 0) + 0.5) / (self.df.get(tok, 0) + 0.5))
            for doc_idx, tf in self.postings[tok].items():
                dl = self.doc_len[doc_idx] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * (tf * (self.k1 + 1) / denom)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:k]


# ---------------------------------------------------------------------------
# SQLite 持久化向量索引
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   TEXT NOT NULL,
    section  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL,
    tokens   TEXT NOT NULL DEFAULT '',
    vector   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


@dataclass
class Hit:
    """一条检索结果。"""

    chunk_id: str
    doc_id: str
    section: str
    text: str
    score: float
    source: str = ""            # vector / bm25 / hybrid
    ranks: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "section": self.section,
            "text": self.text,
            "score": round(self.score, 6),
            "source": self.source,
            "ranks": self.ranks,
        }


class VectorIndex:
    """SQLite 里的向量索引：块、向量、元数据都在一个文件里。

    `embedder` 只需实现 `embed(text) -> list[float]`（项目里用的是 HashingEmbedder）。
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB, embedder=None, dim: int = 2048) -> None:
        self.db_path = Path(db_path)
        self.dim = dim
        self.embedder = embedder
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: list[tuple[int, str, str, str, list[float]]] | None = None
        self._bm25: BM25 | None = None

    # ---- 建/写 ----
    def rebuild(self, records: list[dict]) -> int:
        """全量重建索引。records: [{doc_id, section, text}]（chunk_id 自动编）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM chunks")
            for rec in records:
                text = rec["text"]
                vec = self._embed(text)
                conn.execute(
                    "INSERT INTO chunks (doc_id, section, text, tokens, vector) VALUES (?,?,?,?,?)",
                    (
                        rec["doc_id"],
                        rec.get("section", ""),
                        text,
                        json.dumps(tokenize(text)[:4000], ensure_ascii=False),
                        array("f", vec).tobytes(),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('dim', ?)", (str(self.dim),)
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('count', ?)",
                (str(len(records)),),
            )
            conn.commit()
        finally:
            conn.close()
        self._rows = None
        self._bm25 = None
        return len(records)

    # ---- 读 ----
    def _load(self) -> list[tuple[int, str, str, str, list[float]]]:
        if self._rows is not None:
            return self._rows
        if not self.db_path.is_file():
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT id, doc_id, section, text, vector FROM chunks").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        parsed: list[tuple[int, str, str, str, list[float]]] = []
        for cid, doc_id, section, text, blob in rows:
            vec = list(array("f", blob))
            parsed.append((cid, doc_id, section, text, vec))
        self._rows = parsed
        return parsed

    def _bm25_index(self) -> BM25:
        if self._bm25 is not None:
            return self._bm25
        index = BM25()
        for _cid, _doc, _sec, text, _vec in self._load():
            index.add(text)
        index.finalize()
        self._bm25 = index
        return index

    def stats(self) -> dict:
        rows = self._load()
        docs = {r[1] for r in rows}
        size = self.db_path.stat().st_size if self.db_path.is_file() else 0
        return {
            "chunks": len(rows),
            "docs": len(docs),
            "dim": self.dim,
            "db_bytes": size,
            "db": str(self.db_path),
        }

    # ---- 检索 ----
    def _embed(self, text: str) -> list[float]:
        if self.embedder is None:
            from .retrieval.embedding import HashingEmbedder

            self.embedder = HashingEmbedder(dim=self.dim)
        vec = self.embedder.embed(text)
        return list(vec)

    def search_vector(self, query: str, k: int = 5) -> list[Hit]:
        q = self._embed(query)
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        scored: list[tuple[int, float]] = []
        for cid, doc_id, section, text, vec in self._load():
            dot = sum(a * b for a, b in zip(q, vec))
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            scored.append((cid, dot / (qn * norm)))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return self._hits(scored[:k], "vector")

    def search_bm25(self, query: str, k: int = 5) -> list[Hit]:
        index = self._bm25_index()
        ranked = index.search(query, k)
        rows = self._load()
        hits: list[Hit] = []
        for doc_idx, score in ranked:
            if doc_idx >= len(rows):
                continue
            cid, doc_id, section, text, _vec = rows[doc_idx]
            hits.append(
                Hit(
                    chunk_id=f"{doc_id}#{cid}",
                    doc_id=doc_id,
                    section=section,
                    text=text,
                    score=float(score),
                    source="bm25",
                )
            )
        return hits

    def search(self, query: str, k: int = 5, mode: str = "hybrid",
               rrf_k: int = 60) -> list[Hit]:
        """统一入口。mode: vector / bm25 / hybrid（默认混合，用 RRF 融合）。"""
        if mode == "vector":
            return self.search_vector(query, k)
        if mode == "bm25":
            return self.search_bm25(query, k)
        if mode != "hybrid":
            raise ValueError(f"未知检索模式：{mode}")

        pool = max(k * 4, 20)
        vec_hits = self.search_vector(query, pool)
        bm_hits = self.search_bm25(query, pool)

        fused: dict[str, Hit] = {}
        for source, hits in (("vector", vec_hits), ("bm25", bm_hits)):
            for rank, hit in enumerate(hits, start=1):
                cur = fused.get(hit.chunk_id)
                if cur is None:
                    cur = Hit(
                        chunk_id=hit.chunk_id, doc_id=hit.doc_id, section=hit.section,
                        text=hit.text, score=0.0, source="hybrid",
                    )
                    fused[hit.chunk_id] = cur
                cur.score += 1.0 / (rrf_k + rank)
                cur.ranks[source] = rank
        ordered = sorted(fused.values(), key=lambda h: h.score, reverse=True)
        return ordered[:k]

    # ---- 内部 ----
    def _hits(self, scored: list[tuple[int, float]], source: str) -> list[Hit]:
        rows = {r[0]: r for r in self._load()}
        hits: list[Hit] = []
        for cid, score in scored:
            row = rows.get(cid)
            if row is None:
                continue
            _cid, doc_id, section, text, _vec = row
            hits.append(
                Hit(
                    chunk_id=f"{doc_id}#{cid}",
                    doc_id=doc_id,
                    section=section,
                    text=text,
                    score=float(score),
                    source=source,
                )
            )
        return hits


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------
def evaluate(index: VectorIndex, cases: list[dict], k: int = 3,
             modes: tuple[str, ...] = ("vector", "bm25", "hybrid")) -> dict:
    """算 recall@k 与 MRR。

    cases: [{"query": str, "gold": [doc_id, ...]}] —— gold 是"正确答案所在文档"。
    为什么用文档级而不是块级做标注：标注成本低得多，而且对"喂给模型材料"
    这个用途来说，命中同一篇文档的关键段落就够用了。
    """
    results: dict[str, dict] = {}
    for mode in modes:
        hit_count = 0
        mrr_sum = 0.0
        details: list[dict] = []
        for case in cases:
            hits = index.search(case["query"], k=k, mode=mode)
            gold = set(case["gold"])
            rank_of_gold = next(
                (i for i, h in enumerate(hits, start=1) if h.doc_id in gold), None
            )
            if rank_of_gold:
                hit_count += 1
                mrr_sum += 1.0 / rank_of_gold
            details.append(
                {
                    "query": case["query"],
                    "gold": sorted(gold),
                    "top": [h.doc_id for h in hits],
                    "rank": rank_of_gold,
                }
            )
        results[mode] = {
            "recall": hit_count / len(cases) if cases else 0.0,
            "mrr": mrr_sum / len(cases) if cases else 0.0,
            "details": details,
        }
    return results
