"""契约接口（server 的 /api/contract*）与前端资源的测试。

为何单独测：
- 起草会真的调模型 → 测试里必须换成替身（否则 CI 联网、花钱、还不稳定）
- 保存会写 configs/ → 测试里必须把 CONFIG_DIR 指到临时目录（否则污染仓库）
这两条都属于"测试跑完不能留下痕迹"的纪律。
"""
from __future__ import annotations

import json
import threading

import pytest
import requests

from selfheal import bootstrap, server

CONTRACT = {
    "name": "my-app",
    "base_url": "http://127.0.0.1:9000",
    "health": {"id": "items"},
    "endpoints": [
        {"id": "items", "method": "GET", "path": "/api/items",
         "expect_status": 200, "expect_fields": ["code"], "notes": "列表"},
    ],
}


@pytest.fixture()
def live(monkeypatch, ws_tmp):
    """起真服务；CONFIG_DIR 指向临时目录，起草用替身。"""
    monkeypatch.setattr(server, "CONFIG_DIR", ws_tmp / "configs")
    (ws_tmp / "configs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        bootstrap.llm_client, "chat_for",
        lambda *a, **k: json.dumps(CONTRACT, ensure_ascii=False),
    )

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def _payload(**over):
    data = {"name": "my-app", "base_url": "http://127.0.0.1:9000",
            "files": [{"path": "A.java", "content": "@GetMapping(\"/api/items\")"}]}
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------
def test_api_draft_returns_contract(live):
    resp = requests.post(f"{live}/api/contract/draft", json=_payload(), timeout=30)
    body = resp.json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["endpoint_count"] == 1
    assert body["contract"]["name"] == "my-app"


def test_api_draft_rejects_empty_files(live):
    resp = requests.post(f"{live}/api/contract/draft", json=_payload(files=[]), timeout=30)
    assert resp.status_code == 400
    assert "源码" in resp.json()["error"]


def test_api_draft_surfaces_violations(live, monkeypatch):
    monkeypatch.setattr(
        bootstrap.llm_client, "chat_for",
        lambda *a, **k: json.dumps({"name": "x", "base_url": "http://x", "endpoints": []}),
    )
    resp = requests.post(f"{live}/api/contract/draft", json=_payload(), timeout=60)
    body = resp.json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["violations"]


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------
def test_api_save_then_load_roundtrip(live):
    saved = requests.post(f"{live}/api/contract/save",
                          json={"contract": CONTRACT}, timeout=10).json()
    assert saved["ok"] is True
    assert saved["name"] == "my-app"

    got = requests.get(f"{live}/api/contract", params={"name": "my-app"}, timeout=10).json()
    assert got["contract"]["endpoints"][0]["path"] == "/api/items"


def test_api_save_refuses_overwrite_without_force(live):
    requests.post(f"{live}/api/contract/save", json={"contract": CONTRACT}, timeout=10)

    again = requests.post(f"{live}/api/contract/save",
                          json={"contract": CONTRACT}, timeout=10).json()
    assert again["ok"] is False
    assert again.get("exists") is True

    forced = requests.post(f"{live}/api/contract/save",
                           json={"contract": CONTRACT, "force": True}, timeout=10).json()
    assert forced["ok"] is True


def test_api_save_rejects_invalid_contract(live):
    bad = requests.post(f"{live}/api/contract/save",
                        json={"contract": {"name": "x", "base_url": "http://x"}}, timeout=10)
    assert bad.status_code == 400
    assert "不合格" in bad.json()["error"]


def test_api_load_contract_404_and_400(live):
    assert requests.get(f"{live}/api/contract", params={"name": "nope"}, timeout=10).status_code == 404
    assert requests.get(f"{live}/api/contract", params={"name": "../x"}, timeout=10).status_code == 400


def test_saved_contract_shows_up_in_targets(live):
    requests.post(f"{live}/api/contract/save", json={"contract": CONTRACT}, timeout=10)
    names = [t["name"] for t in requests.get(f"{live}/api/targets", timeout=10).json()["targets"]]
    assert "my-app" in names


# ---------------------------------------------------------------------------
# 前端资源里确实有给导入面板用的钩子
# ---------------------------------------------------------------------------
def test_frontend_has_importer_wiring(live):
    html = requests.get(f"{live}/", timeout=10).text
    js = requests.get(f"{live}/web/app.js", timeout=10).text

    assert "导入新项目" in html
    assert "draftContract" in html and "draftContract" in js
    assert "/api/contract/draft" in js
    assert "FileReader" in js          # 浏览器读文件内容，服务端不碰文件系统
