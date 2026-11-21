"""分块：把长文档切成可检索的小段。

为什么单独成模块、且必须是纯函数：
- 分块是 RAG 里对检索质量影响最大的一步，必须能单独调参、单独断言；
- 它不碰网络、不碰模型，所以能进 CI（确定性层），每次 push 都跑。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

_SENT_END = "。！？!?…\n"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    start: int
    end: int

    def as_dict(self) -> dict:
        return asdict(self)


def normalize(text: str) -> str:
    """统一换行/空白，避免同一份文档在不同平台切出不同结果。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """按中文句末标点 + 换行切句。

    为什么按句切而不是硬按字符数切：
    硬切会把一句话劈成两半，检索命中其中一半时答案就断了。
    """
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENT_END:
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return [s for s in (x.strip() for x in out) if s]


def chunk_text(
    text: str,
    doc_id: str,
    chunk_size: int = 200,
    overlap: int = 40,
) -> list[Chunk]:
    """按句打包成块，相邻块共享尾部若干句（overlap）。

    参数约束会被断言：chunk_size>0、0<=overlap<chunk_size。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须 >=0 且 < chunk_size")

    text = normalize(text)
    if not text:
        return []

    # 超长单句先硬切，保证任何块的字符数都不超过 chunk_size
    pieces: list[str] = []
    for sent in split_sentences(text):
        if len(sent) <= chunk_size:
            pieces.append(sent)
        else:
            for i in range(0, len(sent), chunk_size):
                pieces.append(sent[i : i + chunk_size])

    bodies: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def emit() -> None:
        nonlocal cur, cur_len
        body = "".join(cur).strip()
        if body:
            bodies.append(body)
        cur, cur_len = [], 0

    for sent in pieces:
        if cur and cur_len + len(sent) > chunk_size:
            # 取尾部若干句作为下一块的开头（overlap）
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(cur):
                if tail_len + len(prev) <= overlap:
                    tail.insert(0, prev)
                    tail_len += len(prev)
                else:
                    break
            emit()
            cur, cur_len = list(tail), tail_len
            if cur_len + len(sent) > chunk_size:
                # 兜底：overlap 会把块撑爆时，宁可不要 overlap 也不超限
                cur, cur_len = [], 0
        cur.append(sent)
        cur_len += len(sent)
    emit()

    chunks: list[Chunk] = []
    cursor = 0
    for i, body in enumerate(bodies):
        start = text.find(body[:20], cursor)
        if start < 0:
            start = cursor
        end = start + len(body)
        cursor = start + 1
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}#c{i:02d}",
                doc_id=doc_id,
                text=body,
                start=start,
                end=end,
            )
        )
    return chunks
