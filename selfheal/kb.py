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

# 只认二级/三级标题：一级标题是文档名，四级以下太细
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)


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
        if not matches:
            return [Section(doc_id, doc_title, doc_title, 1, text)]

        sections: list[Section] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append(
                Section(doc_id, doc_title, m.group(2).strip(), len(m.group(1)), text[start:end].strip())
            )
        return sections

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
