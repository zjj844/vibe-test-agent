"""嵌入：把文本变成向量。

为什么默认用"哈希 n-gram"而不是 bge / OpenAI：
- 演示和 CI 需要**确定性**：同一句话任何时候都得到同一个向量，测试才不会时红时绿；
- 零依赖：clone 下来就能跑，不需要下模型、不需要 API key；
- 接口一致：真实项目里把 embed() 换成 bge-small-zh-v1.5，其余代码一行不改。
"""
from __future__ import annotations

import math
import re
import zlib
from collections import Counter

_WORD = re.compile(r"[a-zA-Z0-9_.]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def grams(text: str, ns: tuple[int, ...] = (2, 3)) -> list[str]:
    """取中文字符 2/3-gram + 英文/数字整词。

    中文没有空格，字符 n-gram 是最稳的离线切分方式：
    "清华镜像" 会产出 清华/华镜/镜像/清华镜/华镜像 等片段，天然支持部分匹配。

    ⚠️ 关键过滤：**纯 ASCII 的字符 n-gram 全部丢弃**。
    否则 "RAG" 会切出 "ra"/"ag"/"g " 这类碎片，它们永远匹配不上中文语料，
    只会稀释相似度与支持度的分母——实测这让 "评估 RAG 系统要看哪些指标"
    的支持度从 0.10 掉到 0.037，造成误拒答。英文一律走整词匹配（_WORD）。
    """
    t = (text or "").lower()
    out: list[str] = []
    for n in ns:
        for i in range(len(t) - n + 1):
            g = t[i : i + n]
            if not g.strip():
                continue
            if not _CJK.search(g):
                continue
            out.append(g)
    out.extend(m.group(0) for m in _WORD.finditer(t))
    return out


class HashingEmbedder:
    """确定性哈希嵌入：n-gram → crc32 → 带符号累加到固定维度向量 → L2 归一化。"""

    def __init__(self, dim: int = 2048) -> None:
        if dim <= 0:
            raise ValueError("dim 必须大于 0")
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text:
            return vec
        # 带符号哈希：同一 gram 固定落到同一维同一符号，降低碰撞带来的假相似
        for g, k in Counter(grams(text)).items():
            h = zlib.crc32(g.encode("utf-8"))
            idx = h % self.dim
            sign = 1.0 if (h >> 15) & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(k))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
