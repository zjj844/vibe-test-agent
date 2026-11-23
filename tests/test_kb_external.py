"""知识库升级：需求文档 + 外部规范一起检索（hybrid）。

为什么单独测这一组：
接外部规范是"让 RAG 真的进生成链路"的关键一步，而它有几个容易错的点：
  1. 没给 external_dir 时**行为必须完全不变**（老路径不能被改坏）
  2. 给了 external_dir 时，外部规范的块必须真的能被检索到（doc_id 带「规范·」前缀）
  3. 持久化索引重建不能重复叠加块（rebuild 幂等）
"""
from __future__ import annotations

from pathlib import Path

from selfheal import kb

PRD = """SSM CMS 验收需求
验收口径：每条 FR 都是可测断言。

FR-1 登录返回 token
POST /api/auth/login 成功时 code=200，data 必须包含 token。

FR-2 未登录返回 401
受保护接口必须返回 code=401。
"""

STD_LOG = """# 日志规约

## 日志保存时长

日志文件至少保存 15 天。

## 禁止 System.out

禁止直接使用 System.out 打印日志。
"""

STD_DB = """# 建表规约

## 三字段

表必备三字段：id、gmt_create、gmt_modified。
"""


def _make_dirs(ws_tmp: Path) -> tuple[Path, Path]:
    req = ws_tmp / "req"
    ext = ws_tmp / "ext"
    req.mkdir()
    ext.mkdir()
    (req / "PRD.md").write_text(PRD, encoding="utf-8")
    (ext / "日志规约.md").write_text(STD_LOG, encoding="utf-8")
    (ext / "建表规约.md").write_text(STD_DB, encoding="utf-8")
    return req, ext


def test_without_external_dir_behaviour_unchanged(ws_tmp):
    """老路径：不传 external_dir → 内存检索，无索引。"""
    req, _ext = _make_dirs(ws_tmp)

    knowledge = kb.RequirementKB(docs_dir=req)

    assert knowledge.index is None
    assert knowledge.external_chunks == 0
    assert knowledge.index_stats()["mode"] == "memory"
    assert knowledge.search("未登录返回什么", top_k=2)


def test_with_external_dir_builds_persistent_hybrid_index(ws_tmp):
    req, ext = _make_dirs(ws_tmp)

    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3"
    )
    stats = knowledge.index_stats()

    assert knowledge.index is not None
    assert stats["mode"] == "hybrid"
    assert stats["external_chunks"] > 0
    # 索引里同时有需求块和规范块
    assert stats["chunks"] == knowledge.chunk_count > stats["external_chunks"]


def test_external_rules_are_retrievable(ws_tmp):
    """关键：生成链路要能检索到"组织规范"，而不只是本项目需求。"""
    req, ext = _make_dirs(ws_tmp)
    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3"
    )

    hits = knowledge.search("日志要保存多少天", top_k=3)

    assert hits
    assert any(h["doc_id"].startswith("规范·") for h in hits), [h["doc_id"] for h in hits]
    assert any("15 天" in h["text"] for h in hits)


def test_requirement_and_external_both_present_in_tree(ws_tmp):
    req, ext = _make_dirs(ws_tmp)
    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3"
    )

    tree = knowledge.tree()

    assert "📄" in tree and "📚" in tree
    assert "FR-1" in tree
    assert "三字段" in tree or "日志保存时长" in tree


def test_rebuild_is_idempotent_across_instances(ws_tmp):
    """同一个索引文件重复建库，块数不能翻倍。"""
    req, ext = _make_dirs(ws_tmp)
    db = ws_tmp / "kb.sqlite3"

    first = kb.RequirementKB(docs_dir=req, external_dir=ext, index_db=db)
    count1 = first.chunk_count
    second = kb.RequirementKB(docs_dir=req, external_dir=ext, index_db=db)

    assert second.chunk_count == count1


def test_external_corpus_in_subdirectories_is_loaded(ws_tmp):
    """外部语料按来源分层存放（corpus_external/alibaba-p3c/…），必须递归读到。"""
    req, ext = _make_dirs(ws_tmp)
    nested = ext / "alibaba-p3c"
    nested.mkdir()
    (ext / "日志规约.md").unlink()                    # 换成子目录里的同名文件
    (nested / "日志规约.md").write_text(STD_LOG, encoding="utf-8")

    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3"
    )
    stats = knowledge.index_stats()

    assert stats["external_chunks"] > 0
    assert any(s.doc_id.startswith("规范·alibaba-p3c/") for s in knowledge.sections)


def test_requirements_dir_is_not_recursive(ws_tmp):
    """需求目录不递归：避免把别的项目的需求误收进来。"""
    req, ext = _make_dirs(ws_tmp)
    other = req / "another-project"
    other.mkdir()
    (other / "OTHER.md").write_text("## 别的项目需求\n不应该被收进来\n", encoding="utf-8")

    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3"
    )

    assert all(not s.doc_id.startswith("another-project") for s in knowledge.sections)


def test_bm25_mode_also_works(ws_tmp):
    req, ext = _make_dirs(ws_tmp)
    knowledge = kb.RequirementKB(
        docs_dir=req, external_dir=ext, index_db=ws_tmp / "kb.sqlite3", mode="bm25"
    )

    hits = knowledge.search("gmt_create", top_k=2)
    assert hits
    assert any("三字段" in h["text"] for h in hits)
