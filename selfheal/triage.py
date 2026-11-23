"""M6 · 判卷老师（triage.py）

=====================================================================
本模块职责
=====================================================================
把 runner 收集到的失败，按**原因**分成 5 类，并给每一类一个**动作**：
停手 / 出缺陷报告 / 重新生成 / 修 / 人工看。

=====================================================================
在整个系统里的位置
=====================================================================
    runner（跑出结果）→ 【triage（判定是谁的责任）】→ loop（决定改不改）→ report

它调用：无（纯规则，不联网、不问模型）
它被调用：loop、runner 的命令行

=====================================================================
为什么这是整个项目最重要的一块
=====================================================================
自愈系统的第一条规则**不是"怎么修"，而是"哪些不许修"**。

如果模型看到红色就去改测试，它会把"接口真的坏了"改成"测试通过了"——
**缺陷被掩盖，比漏测危险得多。**
所以必须先判"这是谁的错"，才谈得上修。

=====================================================================
为什么用规则而不是让模型分类
=====================================================================
模型的判断**不可复现、不可审计**：同一个报错，两次可能分到不同类别，
而且你无法向面试官/同事证明"为什么这次判成系统 Bug"。
规则不一样：可复现、可写单测、可追溯（每个判定都带"命中了哪条规则"）。
模型分类留作 v2 的兜底（只处理规则判不了的那一小部分）。

=====================================================================
五类 + 五个动作
=====================================================================
    ① 服务不可达 → STOP          什么都不许改（先起服务）
    ② 系统 Bug   → REPORT_DEFECT  不改测试，出缺陷报告给人
    ③ 生成代码问题 → REGENERATE    这段用例作废，重新生成
    ④ 断言问题   → FIX           可以改（改断言或改契约，需二选一并说明）
    ⑤ 脚本问题   → FIX           可以改
=====================================================================
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .runner import ERROR, FAILED, CaseResult, RunResult


class FailureKind(str, Enum):
    """失败类别。值用中文，方便直接进报告。"""

    SERVER_UNREACHABLE = "服务不可达"
    SYSTEM_BUG = "系统Bug"
    CONTRACT_MISMATCH = "契约与实现不一致"
    GENERATED_CODE = "生成代码问题"
    ASSERTION = "断言问题"
    SCRIPT = "脚本问题"
    UNKNOWN = "未知"


class Action(str, Enum):
    """允许采取的动作。"""

    STOP = "stop"                    # 立刻停手，什么都不改
    REPORT_DEFECT = "report_defect"  # 写缺陷报告，不改测试
    HOLD = "hold"                    # 挂起：不改也不重写，交给人确认
    REGENERATE = "regenerate"        # 重新生成这段用例
    FIX = "fix"                      # 可以改测试
    MANUAL = "manual"                # 规则判不了，交给人


# ---------------------------------------------------------------------------
# 规则表：从上到下匹配，第一条命中的生效（顺序很重要！）
# ---------------------------------------------------------------------------
# 为什么顺序是"严重 → 轻"：
#   "连接被拒"也可能同时包含在别的文本里，先判不可达最安全；
#   而"接口真的 500"必须先于"断言失败"判，否则会被当成'断言写错了'去改测试。
RULES: tuple[tuple[str, FailureKind, Action, str], ...] = (
    # ① 服务不可达：所有用例都会红，但红不是用例的错
    (
        r"ConnectionRefusedError|ConnectionError|NewConnectionError|"
        r"Max retries exceeded|Failed to establish a new connection|"
        r"NameResolutionError|Connection refused|SSLError.*refused",
        FailureKind.SERVER_UNREACHABLE,
        Action.STOP,
        "连不上被测服务：先起服务，再谈测试（此时改测试毫无意义）",
    ),
    # ②' 业务层 5xx：**HTTP 通了（2xx）**，但响应体里 code=5xx
    #
    # 为什么必须单列、且排在 ② 之前：很多国内后端的约定是"业务错误用 body.code 表达，
    # HTTP 仍返 200"。实测踩到：契约里 login 接口没填 body → 后端抛异常被全局
    # 异常处理器兜成 {"code":500,"msg":"系统繁忙"} → 断言消息是 `assert 500 == 200`，
    # 被 ② 判成"HTTP 5xx 系统缺陷"，于是报告把责任推给服务端，而真正原因是
    # **请求参数/契约不完整**。所以：只要消息里明确写着"响应体 code=5xx"，就走这一条。
    (
        r"响应体 code=5\d\d",
        FailureKind.SYSTEM_BUG,
        Action.REPORT_DEFECT,
        "HTTP 通了但响应体业务码是 5xx（多为后端异常被兜底）：先核对请求参数/契约是否完整，再判定是否为缺陷",
    ),
    # ② 系统 Bug：接口真的坏了 —— HTTP 5xx
    (
        r"Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|"
        r"HTTP 5\d\d|status[\s_]*code[\s_]*[=:][\s_]*5\d\d|assert 5\d\d ==",
        FailureKind.SYSTEM_BUG,
        Action.REPORT_DEFECT,
        "接口返回 5xx：这是服务端缺陷，**不许改测试**，写缺陷报告交给人",
    ),
    # ③ 契约与实现不一致：HTTP 200，但响应体的值不符合契约
    #
    # 为什么单列一类、而且是 HOLD 而不是 FIX —— 这是本项目最容易被忽略的坑：
    # 之前这条会落到 ⑤/④（断言问题）→ 动作 FIX → 第 2 轮"重写"，于是模型很可能
    # 把断言改宽（比如把 `== 200` 改成 `in (200, 999)`）让它变绿。
    # 那就等于"把缺陷改成预期行为"，和本项目的根本原则直接冲突。
    #
    # 而这种情况**恰恰无法自动判定责任**：
    #   · 可能服务错了（该返回 2，返回了 0）
    #   · 可能契约写错了（业务规则本来就该是 0）
    #   · 可能用例写错了（比较的字段不是一个东西）
    # 三者只有人能分清。所以动作是 HOLD：不改、也不重写，挂起等人。
    (
        r"响应字段值与契约不符|响应缺字段|字段值与契约|missing_fields|"
        r"契约里没有接口",
        FailureKind.CONTRACT_MISMATCH,
        Action.HOLD,
        "HTTP 通了但数据不对：可能是服务错、契约错或用例错，三者只有人能分清 → 不改测试，交给人确认",
    ),
    # ④ 生成代码问题：代码本身有硬伤（凭空发明 API / 语法错 / 名字写错）
    (
        r"AttributeError|NameError|ModuleNotFoundError|ImportError|"
        r"SyntaxError|IndentationError|is not callable",
        FailureKind.GENERATED_CODE,
        Action.REGENERATE,
        "生成的代码有硬伤（引用了不存在的成员/模块）：作废这段，重新生成",
    ),
    # ④ 断言问题：代码跑通了，但期望与事实不符
    (
        r"AssertionError|assert\s.+==|assert not ",
        FailureKind.ASSERTION,
        Action.FIX,
        "断言与事实不符：需判断是断言写错、还是契约写错（二选一并说明）",
    ),
    # ⑤ 脚本问题：逻辑类错误（取错字段、越界、类型用错）
    (
        r"KeyError|IndexError|TypeError|ValueError|ZeroDivisionError|JSONDecodeError",
        FailureKind.SCRIPT,
        Action.FIX,
        "用例逻辑有错（取错字段/类型不符）：可以修",
    ),
)


@dataclass(frozen=True)
class Verdict:
    """一条失败的判定结果。"""

    nodeid: str
    kind: FailureKind
    action: Action
    reason: str
    matched: str = ""      # 命中的规则片段（可追溯：为什么判成这一类）

    def line(self) -> str:
        return f"{self.kind.value:<12} {self.action.value:<14} {self.nodeid}"


@dataclass
class TriageReport:
    """整批失败的判定汇总。"""

    verdicts: tuple[Verdict, ...] = ()
    passed: int = 0
    skipped: int = 0
    error: str = ""        # 执行层错误（没产出 xml / 超时）

    # ---- 统计 ----
    def count(self, kind: FailureKind) -> int:
        return sum(1 for v in self.verdicts if v.kind is kind)

    @property
    def fixable(self) -> tuple[Verdict, ...]:
        """允许自动修的（③④⑤ 里的 FIX / REGENERATE 由 loop 决定怎么处理）。"""
        return tuple(v for v in self.verdicts if v.action is Action.FIX)

    @property
    def must_stop(self) -> bool:
        """出现"服务不可达"就必须停手。"""
        return any(v.action is Action.STOP for v in self.verdicts)

    @property
    def defects(self) -> tuple[Verdict, ...]:
        """需要写缺陷报告的（系统 Bug）——这部分**永远不改测试**。"""
        return tuple(v for v in self.verdicts if v.action is Action.REPORT_DEFECT)

    @property
    def held(self) -> tuple[Verdict, ...]:
        """挂起等人确认的（契约与实现不一致）——不改、也不重写。"""
        return tuple(v for v in self.verdicts if v.action is Action.HOLD)

    # ---- 渲染 ----
    def conclusion(self) -> str:
        if self.error:
            return f"执行层失败：{self.error}"
        if not self.verdicts:
            return f"无失败（通过 {self.passed}，跳过 {self.skipped}）"
        parts = []
        for kind in FailureKind:
            n = self.count(kind)
            if n:
                parts.append(f"{kind.value} {n}")
        head = "出现服务不可达，必须停手；" if self.must_stop else ""
        tail = ""
        if self.defects:
            tail += f"其中 {len(self.defects)} 条是**服务端缺陷**，不许改测试。"
        if self.held:
            tail += f"另有 {len(self.held)} 条**疑似契约与实现不一致**，需人工确认（不改测试）。"
        return f"{head}共 {len(self.verdicts)} 条失败：" + "、".join(parts) + "。" + tail

    def render(self) -> str:
        lines = ["=" * 70, " 失败分流（triage）", "=" * 70]
        if self.error:
            lines.append(f"❗ {self.error}")
            return "\n".join(lines)
        if not self.verdicts:
            lines.append(f"✅ {self.conclusion()}")
            return "\n".join(lines)

        lines.append(f"{'类别':<12}{'动作':<14}用例")
        lines.append("-" * 70)
        for v in self.verdicts:
            lines.append(f"{v.kind.value:<12}{v.action.value:<14}{v.nodeid}")
        lines.append("-" * 70)
        lines.append("结论：" + self.conclusion())

        # 按类别聚合原因（同一类不用重复说 N 遍）
        seen: set[FailureKind] = set()
        lines.append("\n各类原因：")
        for v in self.verdicts:
            if v.kind in seen:
                continue
            seen.add(v.kind)
            lines.append(f"  · {v.kind.value}（{self.count(v.kind)} 条）：{v.reason}")
            if v.matched:
                lines.append(f"    命中规则：{v.matched[:60]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def classify_case(case: CaseResult) -> Verdict:
    """给单条失败分类。

    只看**错误文本**（message + detail）——这就是"可复现"的来源：
    同样的报错文本，永远得到同样的类别。
    """
    haystack = f"{case.message}\n{case.detail}"
    for pattern, kind, action, reason in RULES:
        m = re.search(pattern, haystack, re.I)
        if m:
            return Verdict(case.nodeid, kind, action, reason, matched=m.group(0))
    return Verdict(case.nodeid, FailureKind.UNKNOWN, Action.MANUAL,
                   "没匹配到已知规则：需要人工判断（v2 才考虑用模型兜底）")


def classify_run(run: RunResult) -> TriageReport:
    """给整批结果分类。执行层出错（超时/没产出 xml）时直接返回错误说明。"""
    if run.error:
        return TriageReport(error=run.error)

    verdicts = tuple(
        classify_case(c) for c in run.cases if c.status in (FAILED, ERROR)
    )
    return TriageReport(
        verdicts=verdicts,
        passed=run.passed,
        skipped=run.count("skipped"),
    )


# ---------------------------------------------------------------------------
# 命令行入口：只分析一份 junitxml
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.triage --xml evals/reports/junit-xxx.xml"""
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from .runner import parse_junitxml

    parser = argparse.ArgumentParser(description="给一份 junitxml 做失败分流")
    parser.add_argument("--xml", required=True, help="junitxml 路径")
    args = parser.parse_args(argv)

    cases = parse_junitxml(args.xml)
    run = RunResult(returncode=0, cases=cases, xml_path=None, duration=0.0)
    print(classify_run(run).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
