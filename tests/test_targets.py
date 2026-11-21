"""M1 targets.py 的离线测试。

为什么这层必须离线：
- 用 stdlib 的 http.server 起一个**假服务**，返回固定的状态码；
- 这样"探活成功/状态码不符/连接被拒"三种情况都能**确定性**复现；
- 不需要真的把 RAG 后端或 Java CMS 跑起来（否则测试会依赖环境，CI 就跑不了）。

这是"被测对象可替换"带来的好处：换掉 base_url 就能测另一个服务。
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from selfheal.targets import Endpoint, Target, TargetConfigError, load_target, probe


# ---------------------------------------------------------------------------
# 假服务：固定返回 status + JSON
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    status = 200
    payload: dict = {"status": "ok"}

    def _reply(self) -> None:  # noqa: D102
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args) -> None:  # 静音，别把测试输出刷屏
        pass


@pytest.fixture()
def fake_server(monkeypatch):
    """起一个随机端口的假服务，测试结束自动关。"""
    monkeypatch.setattr(_Handler, "status", 200)
    monkeypatch.setattr(_Handler, "payload", {"status": "ok", "doc_count": 8})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _target(base_url: str) -> Target:
    ep = Endpoint(id="health", method="GET", path="/api/health", expect_status=200)
    return Target(name="fake", base_url=base_url, endpoints=(ep,), health=ep, timeout=5)


def _closed_port() -> int:
    """拿一个"刚被关掉"的端口，用来确定性地制造连接被拒。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 读配置
# ---------------------------------------------------------------------------
def test_load_real_config():
    t = load_target("rag-backend")
    assert t.name == "rag-backend"
    assert t.base_url.startswith("http")
    assert len(t.endpoints) == 4
    assert t.health.id == "health"
    # 契约里的字段不能是空的——空字段等于没写
    assert all(ep.id and ep.path for ep in t.endpoints)


def test_endpoint_url_join_handles_slashes():
    ep = Endpoint(id="x", method="GET", path="/api/health")
    assert ep.url("http://h:8000") == "http://h:8000/api/health"
    assert ep.url("http://h:8000/") == "http://h:8000/api/health"


def test_load_missing_config_lists_available():
    with pytest.raises(TargetConfigError) as exc:
        load_target("no-such-target")
    assert "找不到 target 配置" in str(exc.value)
    assert "rag-backend" in str(exc.value)   # 错误信息要告诉人有哪些可选


def test_load_rejects_missing_field(ws_tmp):
    bad = ws_tmp / "bad.json"
    bad.write_text(json.dumps({"name": "x", "endpoints": []}), encoding="utf-8")
    with pytest.raises(TargetConfigError) as exc:
        load_target(bad)
    assert "base_url" in str(exc.value)


def test_load_rejects_empty_endpoints(ws_tmp):
    bad = ws_tmp / "empty.json"
    bad.write_text(json.dumps({"name": "x", "base_url": "http://h", "endpoints": []}), encoding="utf-8")
    with pytest.raises(TargetConfigError) as exc:
        load_target(bad)
    assert "没东西可测" in str(exc.value)


def test_load_tolerates_utf8_bom(ws_tmp):
    """Windows 上保存的 json 常带 BOM（文件开头 3 个不可见字节）。

    带 BOM 的文件用 encoding="utf-8" 读，json.loads 会报
    "Unexpected UTF-8 BOM" —— 这条测试锁定我们必须用 utf-8-sig 读。
    """
    good = ws_tmp / "bom.json"
    payload = {
        "name": "bom",
        "base_url": "http://127.0.0.1:1",
        "endpoints": [{"id": "h", "method": "GET", "path": "/h"}],
    }
    good.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

    t = load_target(good)
    assert t.name == "bom"
    assert len(t.endpoints) == 1


# ---------------------------------------------------------------------------
# 探活：三种结局都必须"只报告、不抛异常"
# ---------------------------------------------------------------------------
def test_probe_ok(fake_server):
    r = probe(_target(fake_server))
    assert r.ok is True
    assert r.status == 200
    assert "服务可达" in r.detail
    assert r.latency_ms >= 0


def test_probe_status_mismatch(fake_server, monkeypatch):
    monkeypatch.setattr(_Handler, "status", 500)
    r = probe(_target(fake_server))
    assert r.ok is False
    assert r.status == 500
    assert "状态码不符" in r.detail


def test_probe_connection_refused_does_not_raise():
    """这是最关键的一条：服务没起时必须返回 ok=False，而不是让程序崩。"""
    r = probe(_target(f"http://127.0.0.1:{_closed_port()}"), retries=1)
    assert r.ok is False
    assert r.status is None
    assert "连接被拒" in r.detail
    assert "服务没启动" in r.detail
