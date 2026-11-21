"""snapshot.py + report.py 的离线测试。

为什么不真跑循环：这两个模块的输入都是**数据结构**。
只要我能构造出一个"像那么回事"的循环结果，就能验证渲染对不对 ——
不需要模型、不需要服务。这就是"把编排写成纯代码"的直接好处：
**除了那两个模型调用点，其他全都能单测。**
"""
from __future__ import annotations

import json

import pytest

from selfheal import report, runner, snapshot
from selfheal.loop import CycleResult, RoundResult
from selfheal.triage import Action, FailureKind, TriageReport, Verdict


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def snap_root(ws_tmp, monkeypatch):
    """把快照仓库改到临时目录，避免污染仓库根的 .snapshots/。"""
    root = ws_tmp / "snaps"
    monkeypatch.setattr(snapshot, "SNAPSHOT_ROOT", root)
    return root


def _case(name: str, status: str, message: str = "") -> runner.CaseResult:
    return runner.CaseResult(
        nodeid=f"test_generated_x.py::{name}",
        name=name,
        status=status,
        duration=0.12,
        message=message,
    )


def _run(cases: list[runner.CaseResult], error: str = "") -> runner.RunResult:
    return runner.RunResult(
        returncode=0 if all(c.status == "passed" for c in cases) else 1,
        cases=tuple(cases),
        xml_path=None,
        duration=1.5,
        error=error,
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------
def test_take_copies_files_and_writes_manifest(snap_root, ws_tmp):
    f = ws_tmp / "test_a.py"
    f.write_text("def test_a():\n    assert True\n", encoding="utf-8")

    snap = snapshot.take([f], label="round1", reason="第 1 轮产物")

    assert snap.dir.exists()
    assert snap.label == "round1"
    assert len(snap.files) == 1
    manifest = json.loads((snap.dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reason"] == "第 1 轮产物"
    entry = manifest["files"][0]
    assert entry["path"].endswith("test_a.py")
    assert len(entry["sha256"]) == 16      # 截断的 sha256，够做指纹
    # 备份出来的文件内容和原文件一致
    copied = snap.dir / entry["path"]
    assert copied.read_text(encoding="utf-8") == f.read_text(encoding="utf-8")


def test_take_skips_missing_paths(snap_root, ws_tmp):
    """不存在的路径不该让快照失败 —— 只如实记录（files 为空）。"""
    snap = snapshot.take([ws_tmp / "nope.py"], label="empty")
    assert snap.files == ()


def test_list_and_latest(snap_root, ws_tmp):
    f = ws_tmp / "test_b.py"
    f.write_text("x = 1\n", encoding="utf-8")
    first = snapshot.take([f], label="first")
    second = snapshot.take([f], label="second")

    listed = snapshot.list_snapshots()
    assert len(listed) == 2
    assert {s.label for s in listed} == {"first", "second"}

    latest = snapshot.latest()
    assert latest is not None
    # 目录名带时间戳，排序后最后一个是"最新"（同秒时靠 label 兜底，这里只断言不炸）
    assert latest.label in {"first", "second"}
    assert {first.dir.name, second.dir.name} <= {s.dir.name for s in listed}


def test_restore_puts_content_back(snap_root, ws_tmp):
    """回滚点真的能回滚：改动文件后 restore，内容必须回到快照那一刻。"""
    f = ws_tmp / "test_c.py"
    original = "def test_c():\n    assert 1 == 1\n"
    f.write_text(original, encoding="utf-8")
    snap = snapshot.take([f], label="good")

    f.write_text("def test_c():\n    assert 1 == 2  # 被改坏了\n", encoding="utf-8")
    restored = snapshot.restore(snap)

    assert len(restored) == 1
    assert f.read_text(encoding="utf-8") == original


def test_load_reads_manifest_back(snap_root, ws_tmp):
    f = ws_tmp / "test_d.py"
    f.write_text("x = 2\n", encoding="utf-8")
    snap = snapshot.take([f], label="loadme", reason="为了测试 load")

    again = snapshot.load(snap.dir)
    assert again.label == "loadme"
    assert again.reason == "为了测试 load"
    assert again.files == snap.files


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _cycle(status: str, rounds: list[RoundResult]) -> CycleResult:
    c = CycleResult(
        target_name="rag-backend",
        started_at="2026-11-21 10:00:00",
        finished_at="2026-11-21 10:00:30",
        status=status,
        max_rounds=2,
        rounds=rounds,
    )
    return c


def test_build_report_contains_all_key_sections():
    run = _run([_case("test_health", "passed"), _case("test_ask", "failed", "500 != 200")])
    t = TriageReport(
        verdicts=(
            Verdict(
                nodeid="test_generated_x.py::test_ask",
                kind=FailureKind.SYSTEM_BUG,
                action=Action.REPORT_DEFECT,
                reason="/api/ask 返回 500",
                matched="500",
            ),
        ),
        passed=1,
    )
    r = RoundResult(index=1, run=run, triage=t, generated_file=None,
                    note="发现 1 条系统缺陷 → 不改测试")
    text = report.build(_cycle("defect", [r]))

    assert "# 测试报告 · rag-backend" in text
    assert "## 概览" in text
    assert "| 通过率 | 50.0% |" in text
    assert "## 缺陷清单" in text
    assert "`test_generated_x.py::test_ask`" in text
    assert "系统Bug" in text
    assert "## 每轮明细" in text
    assert "### 第 1 轮" in text


def test_build_report_says_no_defect_when_clean():
    run = _run([_case("test_health", "passed")])
    r = RoundResult(index=1, run=run, note="全部通过")
    text = report.build(_cycle("success", [r]))

    assert "✅ 全部通过" in text
    assert "本次没有发现服务端缺陷" in text


def test_write_report_creates_file(ws_tmp):
    run = _run([_case("test_health", "passed")])
    r = RoundResult(index=1, run=run, note="全部通过")
    path = report.write(_cycle("success", [r]), out_dir=ws_tmp / "reports")

    assert path.exists()
    assert path.name.startswith("REPORT-")
    assert path.name.endswith("-rag-backend.md")
    assert "测试报告" in path.read_text(encoding="utf-8")
