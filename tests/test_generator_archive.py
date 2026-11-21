"""补充测试：生成产物按批次隔离。

背景（实测踩到）：在终端敲 `pytest evals/generated` 会把**历史批次**也一起跑，
于是上一版带 bug 的文件报失败，看起来像"现在也不对"，其实早就修好了。
规则：`evals/generated/` 只放当前批次，历史挪进 `_archive/`（保留证据但不执行）。
"""
from __future__ import annotations

from selfheal import generator


def test_archive_previous_moves_old_files(ws_tmp):
    out = ws_tmp / "generated"
    out.mkdir()
    (out / "test_generated_20260101-000000-old.py").write_text("def test_old(): pass\n", encoding="utf-8")

    moved = generator.archive_previous(out)

    assert len(moved) == 1
    assert not (out / "test_generated_20260101-000000-old.py").exists()   # 不再在原目录
    assert (out / generator.ARCHIVE_DIR / "test_generated_20260101-000000-old.py").exists()


def test_archive_previous_noop_when_empty(ws_tmp):
    out = ws_tmp / "empty"
    out.mkdir()
    assert generator.archive_previous(out) == []


def test_save_leaves_only_current_batch(ws_tmp):
    """连续两次 save 之后，目录里只能剩一个可执行文件（最新那批）。"""
    target = generator.Target(
        name="fake", base_url="http://127.0.0.1:9",
        endpoints=(generator.Endpoint(id="health", method="GET", path="/api/health"),),
        health=generator.Endpoint(id="health", method="GET", path="/api/health"), timeout=5,
    )
    result = generator.GenerationResult(
        ok=True, code="def test_health():\n    endpoint, r = api('health')\n", case_count=1, endpoint_ids=["health"]
    )

    first = generator.save(result, target, out_dir=ws_tmp, tag="a")
    second = generator.save(result, target, out_dir=ws_tmp, tag="b")

    live = sorted(p.name for p in ws_tmp.glob("test_generated_*.py"))
    assert live == [second.name], f"目录里应只剩最新一批，实际 {live}"
    assert (ws_tmp / generator.ARCHIVE_DIR / first.name).exists()


def test_save_writes_conftest_that_ignores_archive(ws_tmp):
    """归档目录必须被 pytest 忽略，否则 `pytest evals/generated` 还是会跑历史批次。

    实测踩到：光把旧文件挪进 `_archive/` 没用——pytest 会**递归**收集子目录，
    于是上一版带 bug 的文件继续报红，看起来像"现在也不对"。
    """
    target = generator.Target(
        name="fake", base_url="http://127.0.0.1:9",
        endpoints=(generator.Endpoint(id="health", method="GET", path="/api/health"),),
        health=generator.Endpoint(id="health", method="GET", path="/api/health"), timeout=5,
    )
    result = generator.GenerationResult(
        ok=True, code="def test_health():\n    endpoint, r = api('health')\n", case_count=1, endpoint_ids=["health"]
    )
    generator.save(result, target, out_dir=ws_tmp, tag="c")

    conftest = ws_tmp / "conftest.py"
    assert conftest.exists(), "产物目录必须有 conftest 来阻止收集归档目录"
    assert "collect_ignore_glob" in conftest.read_text(encoding="utf-8")
