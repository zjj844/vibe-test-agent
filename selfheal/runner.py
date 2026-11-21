"""M5 · 监考（runner.py）

=====================================================================
本模块职责
=====================================================================
把一批测试文件**真的跑一遍**，然后把结果变成程序能处理的结构化数据。

=====================================================================
在整个系统里的位置
=====================================================================
    main → loop → generator → critic → 【runner】 → triage → report
                                          ↓
                                     junitxml（机读成绩单）

它调用：pytest（子进程）
它被调用：loop（主循环）、triage（吃它的输出）

=====================================================================
关键概念：junitxml 是什么
=====================================================================
pytest 除了在屏幕上打印"3 passed"，还能写出一份 **XML 格式的成绩单**：
每个用例的名字、状态（通过/失败/跳过）、耗时、失败信息、完整 traceback 都在里面。

为什么不用"看屏幕输出"：屏幕输出是给人看的，格式会变；
junitxml 是**给程序看的标准格式**，稳定、好解析。
（CI 系统（Jenkins/GitHub Actions）也是靠它来显示测试报告的。）

=====================================================================
三条设计纪律
=====================================================================
1. **不抛异常**：跑测试失败是常态（用例就是用来失败的）。
   网络/超时/没有 xml，都要变成"结果对象里的一句话"，而不是让程序崩。
2. **不走管道**：subprocess 用 DEVNULL，不捕获 stdout。
   （本机沙箱/某些环境下"带管道的子进程"会被拒；而且屏幕输出我们也不需要。）
3. **traceback 要完整保留**：triage 分流完全依赖错误文本，
   截断得太狠会导致分错类。这里保留末尾 4000 字符。
=====================================================================
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parent.parent / "evals" / "reports"

# ⚠️ 这里有两套"名字"，别混：
#   ① 状态名（我们自己用）：passed / failed / error / skipped
#   ② XML 标签名（junitxml 规定）：<failure> / <error> / <skipped>
# 实测踩坑：把 ② 写成 ① 的 `failed`，解析器永远匹配不到 <failure>，
# 于是所有失败都被判成"通过"——假绿。解析外部格式时一定要抬头看真实的标签名。
PASSED, FAILED, ERROR, SKIPPED = "passed", "failed", "error", "skipped"

_TAG_FAILURE = "failure"     # XML 标签
_TAG_ERROR = "error"         # XML 标签（也是状态名，但这里是标签）
_TAG_SKIPPED = "skipped"     # XML 标签


@dataclass(frozen=True)
class CaseResult:
    """一条用例的结果。"""

    nodeid: str          # 形如 "test_x.py::test_name"
    name: str            # 用例函数名
    status: str          # passed / failed / error / skipped
    duration: float      # 秒
    message: str = ""    # 失败消息首行（triage 用）
    detail: str = ""     # 完整 traceback（triage 用）

    @property
    def ok(self) -> bool:
        return self.status == PASSED


@dataclass(frozen=True)
class RunResult:
    """一次执行的整体结果。"""

    returncode: int
    cases: tuple[CaseResult, ...]
    xml_path: Path | None
    duration: float
    error: str = ""      # pytest 自己崩了 / 超时 / 没产出 xml 时的说明

    # ---- 统计 ----
    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)

    @property
    def passed(self) -> int:
        return self.count(PASSED)

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """所有"非通过且非跳过"的用例（失败和错误都要处理）。"""
        return tuple(c for c in self.cases if c.status in (FAILED, ERROR))

    @property
    def all_green(self) -> bool:
        return bool(self.cases) and not self.failures

    def table(self) -> str:
        """给人看的一行行结果。"""
        lines = []
        for c in self.cases:
            mark = {"passed": "✅", "failed": "❌", "error": "💥", "skipped": "⏭"}.get(c.status, "?")
            lines.append(f"  {mark} {c.name:<45} {c.duration:6.2f}s  {c.status}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析 junitxml
# ---------------------------------------------------------------------------
def parse_junitxml(xml_path: Path | str) -> tuple[CaseResult, ...]:
    """把 junitxml 读成一串 CaseResult。

    junitxml 的结构（简化）：
        <testsuites>
          <testsuite>
            <testcase name="test_x" time="1.2">
              <failure message="...">完整 traceback</failure>   ← 失败才有
            </testcase>
          </testsuite>
        </testsuites>
    """
    path = Path(xml_path)
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")

    cases: list[CaseResult] = []
    for suite in suites:
        for tc in suite.findall("testcase"):
            status, message, detail = PASSED, "", ""
            for child in tc:
                if child.tag == _TAG_FAILURE:
                    status = FAILED
                    message = (child.get("message") or "").strip()[:300]
                    detail = (child.text or "").strip()[-4000:]
                elif child.tag == _TAG_ERROR:
                    status = ERROR
                    message = (child.get("message") or "").strip()[:300]
                    detail = (child.text or "").strip()[-4000:]
                elif child.tag == _TAG_SKIPPED:
                    status = SKIPPED
                    message = (child.get("message") or "").strip()[:300]
            classname = tc.get("classname") or ""
            name = tc.get("name") or "(unnamed)"
            nodeid = f"{classname}::{name}" if classname else name
            cases.append(
                CaseResult(
                    nodeid=nodeid, name=name, status=status,
                    duration=float(tc.get("time") or 0.0),
                    message=message, detail=detail,
                )
            )
    return tuple(cases)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_tests(
    test_path: Path | str,
    timeout: float = 900.0,
    report_dir: Path | str = REPORT_DIR,
    extra_args: tuple[str, ...] = (),
) -> RunResult:
    """跑 pytest，返回结构化结果。

    参数：
      test_path  —— 一个文件或一个目录（直接传给 pytest）
      timeout    —— 整体超时（秒）。为什么要设：被测服务卡住时用例会一直等，
                    自愈循环不能无限等下去——**这就是"有界"的又一处体现**。
    """
    path = Path(test_path)
    if not path.exists():
        return RunResult(returncode=-1, cases=(), xml_path=None, duration=0.0,
                         error=f"测试路径不存在：{path}")

    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ 文件名必须**唯一**（时间戳 + 进程号 + 随机后缀）。
    # 实测踩到的坑：只精确到秒时，同一秒内跑两次会复用同一个 junit 路径；
    # 而 pytest 在"收集阶段就崩"（比如 import 错误）时**不会写 xml**，
    # 于是我们解析到的是上一次留下的旧成绩单 —— 表现为"明明是失败用例，却显示 passed"（假绿）。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    xml_path = out_dir / f"junit-{stamp}-{uniq}.xml"
    if xml_path.exists():          # 双保险：绝不复用旧文件
        xml_path.unlink()

    cmd = [
        sys.executable, "-m", "pytest", str(path),
        f"--junitxml={xml_path}",
        "-p", "no:cacheprovider",     # 不写 .pytest_cache（避免权限问题）
        "-q",
        *extra_args,
    ]

    started = time.perf_counter()
    try:
        # stdout/stderr 都丢给 DEVNULL：不要管道（本机受限环境下管道子进程会被拒）
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RunResult(returncode=-2, cases=(), xml_path=None,
                         duration=time.perf_counter() - started,
                         error=f"执行超时（>{timeout:.0f}s）：被测服务可能卡住或用例等待过久")
    except OSError as exc:
        return RunResult(returncode=-3, cases=(), xml_path=None,
                         duration=time.perf_counter() - started,
                         error=f"无法启动 pytest：{type(exc).__name__}: {exc}")

    duration = time.perf_counter() - started
    if not xml_path.exists():
        return RunResult(returncode=-4, cases=(), xml_path=None, duration=duration,
                         error="pytest 没有产出 junitxml（可能在收集阶段就崩了，例如 import 错误）")

    cases = parse_junitxml(xml_path)
    return RunResult(returncode=0, cases=cases, xml_path=xml_path, duration=duration)


# ---------------------------------------------------------------------------
# 命令行入口：跑一批测试，看结果（可选同时分流）
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.runner --path evals/generated [--triage]"""
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="执行测试并（可选）分流失败原因")
    parser.add_argument("--path", default="evals/generated", help="测试文件或目录")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--triage", action="store_true", help="同时输出失败分类")
    parser.add_argument("--show-detail", action="store_true", help="打印失败 traceback 末尾")
    args = parser.parse_args(argv)

    run = run_tests(args.path, timeout=args.timeout)
    print("=" * 70)
    print(f" 执行：{args.path}")
    print("=" * 70)
    if run.error:
        print(f"❗ {run.error}")
        return 1

    print(run.table())
    print("-" * 70)
    print(f" 合计 {len(run.cases)} 个：通过 {run.passed} · 失败 {run.count(FAILED)} · "
          f"错误 {run.count(ERROR)} · 跳过 {run.count(SKIPPED)} · 耗时 {run.duration:.1f}s")
    if run.xml_path:
        print(f" 成绩单：{run.xml_path}")

    if args.show_detail:
        for c in run.failures:
            print(f"\n--- {c.name} ---\n{c.message}\n{c.detail[-800:]}")

    if args.triage:
        from . import triage   # 延迟导入，避免循环依赖

        report = triage.classify_run(run)
        print("\n" + report.render())

    return 0 if run.all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
