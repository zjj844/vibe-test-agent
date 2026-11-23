"""① 需求知识库：Markdown 需求文档 → 层级分块 → 向量检索。

和"普通 RAG"的区别就在**层级**：
不按固定长度硬切，而是先按 Markdown 标题（## / ###）划成"模块"，
再在模块内部按句子切子块。好处：
- 检索命中的块自带"它属于哪个模块的哪一节"，生成用例时上下文不会串味；
- 可以打印模块结构树给人核对（切错了人一眼能看出来）。

复用 `selfheal/retrieval/` 里的 chunk_text / HashingEmbedder / VectorStore —— 不重复造轮子。
（那几个文件原本在 RAG 产品仓库的 `rag/` 下，为了让本工具**不依赖另一个仓库**，做了精简移植。）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .retrieval import HashingEmbedder, VectorStore, chunk_text
from .retrieval import Retriever

DEFAULT_DOCS_DIR = Path(__file__).resolve().parent / "requirements_docs"

# ---------------------------------------------------------------------------
# 分节：优先认 Markdown 标题，其次认"中文式编号标题"
# ---------------------------------------------------------------------------
# 为什么不能只认 `##`：
# 实测踩到 —— 用户粘进来的 PRD 长这样（国内团队很常见）：
#
#     SSM 企业级CMS内容管理系统 验收需求（FR）
#     FR-1 登录返回 token 与权限码
#     POST /api/auth/login 成功时 code=200 ...
#     FR-2 注册默认 viewer
#     ...
#
# 一条 `##` 都没有。只认 Markdown 标题的话，整篇会被当成"一个巨大的模块"，
# 然后按 300 字硬切 —— 一条需求被劈成两半，检索必然串味。
# 所以这里额外认三种编号式标题：`FR-1 xxx` / `1. xxx` / `一、xxx`。
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)

_NUMBERED_HEADING = re.compile(
    r"^(?:"
    r"([A-Z]{2,6}-\d{1,3})"          # FR-1 / NFR-2 / AC-3
    r"|(\d{1,2}\.\d{0,2})"           # 1. / 1.2
    r"|([一二三四五六七八九十]{1,3})、"  # 一、 / 十二、
    r")\s*(.+?)\s*$",
    re.MULTILINE,
)


def _split_numbered(text: str) -> list[tuple[int, str, str]]:
    """按编号式标题切节，返回 [(level, title, body)]。

    只在"整篇没有 Markdown 标题"时才用 —— 有 `##` 就优先信 Markdown，
    避免把正文里的"1."当成标题（比如列表编号）。
    """
    matches = list(_NUMBERED_HEADING.finditer(text))
    if not matches:
        return []
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = m.group(4) or ""
        prefix = m.group(1) or m.group(2) or m.group(3) or ""
        full_title = f"{prefix} {title}".strip()
        out.append((2, full_title, text[start:end].strip()))
    return out



@dataclass
class Section:
    doc_id: str
    doc_title: str
    title: str
    level: int
    text: str


@dataclass
class RequirementKB:
    docs_dir: Path = DEFAULT_DOCS_DIR
    chunk_size: int = 300
    overlap: int = 60
    embedder: HashingEmbedder = field(default_factory=lambda: HashingEmbedder(dim=2048))

    def __post_init__(self) -> None:
        self.store = VectorStore()
        self.retriever = Retriever(self.store, self.embedder, top_k=3)
        self.sections: list[Section] = []
        self._build()

    # ---- 解析：按标题切模块 ----
    @staticmethod
    def _split_sections(doc_id: str, doc_title: str, text: str) -> list[Section]:
        matches = list(_HEADING.finditer(text))
        if matches:
            sections: list[Section] = []
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                sections.append(
                    Section(doc_id, doc_title, m.group(2).strip(), len(m.group(1)),
                            text[start:end].strip())
                )
            return sections

        # 没有 Markdown 标题 → 试试"中文式编号标题"（FR-1 / 1. / 一、）
        numbered = _split_numbered(text)
        if numbered:
            # 第一个编号之前的内容（文档标题/验收口径）单独成节，别丢掉
            head_end = text.find(numbered[0][2][:20]) if numbered else 0
            sections = []
            head = text[:head_end].strip() if head_end > 0 else ""
            if head:
                sections.append(Section(doc_id, doc_title, "概述", 1, head))
            sections.extend(
                Section(doc_id, doc_title, title, level, body)
                for level, title, body in numbered
            )
            return sections

        return [Section(doc_id, doc_title, doc_title, 1, text)]

    def _build(self) -> None:
        all_chunks = []
        for path in sorted(Path(self.docs_dir).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            doc_id = path.stem
            doc_title = text.strip().splitlines()[0].lstrip("# ").strip()
            for idx, section in enumerate(self._split_sections(doc_id, doc_title, text)):
                self.sections.append(section)
                # 子块 id 里带上小节序号，方便回溯"这条用例出自哪一节"
                sub_id = f"{doc_id}#s{idx:02d}"
                all_chunks.extend(
                    chunk_text(section.text, sub_id, chunk_size=self.chunk_size, overlap=self.overlap)
                )
        vectors = [self.embedder.embed(c.text) for c in all_chunks]
        self.store.add(all_chunks, vectors)

    # ---- 对外接口 ----
    @property
    def doc_count(self) -> int:
        return len({s.doc_id for s in self.sections})

    @property
    def chunk_count(self) -> int:
        return len(self.store)

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        return self.retriever.search(query, top_k=top_k)

    def tree(self) -> str:
        """模块结构树：把"AI 把需求切成了什么样"摊开给人核对。"""
        lines: list[str] = []
        current_doc = None
        for s in self.sections:
            if s.doc_id != current_doc:
                current_doc = s.doc_id
                lines.append(f"📄 {s.doc_title}  ({s.doc_id})")
            indent = "   " * (s.level - 1)
            lines.append(f"{indent}├─ {s.title}")
        return "\n".join(lines)
