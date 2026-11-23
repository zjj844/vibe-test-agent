"""需求文档分节：Markdown 标题 与 中文编号标题 两种风格都要认。

真实场景：用户粘进来的 PRD 是这种风格（国内团队很常见）——

    SSM 企业级CMS内容管理系统 验收需求（FR）
    FR-1 登录返回 token 与权限码
    POST /api/auth/login 成功时 code=200 ...
    FR-2 注册默认 viewer
    ...

一条 `##` 都没有。只认 Markdown 标题的话，整篇会被当成一个巨大模块再按 300 字硬切，
一条需求被劈成两半，检索必然串味 —— 这正是本次要修的问题。
"""
from __future__ import annotations

from pathlib import Path

from selfheal import kb

FR_DOC = """SSM 企业级CMS内容管理系统 验收需求（FR）
验收口径：每条 FR 均为可测试断言。

FR-1 登录返回 token 与权限码
POST /api/auth/login 成功时 code=200，data 必须包含 token。

FR-2 注册默认 viewer
新用户必须默认绑定 viewer 角色。

FR-3 未登录返回 401
受保护接口必须返回 code=401。
"""

MD_DOC = """# 商品服务需求

## FR-1 商品列表

GET /api/items 返回 data 数组。

## FR-2 订单不存在返回 404

订单查不到时返回 code=404。
"""

CN_DOC = """一、背景
这是背景说明。

二、接口要求
GET /api/x 必须返回 200。
"""


def test_fr_style_headings_are_sections():
    sections = kb.RequirementKB._split_sections("prd", "PRD", FR_DOC)

    titles = [s.title for s in sections]
    assert any("FR-1" in t for t in titles)
    assert any("FR-3" in t for t in titles)
    # 每条 FR 自成一节，正文跟着自己的标题（不串到别的需求里）
    fr1 = next(s for s in sections if "FR-1" in s.title)
    assert "POST /api/auth/login" in fr1.text
    assert "FR-2" not in fr1.text


def test_preamble_before_first_heading_is_kept():
    sections = kb.RequirementKB._split_sections("prd", "PRD", FR_DOC)

    assert sections[0].title == "概述"
    assert "验收口径" in sections[0].text


def test_markdown_headings_still_win():
    """有 `##` 就优先信 Markdown，别把正文里的列表编号当标题。"""
    sections = kb.RequirementKB._split_sections("req", "REQ", MD_DOC)

    titles = [s.title for s in sections]
    assert titles == ["FR-1 商品列表", "FR-2 订单不存在返回 404"]


def test_chinese_numbered_headings_work():
    sections = kb.RequirementKB._split_sections("cn", "CN", CN_DOC)

    titles = [s.title for s in sections]
    assert any("背景" in t for t in titles)
    assert any("接口要求" in t for t in titles)


def test_plain_text_without_any_heading_is_single_section():
    sections = kb.RequirementKB._split_sections("plain", "PLAIN", "只有一段话，没有任何标题。")
    assert len(sections) == 1


def test_kb_indexes_user_prd_by_fr_sections(ws_tmp):
    """端到端：真把这种 PRD 放进目录，能建库并能检索到对应需求。"""
    docs = ws_tmp / "docs"
    docs.mkdir()
    (docs / "PRD.md").write_text(FR_DOC, encoding="utf-8")

    knowledge = kb.RequirementKB(docs_dir=docs)

    assert knowledge.doc_count == 1
    assert len(knowledge.sections) >= 4            # 概述 + 3 条 FR
    hits = knowledge.search("未登录必须返回什么状态码", top_k=3)
    assert hits
    assert any("401" in h["text"] or "FR-3" in h["text"] for h in hits)
