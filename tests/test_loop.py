"""loop.py 的离线测试 —— 专门验证"决定逻辑"。

重点：**这一层不产生任何真实调用**。模型、HTTP、pytest 全被替身替换，
所以我们能精确地构造"第 1 轮可修失败、第 2 轮通过"这种剧本，
断言"它到底做了几个决定、顺序对不对"。

为什么这很重要（面试点）：自愈系统最怕的不是写错代码，而是**决策写错**——
比如把系统缺陷当成脚本问题去改测试，或者明明服务挂了还在那儿重试 5 轮。
这些都能用几十毫秒的单测锁死。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from selfheal import critic, loop, runner, snapshot, targets
from selfheal.generator import GenerationResult
from selfheal.triage import Action, FailureKind, TriageReport, Verdict


# ---------------------------------------------------------------------------
# 替身（fake）构造器
# ---------------------------------------------------------------------------
class FakeKB:
    doc_count = 1
    chunk_count = 3

    def search(self, query, top_k=3):
        return [{"chunk_id": "REQ-001#s00", "text": "接口应返回 answer 字段"}]


def _target() -> targets.Target:
    ep = targets.Endpoint(
        id="health", method="GET", path="/api/health", expect_status=200, notes="探活接口"
    )
    return targets.Target(
        name="fake-app", base_url="http://127.0.0.1:9", endpoints=(ep,), health=ep
    )


def _case(name: str, status: str, message: str = "") -> runner.CaseResult:
    return runner.CaseResult(nodeid=f"test_gen.py::{name}", name=name, status=status,
                             duration=0.1, message=message)


def _run(cases=(), error: str = "") -> runner.RunResult:
    return runner.RunResult(
        returncode=1, cases=tuple(cases), xml_path=None, duration=1.0, error=error
    )


@dataclass
class Step:
    """一轮的剧本：执行结果 + 该结果应有的分流结论。"""

    run: runner.RunResult
    verdicts: tuple[Verdict, ...] = ()


def _v(name: str, kind: FailureKind, action: Action, reason: str = "…") -> Verdict:
    return Verdict(nodeid=f"test_gen.py::{name}", kind=kind, action=action,
                   reason=reason, matched=kind.value)


class Script:
    """按剧本返回每一轮结果，并记录调用痕迹。"""

    def __init__(self, steps: list[Step]):
        self.steps = list(steps)
        self.run_calls = 0
        self.gen_calls = 0
        self.feedback_seen: list[str] = []

    # ---- 安装替身 ----
    def install(self, monkeypatch, ws_tmp, *, probe_ok=True, gen_ok=True,
                violations=(), critic_raises=False, snap_raises=False) -> "Script":
        monkeypatch.setattr(loop.targets, "load_target", lambda name: _target())
        monkeypatch.setattr(
            loop.targets, "probe",
            lambda target, **kw: targets.ProbeResult(
                ok=probe_ok, status=200 if probe_ok else None,
                detail="服务可达" if probe_ok else "连接被拒", latency_ms=1.0,
            ),
        )
        monkeypatch.setattr(loop.kb, "RequirementKB", lambda *a, **k: FakeKB())

        def fake_generate(target, requirements=None, n_cases=3, max_repair=1, feedback=""):
            self.gen_calls += 1
            self.feedback_seen.append(feedback)
            return GenerationResult(
                ok=gen_ok,
                code="def test_health():\n    assert api('health').status == 200\n",
                case_count=1 if gen_ok else 0,
                violations=list(violations),
                endpoint_ids=["health"] if gen_ok else [],
            )

        def fake_save(result, target, out_dir, tag=""):
            path = ws_tmp / f"test_generated_{tag or 'x'}.py"
            path.write_text(result.code, encoding="utf-8")
            return path

        monkeypatch.setattr(loop.generator, "generate", fake_generate)
        monkeypatch.setattr(loop.generator, "save", fake_save)

        if critic_raises:
            def boom(code, samples=2):
                raise RuntimeError("评分服务挂了")
            monkeypatch.setattr(loop.critic, "score", boom)
        else:
            monkeypatch.setattr(
                loop.critic, "score",
                lambda code, samples=2: critic.ScoreResult(
                    scores={"可执行性": 4.0}, total=4.0, samples=2, comment="还行"
                ),
            )

        if snap_raises:
            def bad_take(*a, **k):
                raise OSError("磁盘满了")
            monkeypatch.setattr(loop.snapshot, "take", bad_take)
        else:
            monkeypatch.setattr(
                loop.snapshot, "take",
                lambda paths, label="", reason="": snapshot.Snapshot(
                    dir=ws_tmp / "snaps", label=label or "auto", reason=reason,
                    created_at="2026-11-21 10:00:00", files=tuple(str(p) for p in paths),
                ),
            )

        monkeypatch.setattr(loop.runner, "run_tests", self._run_tests)
        monkeypatch.setattr(loop.triage, "classify_run", self._classify)
        return self

    # ---- 被替换掉的真函数 ----
    def _current(self) -> Step:
        return self.steps[min(self.run_calls, len(self.steps) - 1)]

    def _run_tests(self, path, timeout=900, report_dir=None) -> runner.RunResult:
        step = self._current()
        self.run_calls += 1
        return step.run

    def _classify(self, run) -> TriageReport:
        # 注意：run_calls 已经 +1 了，所以这里要回退一格
        step = self.steps[min(max(self.run_calls - 1, 0), len(self.steps) - 1)]
        return TriageReport(verdicts=step.verdicts, passed=step.run.passed,
                            error=step.run.error)


GREEN = Step(_run([_case("test_health", "passed")]))


# ---------------------------------------------------------------------------
# 剧本 1：服务不可达 → 立即停，绝不生成
# ---------------------------------------------------------------------------
def test_unreachable_stops_before_generation(monkeypatch, ws_tmp):
    script = Script([]).install(monkeypatch, ws_tmp, probe_ok=False)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.UNREACHABLE
    assert len(cycle.rounds) == 1
    assert script.gen_calls == 0          # 没生成
    assert script.run_calls == 0          # 也没执行
    assert "服务不可达" in cycle.rounds[0].note


# ---------------------------------------------------------------------------
# 剧本 2：第一轮就全绿
# ---------------------------------------------------------------------------
def test_success_in_first_round(monkeypatch, ws_tmp):
    script = Script([GREEN]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, write_report=False)

    assert cycle.status == loop.SUCCESS
    assert cycle.ok is True
    assert len(cycle.rounds) == 1
    assert script.run_calls == 1
    assert cycle.final_run is not None and cycle.final_run.passed == 1
    assert cycle.rounds[0].score is not None       # 自评跑过且记录了
    assert cycle.rounds[0].snapshot is not None    # 快照留了


# ---------------------------------------------------------------------------
# 剧本 3：第一轮有可修失败 → 带着失败摘要重写 → 第二轮通过
# ---------------------------------------------------------------------------
def test_fixable_failure_triggers_second_round_with_feedback(monkeypatch, ws_tmp):
    bad = Step(
        _run([_case("test_health", "failed", "KeyError: 'answer'")]),
        (_v("test_health", FailureKind.SCRIPT, Action.FIX, "KeyError: 'answer'"),),
    )
    script = Script([bad, GREEN]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.SUCCESS
    assert len(cycle.rounds) == 2
    assert script.gen_calls == 2
    assert script.feedback_seen[0] == ""                 # 第一轮没有"上次的错"
    assert "KeyError" in script.feedback_seen[1]         # 第二轮带上了失败摘要
    assert "重写" in cycle.rounds[0].note


# ---------------------------------------------------------------------------
# 剧本 4：只有系统缺陷 → 一条都不改，直接出报告
# ---------------------------------------------------------------------------
def test_system_defect_never_edits_tests(monkeypatch, ws_tmp):
    bad = Step(
        _run([_case("test_ask", "failed", "500 != 200")]),
        (_v("test_ask", FailureKind.SYSTEM_BUG, Action.REPORT_DEFECT, "/api/ask 返回 500"),),
    )
    script = Script([bad]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.DEFECT
    assert len(cycle.rounds) == 1
    assert script.gen_calls == 1          # 没有第二次生成 —— 缺陷绝不靠改测试绕过
    assert len(cycle.final_triage.defects) == 1
    assert "不改测试" in cycle.rounds[0].note


# ---------------------------------------------------------------------------
# 剧本 5：两轮都修不好 → 到上限放弃
# ---------------------------------------------------------------------------
def test_gives_up_at_round_limit(monkeypatch, ws_tmp):
    bad = Step(
        _run([_case("test_health", "failed", "TypeError: bad arg")]),
        (_v("test_health", FailureKind.SCRIPT, Action.FIX, "TypeError"),),
    )
    script = Script([bad, bad]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.GAVE_UP
    assert len(cycle.rounds) == 2         # 严格不超过上限
    assert script.run_calls == 2
    assert "上限" in cycle.rounds[-1].note


# ---------------------------------------------------------------------------
# 剧本 6：生成的一直不合规（静态检查不过）
# ---------------------------------------------------------------------------
def test_invalid_generation_becomes_error_at_limit(monkeypatch, ws_tmp):
    script = Script([GREEN]).install(
        monkeypatch, ws_tmp, gen_ok=False, violations=["用了 r.json()，应为 json_body"]
    )
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.ERROR
    assert script.gen_calls == 2
    assert script.run_calls == 0                       # 代码不合规就不该执行
    assert "静态校验未通过" in cycle.rounds[0].note
    assert "r.json()" in script.feedback_seen[1]       # 违规原因回喂了第二轮


# ---------------------------------------------------------------------------
# 剧本 7：执行层失败（pytest 崩了 / 超时）
# ---------------------------------------------------------------------------
def test_execution_error_sets_error_status(monkeypatch, ws_tmp):
    script = Script([Step(_run(error="pytest 超时"))]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.ERROR
    assert len(cycle.rounds) == 1
    assert "pytest 超时" in cycle.rounds[0].note


# ---------------------------------------------------------------------------
# 剧本 8：附加步骤失败要降级，不能拖垮主流程
# ---------------------------------------------------------------------------
def test_critic_failure_is_degraded_not_fatal(monkeypatch, ws_tmp):
    Script([GREEN]).install(monkeypatch, ws_tmp, critic_raises=True)
    cycle = loop.run_cycle("fake-app", max_rounds=2, write_report=False)

    assert cycle.status == loop.SUCCESS
    assert cycle.rounds[0].score is None
    assert any("自评失败" in n for n in cycle.notes)


def test_snapshot_failure_is_degraded_not_fatal(monkeypatch, ws_tmp):
    Script([GREEN]).install(monkeypatch, ws_tmp, snap_raises=True)
    cycle = loop.run_cycle("fake-app", max_rounds=2, use_critic=False, write_report=False)

    assert cycle.status == loop.SUCCESS
    assert cycle.rounds[0].snap is None
    assert any("快照失败" in n for n in cycle.notes)


# ---------------------------------------------------------------------------
# 报告落盘 + 终端渲染
# ---------------------------------------------------------------------------
def test_cycle_writes_report(monkeypatch, ws_tmp):
    Script([GREEN]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=2, report_dir=ws_tmp / "reports")

    assert cycle.report_path is not None
    assert cycle.report_path.exists()
    assert "fake-app" in cycle.report_path.read_text(encoding="utf-8")


def test_render_shows_status_and_cases(monkeypatch, ws_tmp):
    bad = Step(
        _run([_case("test_health", "passed"), _case("test_ask", "failed", "500 != 200")]),
        (_v("test_ask", FailureKind.SYSTEM_BUG, Action.REPORT_DEFECT, "/api/ask 500"),),
    )
    Script([bad]).install(monkeypatch, ws_tmp)
    cycle = loop.run_cycle("fake-app", max_rounds=1, use_critic=False, write_report=False)
    text = loop.render(cycle)

    assert "自愈循环 · fake-app" in text
    assert cycle.status in text
    assert "缺陷清单" in text


# ---------------------------------------------------------------------------
# 内部工具：feedback 只喂"可修类"，绝不喂系统缺陷
# ---------------------------------------------------------------------------
def test_feedback_excludes_system_defects():
    run = _run([_case("test_a", "failed", "KeyError"),
                _case("test_b", "failed", "500 != 200")])
    report = TriageReport(verdicts=(
        _v("test_a", FailureKind.SCRIPT, Action.FIX, "KeyError: x"),
        _v("test_b", FailureKind.SYSTEM_BUG, Action.REPORT_DEFECT, "/api/ask 500"),
    ))

    text = loop._feedback_from(report, run)

    assert "test_a" in text
    assert "test_b" not in text            # 系统缺陷不许进反馈，否则等于教模型改断言
    assert "系统Bug" not in text
