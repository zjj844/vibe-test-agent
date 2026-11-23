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
    """需求 + 外部规范的知识库。

    两种工作方式（**默认行为不变**，只有给了 external_dir 才升级）：

      1. 旧方式（external_dir=None）：内存 VectorStore + 向量检索
         —— 只需要"本项目的需求文档"时够用，也是所有老测试跑的路径。

      2. 新方式（给了 external_dir）：把需求文档 + 外部规范**一起**切块，
         灌进 SQLite 持久化索引，检索走 hybrid（向量 + BM25 的 RRF 融合）。
         —— 为什么升级：外部规范（如《阿里巴巴 Java 开发手册》）是"组织级测试基线"，
         只有和需求文档混在一起检索，生成的用例才会带上"表必备三字段""日志保存 15 天"
         这类通用约束。
    """

    docs_dir: Path = DEFAULT_DOCS_DIR
    chunk_size: int = 300
    overlap: int = 60
    embedder: HashingEmbedder = field(default_factory=lambda: HashingEmbedder(dim=2048))
    external_dir: Path | None = None          # 外部规范语料目录（可选）
    mode: str = "hybrid"                      # vector / bm25 / hybrid（仅索引模式生效）
    index_db: Path | None = None              # 索引落盘位置（默认 .index/kb.sqlite3）

    def __post_init__(self) -> None:
        self.store = VectorStore()
        self.retriever = Retriever(self.store, self.embedder, top_k=3)
        self.sections: list[Section] = []
        self.index = None                     # 升级后是 vectordb.VectorIndex
        self.external_chunks = 0
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

    # ---- 建库 ----
    def _collect(self, base_dir: Path, prefix: str = "", recursive: bool = False) -> list:
        """把一个目录下的 md 切块，返回 chunk 列表（同时记 section）。

        为什么 external 要 recursive、requirements 不递归：
          · 需求文档目录 = "这个项目的一份/几份文档"，平铺就好，
            递归反而会把"别的项目的需求"误收进来（实测：默认目录下按项目分了子目录，
            递归后 doc_count 从 3 变成 8，检索就串项目了）
          · 外部语料 = 按来源分层存放（`corpus_external/alibaba-p3c/`、`aliyun-openapi/`），
            不递归会**一条都读不到**——实测踩过，external_chunks 一直是 0
        """
        chunks = []
        base = Path(base_dir)
        pattern_iter = base.rglob("*.md") if recursive else base.glob("*.md")
        for path in sorted(pattern_iter):
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(base)
            stem = f"{rel.parent.name}/{path.stem}" if len(rel.parts) > 1 else path.stem
            doc_id = f"{prefix}{stem}"
            doc_title = text.strip().splitlines()[0].lstrip("# ").strip() if text.strip() else path.stem
            for idx, section in enumerate(self._split_sections(doc_id, doc_title, text)):
                self.sections.append(section)
                # 子块 id 里带上小节序号，方便回溯"这条用例出自哪一节"
                sub_id = f"{doc_id}#s{idx:02d}"
                chunks.extend(
                    chunk_text(section.text, sub_id, chunk_size=self.chunk_size, overlap=self.overlap)
                )
        return chunks

    def _build(self) -> None:
        req_chunks = self._collect(Path(self.docs_dir))
        vectors = [self.embedder.embed(c.text) for c in req_chunks]
        self.store.add(req_chunks, vectors)

        if not self.external_dir:
            return                                 # 旧路径：只用内存版，行为完全不变

        # 升级路径：需求 + 外部规范一起进持久化索引
        from . import vectordb

        ext_dir = Path(self.external_dir)
        ext_chunks = (
            self._collect(ext_dir, prefix="规范·", recursive=True) if ext_dir.is_dir() else []
        )
        self.external_chunks = len(ext_chunks)

        db_path = self.index_db or (
            Path(__file__).resolve().parent.parent / ".index" / "kb.sqlite3"
        )
        self.index = vectordb.VectorIndex(db_path, embedder=self.embedder)

        records = [
            {"doc_id": c.doc_id, "section": "", "text": c.text}
            for c in (req_chunks + ext_chunks)
        ]
        self.index.rebuild(records)

    # ---- 对外接口 ----
    @property
    def doc_count(self) -> int:
        return len({s.doc_id for s in self.sections})

    @property
    def chunk_count(self) -> int:
        if self.index is not None:
            return self.index.stats()["chunks"]
        return len(self.store)

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        if self.index is None:
            return self.retriever.search(query, top_k=top_k)

        # 混检时给外部规范留一个名额
        # ------------------------------------------------------------------
        # 为什么需要："给需求文档写的查询词"（接口名/业务含义）天然更贴近需求文档，
        # 规范文档（讲建表/日志/命名）几乎永远排不进 top-3 —— 实测就是这样：
        # 外部规范明明在索引里，读需求却一条都没检索到。
        # 所以显式留配额：1 条规范 + 其余需求。这也是"组织规范必须参与"的产品判断，
        # 不是调参技巧。
        if not self.external_chunks or top_k < 2:
            return [h.as_dict() for h in self.index.search(query, k=top_k, mode=self.mode)]

        pool = [h for h in self.index.search(query, k=max(top_k * 4, 12), mode=self.mode)]
        ext = [h for h in pool if h.doc_id.startswith("规范·")]
        req = [h for h in pool if not h.doc_id.startswith("规范·")]

        # 顺序讲究：需求在前（它决定"测什么"），规范在后当补充约束
        chosen = req[: max(top_k - 1, 0)] + ext[:1]
        if len(chosen) < top_k:                    # 名额没填满就用剩下的补齐
            rest = [h for h in pool if h not in chosen]
            chosen += rest[: top_k - len(chosen)]
        return [h.as_dict() for h in chosen]

    def index_stats(self) -> dict:
        """索引概况（日志/报告用）：块数、文档数、外部语料块数、检索模式。"""
        stats = {
            "mode": "memory" if self.index is None else self.mode,
            "chunks": self.chunk_count,
            "docs": self.doc_count,
            "external_chunks": self.external_chunks,
        }
        if self.index is not None:
            stats.update({k: v for k, v in self.index.stats().items() if k in ("db_bytes", "dim")})
        return stats

    def tree(self) -> str:
        """模块结构树：把"AI 把需求切成了什么样"摊开给人核对。"""
        lines: list[str] = []
        current_doc = None
        for s in self.sections:
            if s.doc_id != current_doc:
                current_doc = s.doc_id
                mark = "📚" if s.doc_id.startswith("规范·") else "📄"
                lines.append(f"{mark} {s.doc_title}  ({s.doc_id})")
            indent = "   " * (s.level - 1)
            lines.append(f"{indent}├─ {s.title}")
        return "\n".join(lines)
