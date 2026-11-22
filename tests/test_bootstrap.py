"""契约引导生成的离线测试（bootstrap.py）。

三块分开测：
1. **输入整理**：多文件拼接、截断、空内容
2. **起草 + 校验**：模型输出合法/不合法时的行为（模型用替身，不联网）
3. **落盘**：拒绝覆盖、拒绝不合格契约、正常写入

最要紧的一条：`validate_contract()` 用的是 `targets.parse_target()` —— 
测试里专门验证"加载器拒绝的东西，草案校验也拒绝"，防止两份规则漂移。
"""
from __future__ import annotations

import json

import pytest

from selfheal import bootstrap, targets


CONTRACT_OK = {
    "name": "my-app",
    "base_url": "http://127.0.0.1:9000",
    "health": {"id": "items"},
    "endpoints": [
        {
            "id": "items",
            "method": "GET",
            "path": "/api/items",
            "expect_status": 200,
            "expect_fields": ["code", "data"],
            "notes": "商品列表，需登录",
        }
    ],
}


def _stub_llm(monkeypatch, reply: str):
    monkeypatch.setattr(bootstrap.llm_client, "chat_for", lambda *a, **k: reply)


# ---------------------------------------------------------------------------
# 1. 输入整理
# ---------------------------------------------------------------------------
def test_merge_sources_keeps_filenames():
    text = bootstrap.merge_sources([
        {"path": "A.java", "content": "@GetMapping(\"/a\")"},
        {"path": "B.java", "content": "@PostMapping(\"/b\")"},
    ])
    assert "===== 文件：A.java =====" in text
    assert "===== 文件：B.java =====" in text
    assert "/a" in text and "/b" in text


def test_merge_sources_skips_empty_and_truncates():
    assert bootstrap.merge_sources([{"path": "x.java", "content": "   "}]) == ""

    big = bootstrap.merge_sources([{"path": "big.java", "content": "x" * (bootstrap.MAX_SOURCE_CHARS + 500)}])
    assert "被截断" in big
    assert len(big) < bootstrap.MAX_SOURCE_CHARS + 10_000


# ---------------------------------------------------------------------------
# 2. 起草
# ---------------------------------------------------------------------------
def test_draft_accepts_valid_contract(monkeypatch):
    _stub_llm(monkeypatch, "```json\n" + json.dumps(CONTRACT_OK) + "\n```")

    result = bootstrap.draft("my-app", "http://127.0.0.1:9000",
                             [{"path": "A.java", "content": "@GetMapping(\"/api/items\")"}])

    assert result.ok is True
    assert result.endpoint_count == 1
    assert result.repaired is False
    assert result.contract["name"] == "my-app"
    assert result.contract["endpoints"][0]["method"] == "GET"


def test_draft_forces_name_and_base_url(monkeypatch):
    """模型抄错 name/base_url 时，必须以人给的为准。"""
    wrong = dict(CONTRACT_OK, name="模型瞎起的名字", base_url="http://wrong:1")
    _stub_llm(monkeypatch, json.dumps(wrong, ensure_ascii=False))

    result = bootstrap.draft("real-app", "http://127.0.0.1:9000",
                             [{"path": "A.java", "content": "x"}])

    assert result.contract["name"] == "real-app"
    assert result.contract["base_url"] == "http://127.0.0.1:9000"


def test_draft_normalizes_sloppy_ids(monkeypatch):
    """id 里有大写/空格/中文 → 机械修成合法 id（语义不动）。"""
    sloppy = dict(CONTRACT_OK)
    sloppy["endpoints"] = [dict(CONTRACT_OK["endpoints"][0], id="Order Detail")]
    _stub_llm(monkeypatch, json.dumps(sloppy, ensure_ascii=False))

    result = bootstrap.draft("my-app", "http://127.0.0.1:9000", [{"path": "A.java", "content": "x"}])

    assert result.ok is True
    assert result.contract["endpoints"][0]["id"] == "order-detail"


