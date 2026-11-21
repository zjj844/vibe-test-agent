"""runner.py + triage.py 的离线测试。

两块的测试策略不同：
- **runner**：真跑 pytest（跑的是本文件里临时写出的小测试文件），
  验证"能解析 junitxml、能把失败信息完整取出来"。
- **triage**：纯函数，用**构造的 CaseResult** 测每一类规则，
  不跑任何东西 —— 因为分流规则必须能确定性复现。
"""
from __future__ import annotations

from selfheal import runner, triage
from selfheal.triage import Action, FailureKind


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
PASSING = '''
def test_ok():
    assert 1 + 1 == 2
'''

FAILING = '''
def test_fail():
    assert 1 + 1 == 3
'''

ERRORING = '''
def test_err():
    raise AttributeError("'HttpResult' object has no attribute 'json'")
'''


def test_run_tests_collects_passed(ws_tmp):
    f = ws_tmp / "test_pass.py"
    f.write_text(PASSING, encoding="utf-8")
    run = runner.run_tests(f, report_dir=ws_tmp / "reports")

    assert run.error == ""
    assert len(run.cases) == 1
    assert run.cases[0].status == "passed"
    assert run.all_green is True


def test_run_tests_captures_failure_message(ws_tmp):
    f = ws_tmp / "test_fail.py"
    f.write_text(FAILING, encoding="utf-8")
    run = runner.run_tests(f, report_dir=ws_tmp / "reports")

    assert run.passed == 0
    assert len(run.failures) == 1
    case = run.failures[0]
    assert case.status == "failed"
    assert "3" in f"{case.message}{case.detail}"          # traceback 里有断言值
    assert "test_fail" in case.name


def test_exception_in_test_body_is_failure_and_triaged_as_generated_code(ws_tmp):
    """测试函数体里抛异常 → junitxml 记成 <failure>（不是 <error>）。

    <error> 只用于 setup/teardown/收集阶段出错。
    这点很重要：分流不能依赖 failed/error 的差别，必须看**错误文本**。
    """
    f = ws_tmp / "test_err.py"
    f.write_text(ERRORING, encoding="utf-8")
    run = runner.run_tests(f, report_dir=ws_tmp / "reports")

    assert run.count("failed") == 1
    case = run.failures[0]
    assert "AttributeError" in f"{case.message}{case.detail}"

    # 关键：异常照样能被分流正确识别为"生成代码问题"
    report = triage.classify_run(run)
    assert report.count(FailureKind.GENERATED_CODE) == 1


def test_run_tests_missing_path(ws_tmp):
    run = runner.run_tests(ws_tmp / "nope", report_dir=ws_tmp / "reports")
    assert run.error and "不存在" in run.error


def test_run_tests_import_error_is_recorded_as_error(ws_tmp):
    """收集阶段崩溃（import 错误）时，pytest 仍会产出 xml，但用例被标成 error。

    （最初我以为是"没有 xml"——实测其实有，只是标签是 <error>。
     这也说明：**别猜外部格式，跑一次看真实文件**。）
    """
    f = ws_tmp / "test_broken.py"
    f.write_text("import not_a_real_module_xyz\n\ndef test_x():\n    pass\n", encoding="utf-8")
    run = runner.run_tests(f, report_dir=ws_tmp / "reports")

    assert run.error == ""                     # 执行本身没崩
    assert run.count("error") == 1             # 而是记录成 error
    case = run.failures[0]
    assert "not_a_real_module_xyz" in f"{case.message}{case.detail}" or "ImportError" in f"{case.message}{case.detail}"


# ---------------------------------------------------------------------------
# triage：每一类都要能确定性命中
# ---------------------------------------------------------------------------
def _case(message: str, detail: str = "", status: str = "failed") -> runner.CaseResult:
    return runner.CaseResult(nodeid="t.py::test_x", name="test_x", status=status,
                             duration=0.1, message=message, detail=detail)


def test_classify_server_unreachable():
    v = triage.classify_case(_case("ConnectionRefusedError: [WinError 10061]"))
    assert v.kind is FailureKind.SERVER_UNREACHABLE
    assert v.action is Action.STOP


def test_classify_system_bug_500():
    v = triage.classify_case(_case("assert 500 == 200", "E assert 500 == 200"))
    assert v.kind is FailureKind.SYSTEM_BUG
    assert v.action is Action.REPORT_DEFECT


def test_classify_generated_code():
    v = triage.classify_case(
        _case("AttributeError: 'HttpResult' object has no attribute 'json'",
              status="error")
    )
    assert v.kind is FailureKind.GENERATED_CODE
    assert v.action is Action.REGENERATE


def test_classify_assertion():
    v = triage.classify_case(_case("AssertionError: 引用校验未通过"))
    assert v.kind is FailureKind.ASSERTION
    assert v.action is Action.FIX


def test_classify_script_bug():
    v = triage.classify_case(_case("KeyError: 'answer'", status="error"))
    assert v.kind is FailureKind.SCRIPT
    assert v.action is Action.FIX


def test_classify_unknown_falls_back_to_manual():
    v = triage.classify_case(_case("some totally new error"))
    assert v.kind is FailureKind.UNKNOWN
    assert v.action is Action.MANUAL


def test_rule_order_unreachable_beats_assertion():
    """顺序很重要：'连接被拒'出现在断言文本里时，仍应判成服务不可达。"""
    v = triage.classify_case(_case("AssertionError: ConnectionError while calling api"))
    assert v.kind is FailureKind.SERVER_UNREACHABLE


def test_report_aggregates_and_flags_stop():
    run = runner.RunResult(
        returncode=0, xml_path=None, duration=1.0,
        cases=(
            _case("ConnectionRefusedError", status="error"),
            _case("AssertionError: x"),
        ),
    )
    report = triage.classify_run(run)
    assert report.must_stop is True
    assert len(report.fixable) == 1
    assert "必须停手" in report.conclusion()
    assert "失败分流" in report.render()


def test_report_defects_are_not_fixable():
    run = runner.RunResult(
        returncode=0, xml_path=None, duration=1.0,
        cases=(_case("HTTP 503 Service Unavailable"),),
    )
    report = triage.classify_run(run)
    assert len(report.defects) == 1
    assert report.fixable == ()
    assert "不许改测试" in report.conclusion()


def test_report_propagates_runner_error():
    run = runner.RunResult(returncode=-2, cases=(), xml_path=None, duration=0.0,
                           error="执行超时")
    report = triage.classify_run(run)
    assert "超时" in report.error
    assert "执行层失败" in report.conclusion()
