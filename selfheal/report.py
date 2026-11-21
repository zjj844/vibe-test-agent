"""M8 · 成绩单（report.py）

=====================================================================
本模块职责
=====================================================================
把一次完整循环（可能跑了 1~2 轮）的全部结果，写成一份**人看的一页纸**。

=====================================================================
在整个系统里的位置
=====================================================================
    loop（跑完所有轮）→ 【report】→ REPORT-<时间>.md
                                          ↑
                                    这份文件就是对外交付物

它调用：无（只是拼字符串 + 写文件）
它被调用：loop 结束时

=====================================================================
为什么报告这么重要（不是"顺便输出一下"）
=====================================================================
自愈系统的全部价值，最终都体现在这份报告里：
  1. **通过率** —— 我这次到底测出了什么
  2. **失败分类** —— 哪些是脚本问题（我改），哪些是系统缺陷（你改）
  3. **缺陷清单** —— 给开发/产品看的、可以直接转成 issue 的条目
  4. **每轮做了什么** —— 我为什么改、改完好了没有

如果只打印"3 passed"，那和直接跑 pytest 没有区别 —— **工具的价值在于"归因"。**
=====================================================================
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # 只用于类型提示，避免和 loop 循环导入
    from .loop import CycleResult

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

_STATUS_TEXT = {
    "success": "✅ 全部通过",
    "defect": "⚠️ 发现服务端缺陷（已出缺陷清单，未改测试）",
    "needs_human": "🟡 疑似契约与实现不一致（已挂起，需人工确认；未改测试）",
    "unreachable": "⛔ 服务不可达，已停手",
    "gave_up": "❌ 达到轮数上限仍未通过",
    "error": "❗ 执行层失败",
}


def _fmt_round(idx: int, r) -> list[str]:
    """渲染一轮的结果。"""
    lines = [f"### 第 {idx} 轮", ""]

    if r.generation is not None:
        g = r.generation
        lines.append(f"- 生成：{g.case_count} 个用例"
                     + ("（含 1 次重写）" if g.repaired else "")
                     + (f"，覆盖接口 `{', '.join(g.endpoint_ids)}`" if g.endpoint_ids else ""))
        if not g.ok:
            lines.append(f"- ❗ 静态校验未通过：{'; '.join(g.violations[:3])}")
    if r.score is not None:
        s = r.score
        dims = "  ".join(f"{k}={v:.1f}" for k, v in s.scores.items())
        lines.append(f"- 自评：**{s.total:.2f}/5**（{s.samples} 次采样取中位）　{dims}")
        if s.comment:
            lines.append(f"- 评审意见：{s.comment}")
    if r.snapshot is not None:
        lines.append(f"- 快照：{r.snapshot.label}（{len(r.snapshot.files)} 个文件）")
    if r.generated_file is not None:
        lines.append(f"- 落盘：`{r.generated_file.name}`")
    if r.run is not None:
        run = r.run
        lines.append(
            f"- 执行：{len(run.cases)} 个用例 → 通过 **{run.passed}** · 失败 "
            f"{run.count('failed')} · 错误 {run.count('error')} · 跳过 {run.count('skipped')}"
            f"（{run.duration:.1f}s）"
        )
        if run.error:
            lines.append(f"- ❗ {run.error}")
    if r.triage is not None and r.triage.verdicts:
        t = r.triage
        lines.append("- 分流：" + "、".join(
            f"{k.value} {t.count(k)}" for k in type(t.verdicts[0].kind) if t.count(k)
        ))
    if r.advices:
        lines.append(f"- AI 辅助判断：{len(r.advices)} 条（仅供参考，未产生任何动作）")
    if r.note:
        lines.append(f"- 决定：{r.note}")
    lines.append("")
    return lines


def build(cycle: "CycleResult") -> str:
    """把整次循环渲染成 Markdown。"""
    t = cycle.target_name
    lines: list[str] = []

    lines += [f"# 测试报告 · {t}", ""]
    lines += [
        f"- 被测项目：`{t}`",
        f"- 开始：{cycle.started_at}　结束：{cycle.finished_at}",
        f"- 轮数：{len(cycle.rounds)}（上限 {cycle.max_rounds}）",
        f"- 结论：**{_STATUS_TEXT.get(cycle.status, cycle.status)}**",
        "",
    ]

    # ---- 概览数字 ----
    final_run = cycle.final_run
    if final_run is not None:
        total = len(final_run.cases)
        percent = (final_run.passed / total * 100) if total else 0.0
        lines += [
            "## 概览",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 用例总数 | {total} |",
            f"| 通过 | {final_run.passed} |",
            f"| 失败 / 错误 | {final_run.count('failed')} / {final_run.count('error')} |",
            f"| 跳过 | {final_run.count('skipped')} |",
            f"| 通过率 | {percent:.1f}% |",
            f"| 执行耗时 | {final_run.duration:.1f}s |",
            "",
        ]

    # ---- 缺陷清单（最重要的一节：这是给人跟进的） ----
    defects = cycle.final_triage.defects if cycle.final_triage else ()
    lines += ["## 缺陷清单（系统 Bug：工具不改测试，需人工跟进）", ""]
    if defects:
        lines += ["| 用例 | 判定依据 |", "|---|---|"]
        for v in defects:
            lines.append(f"| `{v.nodeid}` | 命中规则 `{v.matched}`：{v.reason} |")
    else:
        lines.append("- 无（本次没有发现服务端缺陷）")
    lines.append("")

    # ---- 需人工确认（契约与实现不一致：不改也不重写）----
    held = cycle.final_triage.held if cycle.final_triage else ()
    if held:
        lines += ["## 需人工确认（HTTP 通了但数据不符：不改测试、不自动重写）", ""]
        lines += [
            "> 这三者只有人能分清：**服务错了 / 契约写错了 / 用例写错了**。",
            "> 工具在这里主动放弃自动修复 —— 因为改断言是最省事、也最容易把缺陷藏起来的做法。",
            "",
            "| 用例 | 判定依据 |",
            "|---|---|",
        ]
        for v in held:
            lines.append(f"| `{v.nodeid}` | 命中规则 `{v.matched}`：{v.reason} |")
        lines.append("")

    # ---- AI 辅助判断（只给建议，没有任何动作权限）----
    advices = cycle.final_advices
    if advices:
        lines += ["## AI 辅助判断（仅供参考：模型无改测试/改契约的权限）", ""]
        for a in advices:
            if a.ok:
                lines.append(f"- `{a.nodeid}`")
                lines.append(f"  - 责任判断：**{a.responsibility}**")
                lines.append(f"  - 依据：{a.reason}")
                lines.append(f"  - 建议（给人）：{a.suggestion}")
            else:
                lines.append(f"- `{a.nodeid}`：AI 未给出建议（{a.error}）")
        lines.append("")

    # ---- 可修类（工具能处理的） ----
    fixable = cycle.final_triage.fixable if cycle.final_triage else ()
    lines += ["## 可修类失败（脚本 / 断言问题）", ""]
    if fixable:
        for v in fixable:
            lines.append(f"- `{v.nodeid}` —— {v.kind.value}（命中 `{v.matched}`）")
    else:
        lines.append("- 无")
    lines.append("")

    # ---- 每轮明细 ----
    lines += ["## 每轮明细", ""]
    for i, r in enumerate(cycle.rounds, 1):
        lines += _fmt_round(i, r)

    # ---- 备注 ----
    if cycle.notes:
        lines += ["## 备注", ""]
        lines += [f"- {n}" for n in cycle.notes]
        lines.append("")

    lines += ["---", "", f"由 selfheal 自动生成 · {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    return "\n".join(lines)


def write(cycle: "CycleResult", out_dir: Path | str = REPORT_DIR) -> Path:
    """写出报告文件，返回路径。文件名带时间戳，不覆盖历史。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"REPORT-{time.strftime('%Y%m%d-%H%M%S')}-{cycle.target_name}.md"
    path.write_text(build(cycle), encoding="utf-8")
    return path
