"""检索评测：把"外部规范语料 + 三种检索模式"跑出真实数字。

输出 recall@3 / MRR，分别对：
  · vector  —— 纯哈希向量（原来 kb.py 的做法）
  · bm25    —— 纯关键词倒排
  · hybrid  —— 两者 RRF 融合（本项目推荐）

为什么一定要有这一步：没有标注查询和数字，"换成混合检索更好"就是一句空话。
标注是**文档级**的（命中同一篇规范即算对），因为标注成本低、且对"喂材料"
这个用途足够；块级标注留给 v2。

用法：
    python scripts/eval_retrieval.py            # 建索引 + 评测 + 打印
    python scripts/eval_retrieval.py --md       # 顺便写出 docs/07-检索评测.md
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from selfheal import vectordb                              # noqa: E402
from selfheal.retrieval.chunking import chunk_text         # noqa: E402

CORPUS_DIR = ROOT / "corpus_external"
INDEX_DB = ROOT / ".index" / "external.sqlite3"
DOC_MD = ROOT / "docs" / "07-检索评测.md"

# 标注查询 A：**字面重合型**（问题和规范用词高度一致）
CASES: list[dict] = [
    {"query": "类名必须使用 UpperCamelCase 风格", "gold": ["命名风格"], "check": "UpperCamelCase"},
    {"query": "常量命名全部大写，单词间用下划线隔开", "gold": ["命名风格"], "check": "常量"},
    {"query": "集合初始化时指定集合初始值大小", "gold": ["集合处理"], "check": "初始值"},
    {"query": "线程池不允许使用 Executors 去创建", "gold": ["并发处理"], "check": "Executors"},
    {"query": "SimpleDateFormat 是线程不安全的类", "gold": ["并发处理"], "check": "SimpleDateFormat"},
    {"query": "不要在 foreach 循环里进行元素的 remove 或 add 操作", "gold": ["集合处理"], "check": "foreach"},
    {"query": "不要在 finally 块中使用 return", "gold": ["异常处理"], "check": "finally"},
    {"query": "日志文件至少保存 15 天", "gold": ["日志规约"], "check": "15"},
    {"query": "表必备三字段 id gmt_create gmt_modified", "gold": ["建表规约"], "check": "gmt_create"},
    {"query": "唯一索引命名规则", "gold": ["索引规约", "建表规约"], "check": "唯一索引"},
    {"query": "不要使用 count 列名来替代 count 星号", "gold": ["SQL语句"], "check": "count"},
    {"query": "超过三个表禁止 join", "gold": ["索引规约"], "check": "join"},
    {"query": "应用分层中的开放接口层与 Web 层", "gold": ["应用分层"], "check": "分层"},
]

# 标注查询 B：**改述型**（用业务话术问，和规范用词几乎不重合）
# 这一组才是真实的失败来源 —— 用户/需求文档不会照着规范原文提问。
PARAPHRASE_CASES: list[dict] = [
    {"query": "多个线程同时用同一个日期格式化对象会发生什么", "gold": ["并发处理"], "check": "SimpleDateFormat"},
    {"query": "为什么创建集合时要先预估容量", "gold": ["集合处理"], "check": "初始值"},
    {"query": "遍历集合的过程中删元素为什么会报错", "gold": ["集合处理"], "check": "foreach"},
    {"query": "数据库表怎么记录创建时间和修改时间", "gold": ["建表规约"], "check": "gmt_create"},
    {"query": "查询全部字段和只查需要字段哪个更快", "gold": ["SQL语句"], "check": "count"},
    {"query": "控制器层应该写业务逻辑吗", "gold": ["应用分层"], "check": "分层"},
    {"query": "打日志太频繁会不会拖慢程序", "gold": ["日志规约"], "check": "日志"},
    {"query": "捕获异常后什么都不做会有什么风险", "gold": ["异常处理"], "check": "异常"},
]


def load_corpus() -> list[dict]:
    """把外部语料按 300 字 / 60 重叠切块（与 kb.py 一致，保证可比）。"""
    records: list[dict] = []
    for path in sorted(CORPUS_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # 每篇文档的前两级标题当 section
        section = ""
        body_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            body_lines.append(line)
        for chunk in chunk_text("\n".join(body_lines), path.stem, chunk_size=300, overlap=60):
            records.append(
                {
                    "doc_id": path.stem,
                    "section": section,
                    "text": chunk.text,
                }
            )
    return records


def build_index(records: list[dict], force: bool = False) -> tuple[vectordb.VectorIndex, float]:
    index = vectordb.VectorIndex(INDEX_DB)
    if INDEX_DB.is_file() and not force:
        stats = index.stats()
        if stats["chunks"] == len(records):
            return index, 0.0
    started = time.perf_counter()
    index.rebuild(records)
    return index, time.perf_counter() - started


def validate_labels(records: list[dict]) -> list[str]:
    """标注自检：期望文档里真的包含关键词吗？防止数字算在错标上。"""
    problems: list[str] = []
    by_doc: dict[str, str] = {}
    for rec in records:
        by_doc[rec["doc_id"]] = by_doc.get(rec["doc_id"], "") + rec["text"]
    for case in CASES + PARAPHRASE_CASES:
        for doc in case["gold"]:
            body = by_doc.get(doc, "")
            if not body:
                problems.append(f"{case['query'][:20]}… → 语料里没有文档 {doc}")
            elif case["check"] not in body:
                problems.append(
                    f"{case['query'][:20]}… → 文档 {doc} 里找不到关键词 {case['check']!r}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    records = load_corpus()
    if not records:
        print("corpus_external/ 下没有语料，先跑 scripts/fetch_external_corpus.py")
        return 1

    print(f"语料：{len({r['doc_id'] for r in records})} 篇 / {len(records)} 块")
    problems = validate_labels(records)
    if problems:
        print("\n⚠️ 标注自检有问题（数字会失真，先修标注）：")
        for line in problems:
            print("  ·", line)

    index, build_s = build_index(records, force="--rebuild" in argv)
    stats = index.stats()
    print(f"索引：{stats['chunks']} 块，{stats['db_bytes'] / 1024:.0f} KB，"
          f"重建耗时 {build_s * 1000:.0f} ms")

    easy = vectordb.evaluate(index, CASES, k=3)
    hard = vectordb.evaluate(index, PARAPHRASE_CASES, k=3)

    def show(title: str, result: dict) -> None:
        print(f"\n【{title}】")
        print(f"{'模式':<8}{'recall@3':>12}{'MRR':>10}")
        print("-" * 32)
        for mode, data in result.items():
            print(f"{mode:<8}{data['recall'] * 100:>11.1f}%{data['mrr']:>10.3f}")

    show(f"A 字面重合型（{len(CASES)} 条）", easy)
    show(f"B 改述型（{len(PARAPHRASE_CASES)} 条）", hard)

    print("\nB 组逐条（hybrid，最能暴露真实瓶颈）：")
    for row in hard["hybrid"]["details"]:
        mark = "✅" if row["rank"] else "❌"
        print(f"  {mark} rank={row['rank']}  {row['query'][:30]:<30} top={row['top'][:3]}")

    if "--md" in argv:
        write_md(easy, hard, stats, len(records), problems)
        print(f"\n已写出：{DOC_MD.relative_to(ROOT)}")
    return 0


def _md_table(result: dict) -> list[str]:
    lines = ["| 模式 | recall@3 | MRR |", "|---|---|---|"]
    for mode, data in result.items():
        lines.append(f"| {mode} | {data['recall'] * 100:.1f}% | {data['mrr']:.3f} |")
    return lines


def write_md(easy: dict, hard: dict, stats: dict, n_chunks: int, problems: list[str]) -> None:
    lines = [
        "# 检索评测（外部规范语料）",
        "",
        "> 由 `python scripts/eval_retrieval.py --md` 自动生成，数字可复现。",
        "",
        "## 语料",
        "",
        "- 来源：阿里巴巴《Java 开发手册》gitbook markdown + 阿里云 OpenAPI 规范页（`corpus_external/`）",
        f"- 规模：**{len({1}) and n_chunks} 块**（300 字 / 60 重叠），索引文件 {stats['db_bytes'] / 1024:.0f} KB（SQLite）",
        "- 切块参数与 `kb.py` 一致，保证线上可比",
        "",
        "## 结果 A：字面重合型查询（13 条）",
        "",
    ]
    lines += _md_table(easy)
    lines += ["", "## 结果 B：改述型查询（8 条，真实使用场景）", ""]
    lines += _md_table(hard)
    lines += ["", "### B 组逐条（hybrid）", "", "| 查询 | 期望文档 | 命中排名 | Top-3 |", "|---|---|---|---|"]
    for row in hard["hybrid"]["details"]:
        lines.append(
            f"| {row['query']} | {'/'.join(row['gold'])} | {row['rank'] or '未命中'} | "
            f"{', '.join(row['top'][:3])} |"
        )

    if problems:
        lines += ["", "## ⚠️ 标注自检问题", ""] + [f"- {p}" for p in problems]

    lines += [
        "",
        "## 结论（面试口径，按实测说）",
        "",
        f"1. **字面重合型**查询三路几乎都满分（recall@3 = {easy['hybrid']['recall'] * 100:.0f}%）："
        "语料小、术语唯一，谁都能命中，**说明这一组没有区分度**。",
        f"2. **改述型**查询才是真实场景，recall@3 掉到 {hard['hybrid']['recall'] * 100:.0f}% 左右"
        f"（vector {hard['vector']['recall'] * 100:.0f}% / bm25 {hard['bm25']['recall'] * 100:.0f}% / "
        f"hybrid {hard['hybrid']['recall'] * 100:.0f}%）：**瓶颈不在融合策略，而在 embedding 能力**。",
        "3. **所以我的结论不是『混合检索真香』**，而是：融合能补关键词精确匹配，"
        "但语义召回必须靠真 embedding —— 这是我明确排进 v2 的第一项，"
        "接口已留好（`VectorIndex(embedder=...)`，实现 `embed(text)` 即可换）。",
        f"4. **存储代价**：{stats['chunks']} 块索引 {stats['db_bytes'] / 1024:.0f} KB，"
        "哈希向量 2048 维 float32 每块约 8KB，比原文（300 字≈0.9KB）大近 9 倍 —— "
        "真 embedding 常见 768~1536 维，空间反而更省，这也是换的理由之一。",
        "5. **没上向量库**：几百块用 SQLite + 暴力余弦毫秒级；到 10 万块级再换 HNSW/FAISS。",
    ]
    DOC_MD.parent.mkdir(parents=True, exist_ok=True)
    DOC_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
