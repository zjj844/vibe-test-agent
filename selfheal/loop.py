"""M9 · 主循环（loop.py）—— 把前面 8 个模块串成一条闭环

=====================================================================
本模块职责
=====================================================================
**唯一的编排者**。它自己不实现任何能力（不生成代码、不评分、不跑测试），
只负责按固定顺序调用，并根据每一轮的结果决定"下一步做什么 / 什么时候停"。

=====================================================================
一句话记忆
=====================================================================
    探 → 读 → 写 → 校验 → 评 → 快照 → 跑 → 分 → 定 → 报

（读 = 检索需求；写 = LLM 生成；校验 = 静态检查；评 = rubric 自评；
  快照 = 存回滚点；跑 = pytest；分 = 失败分流；定 = 决定继续还是停）

=====================================================================
为什么"编排写在代码里"，而不是让模型自己决定调哪个工具
=====================================================================
这是整个项目最核心的架构选择，面试必问：

  做法 A（自由 tool-calling）：把 8 个工具丢给模型，让它自己决定顺序。
    看起来"智能"，但 —— 不可复现（同样输入两次跑不同）、不可审计
   （出了 bug 不知道它为什么跳到那一步）、不可测试（没法写单元测试）。

  做法 B（固定编排，本文件）：顺序写死在代码里，模型只负责**两件事**：
    第 4 步"写代码"、第 5 步"打分"。其余全是确定性的 Python。

  为什么选 B：测试这个行业要的是**可信**。同一个版本、同样的输入，
  必须跑出同样的流程。所以"哪些决策交给模型"是一条明确的边界：
      模型负责"创作文本"，代码负责"控制流程"。

=====================================================================
有界（bounded）：这个循环一定会停
=====================================================================
四道刹车，缺一不可：
  1. 轮数上限（默认 2）—— 收益递减 + 成本翻倍
  2. 服务不可达 → 立即 STOP（不重试、不硬跑，否则全是假失败）
  3. 命中"系统 Bug" → 不改测试，转成缺陷清单后结束
  4. pytest 整体超时（runner 里 900s）—— 服务卡死也能退出

  特别强调第 3 条：**自愈的第一条规则是"哪些不许修"**。
  如果测试断言的是"英文返回中文内容"，而服务确实返回了中文 ——
  这是服务的 Bug。此时去改测试让它通过，等于**把缺陷藏起来**，
  比漏测还糟。所以 defect 直接终结循环，只出报告。

=====================================================================
它调用谁
=====================================================================
  targets（探活）· kb（检索需求）· generator（生成）· critic（自评）
  snapshot（快照）· runner（执行）· triage（分流）· report（出报告）
=====================================================================
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import critic, generator, kb, report, runner, snapshot, targets, triage

# 生成产物的默认目录（runner/triage 都认这个约定）
GENERATED_DIR = Path(__file__).resolve().parent.parent / "evals" / "generated"

# 循环状态
SUCCESS = "success"            # 全部通过
DEFECT = "defect"              # 发现服务端缺陷（已出清单，未改测试）
UNREACHABLE = "unreachable"    # 服务不可达，停手
GAVE_UP = "gave_up"            # 达到轮数上限仍未通过
ERROR = "error"                # 执行层失败（pytest 崩了 / 生成不合法）


# ---------------------------------------------------------------------------
# 数据结构：一轮 / 一次循环
# ---------------------------------------------------------------------------
@dataclass
class RoundResult:
    """一轮尝试的全部痕迹。故意把每一步的产物都留下来 —— 报告就是从这里渲染的。"""

    index: int
    probe: targets.ProbeResult | None = None
    requirements: list[dict] = field(default_factory=list)
    generation: generator.GenerationResult | None = None
    score: critic.ScoreResult | None = None
    snap: snapshot.Snapshot | None = None
    generated_file: Path | None = None
    run: runner.RunResult | None = None
    triage: triage.TriageReport | None = None
    note: str = ""                     # 这一轮结束后"决定继续/停止"的理由
    elapsed: float = 0.0

    # 给 report.py 用的别名（那边按 r.snapshot 读）
    @property
    def snapshot(self) -> snapshot.Snapshot | None:
        return self.snap


@dataclass
class CycleResult:
    """一次完整循环的结果 —— 也就是 report.write() 的输入。"""

    target_name: str
    started_at: str
    finished_at: str = ""
    status: str = ""
    max_rounds: int = 2
    rounds: list[RoundResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def final_run(self) -> runner.RunResult | None:
        """最后一轮的执行结果（报告里的"概览"就该看这个）。"""
        for r in reversed(self.rounds):
            if r.run is not None:
                return r.run
        return None

    @property
    def final_triage(self) -> triage.TriageReport | None:
        for r in reversed(self.rounds):
            if r.triage is not None:
                return r.triage
        return None

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _requirement_query(target: targets.Target) -> str:
    """构造检索用的查询串。

    为什么不用固定字符串：需求文档是按模块写的，
    拿"业务含义"当查询词才能检索到**相关**的那几节，
    而不是每次都捞到同一段无关内容。
    """
    bits = [target.name]
    bits += [ep.notes for ep in target.endpoints if ep.notes]
    return " ".join(bits) or target.name


def _feedback_from(t: triage.TriageReport, run: runner.RunResult) -> str:
    """把上一轮的失败总结成一段话，回喂给生成器。

    只喂"可修类"的失败 —— 系统缺陷那部分**不能**回喂，
    否则模型会以为"我该改测试让它过"，正中下怀。
    """
    lines: list[str] = []
    for v in t.fixable[:6]:
        case = next((c for c in run.cases if c.nodeid == v.nodeid), None)
        msg = (case.message if case and case.message else v.reason)[:180]
        lines.append(f"- {case.name if case else v.nodeid}：{v.kind.value} —— {msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def _notify(on_step: Any, idx: int, stage: str) -> None:
    """调用进度回调。

    为什么单独包一层并 try/except：
    回调是**外部代码**（可能是前端、可能是测试）。
    它抛异常不能影响主流程 —— 界面挂了不该让一次测试运行跟着挂。
    """
    if on_step is None:
        return
    try:
        on_step(idx, stage)
    except Exception:                                  # noqa: BLE001
        pass


def run_cycle(
    target_name: str,
    n_cases: int = 3,
    max_rounds: int = 2,
    use_critic: bool = True,
    gen_dir: Path | str = GENERATED_DIR,
    report_dir: Path | str = report.REPORT_DIR,
    write_report: bool = True,
    on_step: Any = None,
) -> CycleResult:
    """跑一次完整循环。

    参数：
      target_name  —— 契约名（`selfheal/configs/` 下的 json 名）
      n_cases      —— 每轮生成几条用例
      max_rounds   —— 轮数上限（**有界**的核心）
      use_critic   —— 是否跑自评（关掉可省一次模型调用，调试时常用）
      write_report —— 是否写出 Markdown 报告
      on_step      —— 可选进度回调 `on_step(round_idx, stage)`。
                      给前端报告台用的：有了它，界面才能显示"现在走到第几步"，
                      而不是干等 40 秒。**编排逻辑本身一行没变**——
                      加回调不改变流程，这是"编排在代码里"的好处之一。
    """
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    cycle = CycleResult(target_name=target_name, started_at=started, max_rounds=max_rounds)

    target = targets.load_target(target_name)
    cycle.notes.append(
        f"契约：{len(target.endpoints)} 个接口，base_url={target.base_url}"
    )

    # ---- 先建一次知识库（需求文档不常变，一次循环建一次就够）----
    # 每个被测项目有自己的需求文档（契约里 requirements_dir 指定）。
    # 不指定就用默认那份——但那样检索到的是"别人的需求"，等于白读。
    knowledge = kb.RequirementKB(
        docs_dir=target.requirements_dir
    ) if target.requirements_dir else kb.RequirementKB()
    query = _requirement_query(target)
    cycle.notes.append(
        f"需求库：{knowledge.doc_count} 篇 / {knowledge.chunk_count} 块；检索词 {len(query)} 字符"
    )

    feedback = ""          # 上一轮的失败摘要，喂给下一轮的生成器
    gen_path = Path(gen_dir)

    for idx in range(1, max_rounds + 1):
        r = RoundResult(index=idx)
        t0 = time.perf_counter()
        cycle.rounds.append(r)

        # 整个"一轮"包在 try/finally 里，只为了最后那句 finally：
        # **不管这一轮是怎么结束的（正常 break、异常、还是 Ctrl+C），耗时都要记上。**
        # 实测踩过：只在"继续下一轮"的分支里记时间，结果成功那一轮显示"总耗时 0.0s"。
        try:
            # ---------- 第 1 步：探活（不可达就别玩了）----------
            _notify(on_step, idx, "探活")
            r.probe = targets.probe(target)
            if not r.probe.ok:
                r.note = f"服务不可达：{r.probe.detail}"
                cycle.status = UNREACHABLE
                cycle.notes.append("探测失败 → 立即停手（不硬跑，避免全是假失败）")
                break

            # ---------- 第 2 步：读需求 ----------
            _notify(on_step, idx, "检索需求")
            r.requirements = knowledge.search(query, top_k=3)

            # ---------- 第 3 步：写代码（唯一"创作"步骤）----------
            _notify(on_step, idx, "生成用例")
            try:
                r.generation = generator.generate(
                    target, r.requirements, n_cases=n_cases, feedback=feedback
                )
            except Exception as exc:                   # noqa: BLE001 模型/网络异常不炸整个循环
                r.note = f"生成失败：{exc}"
                cycle.status = ERROR
                cycle.notes.append(r.note)
                break

            if not r.generation.ok:
                r.note = "静态校验未通过：" + "；".join(r.generation.violations[:3])
                if idx >= max_rounds:
                    cycle.status = ERROR
                    cycle.notes.append(r.note)
                    break
                feedback = "上一版代码没通过静态检查：\n- " + "\n- ".join(
                    r.generation.violations
                )
                continue                               # 下一轮带着违规原因重写

            # ---------- 第 4 步：自评（rubric 打分，仅供参考，不阻断）----------
            if use_critic:
                _notify(on_step, idx, "自评打分")
                try:
                    r.score = critic.score(r.generation.code, samples=2)
                except Exception as exc:               # noqa: BLE001 评分失败不影响主流程
                    cycle.notes.append(f"第 {idx} 轮自评失败（已忽略）：{exc}")

            # ---------- 第 5 步：落盘 ----------
            _notify(on_step, idx, "落盘")
            try:
                r.generated_file = generator.save(
                    r.generation, target, gen_path, tag=f"r{idx}"
                )
            except Exception as exc:                   # noqa: BLE001
                r.note = f"落盘失败：{exc}"
                cycle.status = ERROR
                cycle.notes.append(r.note)
                break

            # ---------- 第 6 步：快照（留回滚点）----------
            _notify(on_step, idx, "快照")
            try:
                r.snap = snapshot.take(
                    [r.generated_file],
                    label=f"round{idx}",
                    reason=f"第 {idx} 轮生成产物，可用于回滚到本轮",
                )
            except Exception as exc:                   # noqa: BLE001 快照失败不阻断（降级）
                cycle.notes.append(f"第 {idx} 轮快照失败（已忽略）：{exc}")

            # ---------- 第 7 步：跑 ----------
            _notify(on_step, idx, "执行用例")
            r.run = runner.run_tests(r.generated_file, timeout=900, report_dir=report_dir)
            if r.run.error:
                r.note = f"执行层失败：{r.run.error}"
                cycle.status = ERROR
                cycle.notes.append(r.note)
                break

            # ---------- 第 8 步：分流 ----------
            _notify(on_step, idx, "失败分流")
            r.triage = triage.classify_run(r.run)

            # ---------- 第 9 步：决定 ----------
            _notify(on_step, idx, "决策")
            if r.run.all_green:
                r.note = f"全部通过（{r.run.passed} 条），收工"
                cycle.status = SUCCESS
                break

            if r.triage.must_stop:
                r.note = "出现服务不可达 → 停手"
                cycle.status = UNREACHABLE
                break

            if r.triage.defects and not r.triage.fixable:
                # 全是系统 Bug：一条都不许改，直接出缺陷报告
                r.note = f"发现 {len(r.triage.defects)} 条系统缺陷 → 不改测试，出报告"
                cycle.status = DEFECT
                break

            if r.triage.defects:
                cycle.notes.append(
                    f"第 {idx} 轮：{len(r.triage.defects)} 条系统缺陷先挂起（不修），"
                    f"只修 {len(r.triage.fixable)} 条可修失败"
                )

            if idx >= max_rounds:
                r.note = f"达到轮数上限（{max_rounds}）仍有失败 → 放弃自愈"
                cycle.status = GAVE_UP
                break

            feedback = _feedback_from(r.triage, r.run)
            r.note = f"可修失败 {len(r.triage.fixable)} 条 → 带着失败摘要重写"
        finally:
            r.elapsed = time.perf_counter() - t0

    cycle.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    cycle.notes.append(f"总耗时 {_elapsed_of(cycle):.1f}s")

    if write_report:
        try:
            cycle.report_path = report.write(cycle, report_dir)
        except Exception as exc:                       # noqa: BLE001 报告失败不该让结果丢失
            cycle.notes.append(f"写报告失败：{exc}")

    return cycle


def _elapsed_of(cycle: CycleResult) -> float:
    return sum(r.elapsed for r in cycle.rounds)


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------
_MARK = {SUCCESS: "✅", DEFECT: "⚠️", UNREACHABLE: "⛔", GAVE_UP: "❌", ERROR: "❗"}


def render(cycle: CycleResult) -> str:
    """把整次循环画在终端上（给"我现在就想看结果"的场景）。"""
    lines = ["=" * 72, f" 自愈循环 · {cycle.target_name}", "=" * 72]
    for r in cycle.rounds:
        lines.append(f"\n--- 第 {r.index} 轮 ---")
        if r.probe is not None:
            lines.append(f"  探活  {r.probe.line()}")
        if r.requirements:
            lines.append(f"  读需求 检索到 {len(r.requirements)} 个片段："
                         + ", ".join(c["chunk_id"] for c in r.requirements))
        if r.generation is not None:
            lines.append(f"  写   {r.generation.summary()}")
        if r.score is not None:
            lines.append(f"  评   总分 {r.score.total:.2f}/5  {r.score.comment}")
        if r.snap is not None:
            lines.append(f"  快照 {r.snap.summary()}")
        if r.run is not None:
            lines.append(f"  跑   通过 {r.run.passed} / 共 {len(r.run.cases)}（{r.run.duration:.1f}s）")
        if r.triage is not None:
            lines.append(f"  分   {r.triage.conclusion()}")
        if r.note:
            lines.append(f"  定   {r.note}")

    lines.append("")
    lines.append("-" * 72)
    lines.append(f"结果：{_MARK.get(cycle.status, '?')} {cycle.status}")

    final = cycle.final_run
    if final is not None and final.cases:
        lines.append(final.table())

    defects = cycle.final_triage.defects if cycle.final_triage else ()
    if defects:
        lines.append("\n缺陷清单（工具不改测试，需人工跟进）：")
        for v in defects:
            lines.append(f"  · {v.nodeid}｜{v.reason}")

    if cycle.report_path:
        lines.append(f"\n报告：{cycle.report_path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.loop --target rag-backend --n 3 --rounds 2

    预期输出（被测服务已启动）：
        结果：✅ success
        报告：D:\\dsh\\vibe-test-agent\\reports\\REPORT-...-rag-backend.md
    服务没起：
        结果：⛔ unreachable
    """
    import argparse

    ap = argparse.ArgumentParser(description="自愈测试主循环")
    ap.add_argument("--target", required=True, help="契约名，如 rag-backend")
    ap.add_argument("--n", type=int, default=3, help="每轮生成几条用例")
    ap.add_argument("--rounds", type=int, default=2, help="轮数上限（有界）")
    ap.add_argument("--no-critic", action="store_true", help="跳过 rubric 自评（省一次模型调用）")
    ap.add_argument("--no-report", action="store_true", help="不写 Markdown 报告")
    args = ap.parse_args(argv)

    cycle = run_cycle(
        args.target,
        n_cases=args.n,
        max_rounds=args.rounds,
        use_critic=not args.no_critic,
        write_report=not args.no_report,
    )
    print(render(cycle))
    return 0 if cycle.status in (SUCCESS, DEFECT) else 1


if __name__ == "__main__":
    raise SystemExit(main())
