"""server.py（本地报告台）的离线测试。

三块分开测：
1. **Markdown 渲染**：纯函数，报告格式是我们自己定的，测几个真实片段就够
2. **报告解析**：拿 `docs/samples/` 里的真实报告当固定输入（deterministic）
3. **HTTP 接口**：真的起一个服务（端口 0 让系统分配），用 requests 打过去

为什么第 3 块也要测：报告台的价值就是"能打开、能点"，接口签名一改
前端就白屏 —— 而这种问题只有真发一次请求才暴露得出来。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import requests

from selfheal import server

SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "samples"


# ---------------------------------------------------------------------------
# 1. Markdown 渲染
# ---------------------------------------------------------------------------
def test_render_headings_table_list_and_bold():
    md = """# 测试报告 · demo

- 结论：**✅ 全部通过**

## 概览

| 指标 | 数值 |
|---|---|
| 通过率 | 100.0% |

- 第一条
- 第二条

---

```python
print("hi")
```
"""
    html = server.render_markdown(md)

    assert "<h1>测试报告 · demo</h1>" in html
    assert "<h2>概览</h2>" in html
    assert "<strong>✅ 全部通过</strong>" in html
    assert "<table>" in html and "<th>指标</th>" in html and "<td>100.0%</td>" in html
    # 3 个 <li>：报告头部的 "- 结论：…" 也是一个列表项，加上两条正文列表
    assert html.count("<li>") == 3
    assert "<li>结论：<strong>✅ 全部通过</strong></li>" in html
    assert "<hr>" in html
    assert '<pre><code>print("hi")</code></pre>' in html


def test_render_escapes_html():
    """报告里有 `<failure>` 这类尖括号时不能被当成标签渲染。"""
    html = server.render_markdown("标签是 `<failure>`，不是 `failed`")
    assert "&lt;failure&gt;" in html
    assert "<failure>" not in html


# ---------------------------------------------------------------------------
# 2. 报告解析（用仓库里留档的真实报告）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "filename,expected_target,expected_passed,expected_total",
    [
        ("REPORT-rag-backend.md", "rag-backend", 3, 3),
        ("REPORT-java-cms.md", "java-cms", 3, 3),
    ],
)
def test_parse_real_report(filename, expected_target, expected_passed, expected_total):
    md = (SAMPLES / filename).read_text(encoding="utf-8")
    info = server.parse_report(md)

    assert info["target"] == expected_target
    assert info["passed"] == expected_passed
    assert info["total"] == expected_total
    assert info["pass_rate"] == 100.0
    assert info["defects"] == 0
    assert info["rounds"] == 1
    assert info["rounds_limit"] == 2
    assert "全部通过" in info["conclusion"]


def test_parse_report_counts_defect_rows():
    """缺陷表格有 N 行数据 → defects 必须是 N（不能把表头算进去）。"""
    md = """# 测试报告 · demo

- 结论：**⚠️ 发现服务端缺陷（已出缺陷清单，未改测试）**

## 概览

| 指标 | 数值 |
|---|---|
| 用例总数 | 2 |
| 通过 | 1 |
| 失败 / 错误 | 1 / 0 |
| 通过率 | 50.0% |
| 执行耗时 | 2.5s |

## 缺陷清单（系统 Bug：工具不改测试，需人工跟进）

| 用例 | 判定依据 |
|---|---|
| `test_x.py::test_a` | 命中规则 `500`：服务端 500 |

## 可修类失败（脚本 / 断言问题）

- 无
"""
    info = server.parse_report(md)

    assert info["total"] == 2
    assert info["passed"] == 1
    assert info["failed"] == 1
    assert info["pass_rate"] == 50.0
    assert info["defects"] == 1            # 只数数据行
    assert "缺陷" in info["conclusion"]


# ---------------------------------------------------------------------------
# 3. 目录穿越防护
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["../secrets.md", "sub/dir/report.md", "notes.txt", ""])
def test_load_report_rejects_bad_names(name):
    with pytest.raises(ValueError):
        server.load_report(name)


def test_load_report_returns_html(monkeypatch, ws_tmp):
    monkeypatch.setattr(server, "REPORT_DIR", ws_tmp)
    (ws_tmp / "REPORT-x.md").write_text("# 测试报告 · demo\n\n- 结论：**✅ 全部通过**\n", encoding="utf-8")

    got = server.load_report("REPORT-x.md")
    assert got["name"] == "REPORT-x.md"
    assert "<h1>测试报告 · demo</h1>" in got["html"]
    assert got["info"]["target"] == "demo"


def test_list_targets_reads_configs():
    names = {t["name"] for t in server.list_targets()}
    assert "java-cms" in names and "rag-backend" in names
    java = next(t for t in server.list_targets() if t["name"] == "java-cms")
    assert java["needs_auth"] is True
    assert java["endpoint_count"] >= 5


# ---------------------------------------------------------------------------
# 4. HTTP 接口（真起服务）
# ---------------------------------------------------------------------------
@pytest.fixture()
def live_server(monkeypatch, ws_tmp):
    """起一个只读服务：报告目录指向临时目录，避免依赖本机历史报告。"""
    monkeypatch.setattr(server, "REPORT_DIR", ws_tmp)
    (ws_tmp / "REPORT-demo.md").write_text(
        "# 测试报告 · demo\n\n- 结论：**✅ 全部通过**\n\n"
        "## 概览\n\n| 指标 | 数值 |\n|---|---|\n| 用例总数 | 1 |\n| 通过 | 1 |\n| 失败 / 错误 | 0 / 0 |\n| 通过率 | 100.0% |\n| 执行耗时 | 0.3s |\n",
        encoding="utf-8",
    )

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_api_reports_lists_files(live_server):
    data = requests.get(f"{live_server}/api/reports", timeout=5).json()
    assert [r["name"] for r in data["reports"]] == ["REPORT-demo.md"]
    assert data["reports"][0]["pass_rate"] == 100.0


def test_api_report_returns_html_and_markdown(live_server):
    data = requests.get(f"{live_server}/api/report", params={"name": "REPORT-demo.md"}, timeout=5).json()
    assert "<h1>测试报告 · demo</h1>" in data["html"]
    assert "结论" in data["markdown"]


def test_api_report_404_and_400(live_server):
    missing = requests.get(f"{live_server}/api/report", params={"name": "REPORT-none.md"}, timeout=5)
    assert missing.status_code == 404
    bad = requests.get(f"{live_server}/api/report", params={"name": "../x.md"}, timeout=5)
    assert bad.status_code == 400


def test_static_index_and_app_js(live_server):
    page = requests.get(f"{live_server}/", timeout=5)
    assert page.status_code == 200
    assert "selfheal 报告台" in page.text
    assert "/web/app.js" in page.text

    js = requests.get(f"{live_server}/web/app.js", timeout=5)
    assert js.status_code == 200
    assert "createApp" in js.text


def test_api_run_rejects_unknown_target(live_server):
    resp = requests.post(f"{live_server}/api/run", json={"target": "no-such-target"}, timeout=5)
    assert resp.status_code == 409
    assert "未知的 target" in resp.json()["error"]


def test_run_status_idle_initially(live_server):
    state = requests.get(f"{live_server}/api/run/status", timeout=5).json()
    assert state["status"] in ("idle", "done", "error")
