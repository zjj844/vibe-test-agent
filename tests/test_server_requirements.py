"""需求文档接口（/api/requirements*）的测试。

重点：
- 保存后**契约要自动补上 requirements_dir**（否则检索仍走默认目录 → 喂错语料，这是踩过的坑）
- 文件名/target 走白名单（防目录穿越）
- 同名不覆盖（除非 force）
"""
from __future__ import annotations

import json
import threading

import pytest
import requests

from selfheal import server

REQ_TEXT = """# REQ-001 商品接口

## FR-1 商品列表

GET /api/items 返回 data 数组；空时必须为 []，不得为 null。

## FR-2 订单不存在返回 404

订单查不到时返回 code=404，不得 500。
"""


@pytest.fixture()
def live(monkeypatch, ws_tmp):
    monkeypatch.setattr(server, "CONFIG_DIR", ws_tmp / "configs")
    monkeypatch.setattr(server, "REQ_DIR", ws_tmp / "requirements_docs")
    (ws_tmp / "configs").mkdir(parents=True, exist_ok=True)

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _save(base, **over):
    payload = {"target": "my-app", "text": REQ_TEXT}
    payload.update(over)
    return requests.post(f"{base}/api/requirements/save", json=payload, timeout=10)


def test_save_creates_markdown_and_counts_sections(live):
    body = _save(live).json()

    assert body["ok"] is True
    assert body["file"] == "REQ-001-需求.md"          # 自动编号
    assert body["sections"] == 2                       # 两个 ## 小节
    assert body["chars"] == len(REQ_TEXT.strip())


def test_save_patches_contract_with_requirements_dir(live, ws_tmp):
    """保存需求后，契约必须自动指向该目录 —— 否则检索会喂错语料。"""
    contract = {
        "name": "my-app", "base_url": "http://127.0.0.1:9000",
        "endpoints": [{"id": "items", "path": "/api/items"}],
    }
    (ws_tmp / "configs" / "my-app.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8"
    )

    body = _save(live).json()

    assert "requirements_dir" in body["contract"]
    saved = json.loads((ws_tmp / "configs" / "my-app.json").read_text(encoding="utf-8"))
    assert saved["requirements_dir"] == "selfheal/requirements_docs/my-app"


def test_save_without_contract_reports_it(live):
    body = _save(live).json()
    assert body["ok"] is True
    assert "契约不存在" in body["contract"]


def test_second_save_gets_next_number(live):
    _save(live)
    body = _save(live, text="## FR-9 另一个需求\n内容").json()
    assert body["file"] == "REQ-002-需求.md"


def test_explicit_filename_and_no_overwrite(live):
    first = _save(live, filename="REQ-100-测试.md").json()
    assert first["file"] == "REQ-100-测试.md"

    again = _save(live, filename="REQ-100-测试.md").json()
    assert again["ok"] is False and again.get("exists") is True

    forced = _save(live, filename="REQ-100-测试.md", force=True).json()
    assert forced["ok"] is True


@pytest.mark.parametrize("bad_target", ["../etc", "My App", "", "中文项目", "a/b"])
def test_bad_target_rejected(live, bad_target):
    resp = _save(live, target=bad_target)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_empty_text_rejected(live):
    resp = _save(live, text="   ")
    assert resp.status_code == 400


def test_bad_filename_rejected(live):
    resp = _save(live, filename="../evil.md")
    assert resp.status_code == 400


def test_api_lists_requirements(live):
    _save(live, filename="REQ-001-a.md")
    _save(live, filename="REQ-002-b.md", text="## FR-1 x\ny")

    data = requests.get(f"{live}/api/requirements",
                        params={"target": "my-app"}, timeout=10).json()

    assert [f["name"] for f in data["files"]] == ["REQ-001-a.md", "REQ-002-b.md"]
    assert data["files"][0]["sections"] == 2
    assert data["files"][1]["sections"] == 1


def test_api_lists_empty_for_unknown_target(live):
    data = requests.get(f"{live}/api/requirements",
                        params={"target": "nobody"}, timeout=10).json()
    assert data["files"] == []


def test_frontend_has_requirements_wiring(live):
    html = requests.get(f"{live}/", timeout=10).text
    js = requests.get(f"{live}/web/app.js", timeout=10).text

    assert "需求文档" in html
    assert "saveRequirements" in html and "saveRequirements" in js
    assert "/api/requirements/save" in js
