"""前端模板 ↔ app.js 一致性测试（防"模板引用了不存在的字段"导致白屏）。

为什么专门写这个：实测踩过 —— 模板里用了 `reqFiles`，但 app.js 没定义，
Vue 渲染抛错 → **整页白屏**，用户只会说"黑屏了"，排查要靠猜。
浏览器里的错误我这边看不到，所以用静态检查在最便宜的地方挡住它：

    把模板表达式里的 camelCase 标识符提取出来，要求它们都在 app.js 里出现过。

只查 camelCase（含大写字母）的标识符，避开 `length` / `map` / `join` 这类
JS 内置方法名 —— 那些本来就不该在 app.js 里定义。
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"

# 模板里的表达式位置：{{ ... }}、v-if/v-for/v-model/@click/:class 的属性值
_TEMPLATE_EXPR_RE = re.compile(
    r"\{\{(.*?)\}\}|(?:v-if|v-else-if|v-for|v-model[.\w]*|@click|:class|:disabled|:value)"
    r"""\s*=\s*"([^"]*)\"""",
    re.S,
)
_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")

# Vue 模板里允许出现、但不需要在 app.js 里定义的标识符
ALLOWED = {"Math", "JSON", "Object", "Number", "String", "Array"}


def _template_identifiers() -> set[str]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    found: set[str] = set()
    for match in _TEMPLATE_EXPR_RE.finditer(html):
        expr = match.group(1) or match.group(2) or ""
        found.update(_CAMEL_RE.findall(expr))
    return found - ALLOWED


def test_every_template_identifier_is_defined_in_app_js():
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    missing = sorted(name for name in _template_identifiers() if name not in app_js)

    assert not missing, (
        "模板里引用了 app.js 中不存在的标识符（会导致整页白屏）："
        f"{missing}\n请检查 web/index.html 与 web/app.js 是否对齐"
    )


def test_frontend_has_error_banner_instead_of_blank_page():
    app_js = (WEB / "app.js").read_text(encoding="utf-8")

    assert "showFatal" in app_js
    assert "errorHandler" in app_js
    assert "unhandledrejection" in app_js


def test_importer_block_references_existing_state():
    """导入面板用到的几个关键字段必须在 app.js 里落地。"""
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")

    for field in ("imp", "reqText", "reqFiles", "draftRows", "saveRequirements", "draftContract"):
        assert field in html, f"模板里应该用到 {field}"
        assert field in app_js, f"app.js 里必须定义 {field}"