def test_draft_retries_once_then_fails(monkeypatch):
    """两次都缺 endpoints → 放弃（不落盘、不执行）。"""
    bad = json.dumps({"name": "x", "base_url": "http://x", "endpoints": []})
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return bad

    monkeypatch.setattr(bootstrap.llm_client, "chat_for", fake)
    result = bootstrap.draft("my-app", "http://127.0.0.1:9000", [{"path": "A.java", "content": "x"}])

    assert result.ok is False
    assert calls["n"] == 2                      # 第一次 + 重试一次
    assert result.repaired is True
    assert any("endpoints" in v for v in result.violations)


def test_draft_reports_bad_json(monkeypatch):
    _stub_llm(monkeypatch, "我觉得这几个接口都要测")
    result = bootstrap.draft("my-app", "http://127.0.0.1:9000", [{"path": "A.java", "content": "x"}])

    assert result.ok is False
    assert any("没给出合法 JSON" in v for v in result.violations)


def test_draft_rejects_bad_name_and_url(monkeypatch):
    _stub_llm(monkeypatch, json.dumps(CONTRACT_OK))

    assert bootstrap.draft("中文名", "http://x", [{"path": "a", "content": "x"}]).ok is False
    assert bootstrap.draft("ok-name", "127.0.0.1:9000", [{"path": "a", "content": "x"}]).ok is False
    assert bootstrap.draft("ok-name", "http://x", []).ok is False


def test_draft_swallows_model_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("连不上模型")

    monkeypatch.setattr(bootstrap.llm_client, "chat_for", boom)
    result = bootstrap.draft("my-app", "http://127.0.0.1:9000", [{"path": "A.java", "content": "x"}])

    assert result.ok is False
    assert any("模型调用失败" in v for v in result.violations)


# ---------------------------------------------------------------------------
# 3. 校验与落盘
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_contract,expect_fragment",
    [
        ({"name": "x", "base_url": "http://x"}, "endpoints"),
        ({"name": "x", "base_url": "http://x", "endpoints": []}, "endpoints"),
        ({"name": "x", "base_url": "http://x", "endpoints": [
            {"id": "dup", "path": "/a"}, {"id": "dup", "path": "/b"}]}, "重复"),
        ({"name": "x", "base_url": "http://x",
          "auth": {"endpoint": "nope"},
          "endpoints": [{"id": "a", "path": "/a"}]}, "auth.endpoint"),
    ],
)
def test_validate_contract_uses_the_same_rules_as_the_loader(bad_contract, expect_fragment):
    """加载器拒绝的东西，草案校验也必须拒绝 —— 一份规则，两处使用。"""
    with pytest.raises(targets.TargetConfigError) as loader_exc:
        targets.parse_target(dict(bad_contract))

    ok, problems = bootstrap.validate_contract(dict(bad_contract))

    assert ok is False
    assert problems, "加载器报错时，草案校验也必须报错"
    assert expect_fragment in str(loader_exc.value)


def test_save_contract_writes_pretty_json(ws_tmp):
    path = bootstrap.save_contract(CONTRACT_OK, config_dir=ws_tmp)

    assert path.name == "my-app.json"
    text = path.read_text(encoding="utf-8")
    assert '"name": "my-app"' in text          # 缩进过、可读
    assert "我的" not in text                   # 中文没被转义成 \u
    assert json.loads(text)["name"] == "my-app"


def test_save_contract_refuses_overwrite(ws_tmp):
    bootstrap.save_contract(CONTRACT_OK, config_dir=ws_tmp)

    with pytest.raises(FileExistsError):
        bootstrap.save_contract(CONTRACT_OK, config_dir=ws_tmp)

    # 显式 force 才允许
    again = bootstrap.save_contract(CONTRACT_OK, config_dir=ws_tmp, force=True)
    assert again.exists()


def test_save_contract_rejects_invalid(ws_tmp):
    with pytest.raises(targets.TargetConfigError):
        bootstrap.save_contract({"name": "x", "base_url": "http://x"}, config_dir=ws_tmp)


def test_saved_contract_can_be_loaded_by_loader(ws_tmp):
    """闭环：起草 → 保存 → 用真实加载器读回来（并且能探活准备）。"""
    path = bootstrap.save_contract(CONTRACT_OK, config_dir=ws_tmp)

    target = targets.load_target(path)

    assert target.name == "my-app"
    assert len(target.endpoints) == 1
    assert target.health.id == "items"
