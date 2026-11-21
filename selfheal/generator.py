"""M3 · 出题人（generator.py）

=====================================================================
本模块职责
=====================================================================
拿着「接口契约 + 需求片段」，让大模型写出 pytest 测试用例；
落盘之前用**规则**把关：语法对不对、有没有瞎编接口、有没有硬编码 URL。

=====================================================================
在整个系统里的位置
=====================================================================
    main → loop → 【generator】 → critic → runner → triage → report
                     ↓
              target（契约）+ kb（需求）

它调用：llm_client（要模型说话）、targets（知道有哪些接口）、kb（检索需求）
它被调用：loop（主循环）

=====================================================================
三条设计纪律（每一条都是为了让"生成"可被管控）
=====================================================================
1. **只让模型写函数体**：文件头（import + TARGET + api() 助手）是我们写死的模板，
   模型只填 `def test_xxx():`。这样它没有机会发明 import、URL、全局变量。
   —— 这和"约束动作空间"是同一个思路：自由越大，翻车越多。

2. **落盘前跑三道静态检查**（毫秒级，不用真的跑）：
   ① 语法能不能解析
   ② 有没有 `def test_`
   ③ 有没有调用契约里不存在的接口 / 有没有硬编码 URL
   —— 用毫秒级的检查拦掉会浪费 10 秒的失败。

3. **有界自愈：只在"可机器判定"的违规上重试一次**。
   不重试"断言写得好不好"（那属于自评 critic 的事），
   因为那类判断没有唯一答案。

=====================================================================
为什么生成代码要用 `api("接口id")` 而不是自己拼 URL
=====================================================================
因为 url 只有一个来源（target json）。测试代码里不该出现 "http://127.0.0.1:8000"，
否则换个项目（Java CMS，8080）就得改一堆测试——那就不是"换个 json 就能测"了。
"""
from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import llm_client
from .targets import Endpoint, Target

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "evals" / "generated"

# ---------------------------------------------------------------------------
# 我们写死的文件头：模型只负责在后面追加 test 函数
# ---------------------------------------------------------------------------
TEMPLATE_HEADER = '''"""自动生成的测试用例（由 selfheal.generator 生成，请勿手改）。

文件头（import / TARGET / api 助手 / check_contract）由模板固定，模型只填测试函数。
"""

import os

from selfheal.http import request_json
from selfheal.targets import load_target

TARGET = load_target(os.getenv("SELFHEAL_TARGET", "{target_name}"))


def api(endpoint_id):
    """按契约里的接口 id 发一次请求，返回 (endpoint, HttpResult)。

    测试里**不许自己拼 URL**——URL 只能来自 target 契约。
    登录态（token）由 request_json 自动注入，用例不用管。
    """
    endpoint = TARGET.endpoint(endpoint_id)
    assert endpoint is not None, f"契约里没有接口 {{endpoint_id}}"
    return endpoint, request_json(TARGET, endpoint)


def check_contract(endpoint, r):
    """通用契约断言：状态码 → 必填字段 → 期望字段值。

    为什么把它放进模板（而不是让模型每次自己写）：
    这三条是**每条用例都要做的机械检查**，让模型重复写只会写漏、写错。
    和"模板头由我们写死"是同一个道理：不变的交给代码，变化的交给模型。
    """
    assert r.status == endpoint.expect_status, (
        f"状态码 {{r.status}} != 契约 {{endpoint.expect_status}}"
    )
    missing = r.missing_fields(endpoint.expect_fields)
    assert not missing, f"响应缺字段 {{missing}}"
    bad = r.json_mismatches(endpoint.expect_json)
    assert not bad, f"响应字段值与契约不符：{{bad}}"

'''

SYSTEM_PROMPT = """你是一名资深测试开发工程师，正在为一个 HTTP 服务编写 pytest 接口测试。

接口契约（唯一可靠的接口来源，**不许发明契约里没有的接口**）：
{endpoints}

业务需求片段（说明这些接口"应该满足什么"，来自需求文档检索）：
{requirements}

严格要求：
1. 只输出 pytest 测试函数，**不要**输出 import、全局变量或 `api()` / `check_contract()` 助手——它们已经在文件头里了。
2. 每个用例必须调用 `api("接口id")` 拿 `(endpoint, r)`，接口 id 只能取上面列出的那些。
3. **禁止**在代码里出现 http:// 或 https:// 开头的 URL。
4. 每个用例第一行写中文 docstring，注明它覆盖哪条需求（如 "FR-2：..."）。
5. 每条用例开头先调 `check_contract(*api("接口id"))` 做契约断言，再补业务断言；
   不要只断言"没报错"或"非空"。
6. 如果契约里给了"期望响应体"，必须断言这些字段值（用 `r.json_body`）。
7. 标了「不带登录态」的接口是用来验证鉴权边界的：这类用例要断言它**被拒绝**
   （通常是响应体里的 code 表示未登录），而不是断言它成功。
8. 每个用例互相独立，能单独运行。
9. 禁止写 `@pytest.mark.skip`、禁止 `try/except` 吞掉失败、禁止自己处理 token。

请生成 {n} 个用例，覆盖不同的需求点（正常流程、边界、异常各占一些）。
只输出一个 ```python 代码块。
"""


@dataclass
class GenerationResult:
    ok: bool                              # 是否通过全部静态检查
    code: str = ""                        # 生成的测试函数部分（不含模板头）
    case_count: int = 0
    violations: list[str] = field(default_factory=list)
    endpoint_ids: list[str] = field(default_factory=list)
    repaired: bool = False                # 是否触发过重试
    raw_tail: str = ""                    # 模型原始回复结尾（排错用）

    def summary(self) -> str:
        if self.ok:
            return f"✅ 生成 {self.case_count} 个用例，静态检查通过{ '（含 1 次重试）' if self.repaired else '' }"
        return "❌ 生成未通过静态检查：" + "；".join(self.violations[:3])


# ---------------------------------------------------------------------------
# 提示词组装
# ---------------------------------------------------------------------------
def describe_endpoints(target: Target) -> str:
    """把契约写成模型能读懂的清单（含 body / 查询参数 / 期望值 / 鉴权要求）。"""
    lines: list[str] = []
    for ep in target.endpoints:
        body = f" 请求体={ep.body}" if ep.body else ""
        params = f" 查询参数={ep.params}" if ep.params else ""
        fields = ",".join(ep.expect_fields) if ep.expect_fields else "-"
        expect_json = f" 期望响应体={ep.expect_json}" if ep.expect_json else ""
        auth = " 不带登录态（用于验证鉴权边界）" if ep.no_auth else ""
        lines.append(
            f"- id={ep.id} | {ep.method} {ep.path} | 期望状态码={ep.expect_status}"
            f" | 期望字段={fields}{body}{params}{expect_json}{auth}\n"
            f"  业务含义：{ep.notes or '（未填写）'}"
        )
    return "\n".join(lines)


def build_messages(
    target: Target,
    requirements: list[dict],
    n_cases: int = 3,
    feedback: str = "",
) -> list[dict]:
    """组装发给模型的消息。requirements 来自 kb.search()。

    feedback：上一轮跑失败的原因摘要（由 loop 提供）。
    为什么要单独传它、而不是塞进 system prompt：
    它是**每一轮都不同**的增量信息，属于对话内容；塞进系统提示会让"生成规则"
    和"上次错在哪"混在一起，模型更容易把规则也当成可协商的东西。
    而且只喂"可修类"失败（见 loop._feedback_from）——系统缺陷绝不回喂，
    否则等于教模型"改测试让缺陷消失"。
    """
    req_text = (
        "\n\n".join(f"【{c['chunk_id']}】\n{c['text']}" for c in requirements)
        or "（本次未检索到需求片段，只依据契约与业务含义写）"
    )
    system = SYSTEM_PROMPT.format(
        endpoints=describe_endpoints(target), requirements=req_text, n=n_cases
    )
    user = f"请为被测项目 `{target.name}` 生成 {n_cases} 个 pytest 接口测试用例。"
    if feedback:
        user += (
            "\n\n上一轮这批用例跑出来的问题如下（属于测试代码自身的问题，"
            "请在新一版里避免；如果是被测服务本身的 Bug，不要改断言去迁就它）：\n"
            + feedback
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 解析 + 三道静态检查
# ---------------------------------------------------------------------------
def extract_code(text: str) -> str:
    """从模型回复里抠出 python 代码块。"""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def used_endpoint_ids(code: str) -> list[str]:
    """扫出代码里 `api("xxx")` 用到的接口 id（按出现顺序去重）。"""
    return list(dict.fromkeys(re.findall(r"""api\(\s*["']([^"']+)["']""", code)))


# HttpResult 允许被访问的成员（白名单）。
# 为什么需要这份白名单：实测模型会写 `r.json()`——那是 requests.Response 的写法，
# 而我们的 HttpResult 字段叫 `json_body`。这类"凭空发明 API"的错误语法完全合法，
# 只有跑起来才会炸（AttributeError），和当初"属性被写成方法调用"是同一类问题。
_HTTPRESULT_MEMBERS = {
    "status",          # 状态码
    "json_body",       # 解析后的 JSON（dict 或 None）★ 最容易被写成 .json()
    "text",            # 原始响应文本
    "latency_ms",      # 耗时
    "error",           # 网络层错误描述
    "network_failed",  # 是否网络层失败
    "field_names",     # 方法：响应字段名集合
    "missing_fields",  # 方法：缺失的契约字段
    "summary",         # 方法：一行摘要
}


def check_httpresult_usage(code: str) -> list[str]:
    """检查有没有用到 HttpResult 上不存在的成员。"""
    used = set(re.findall(r"\br\.(\w+)", code))
    unknown = sorted(used - _HTTPRESULT_MEMBERS)
    problems: list[str] = []
    if unknown:
        problems.append(
            f"HttpResult 没有这些成员：{unknown}（可用：{sorted(_HTTPRESULT_MEMBERS)}）"
        )
    if re.search(r"\.json\s*\(", code):
        problems.append("用了 r.json()：HttpResult 的字段叫 json_body（不是 requests.Response）")
    return problems


def validate_code(code: str, target: Target) -> tuple[bool, list[str], int]:
    """四道静态检查。返回 (是否通过, 违规列表, 用例数)。

    这四道检查都是**规则**，不是模型判断——所以结果可复现、可写单测。
    """
    violations: list[str] = []

    # ① 语法
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, [f"语法错误：{exc.msg}（第 {exc.lineno} 行）"], 0

    # ② 形式上必须有测试函数
    cases = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    if not cases:
        violations.append("没有找到 def test_* 用例函数")
        return False, violations, 0

    # ③ 契约：只准调用契约里存在的接口 id
    known = {ep.id for ep in target.endpoints}
    used = used_endpoint_ids(code)
    unknown = [i for i in used if i not in known]
    if unknown:
        violations.append(f"调用了契约里不存在的接口 id：{unknown}（可用：{sorted(known)}）")
    if not used:
        violations.append("没有任何 api(...) 调用——用例没有真正发请求")

    # ④ HttpResult 的成员白名单（防止模型凭空发明 API）
    violations.extend(check_httpresult_usage(code))

    # ⑤ 禁止硬编码 URL（URL 只能来自契约）
    if re.search(r"https?://", code):
        violations.append("代码里出现了硬编码 URL，应通过 api(接口id) 发请求")

    # ⑥ 禁止吞异常 / 跳过
    if re.search(r"\bexcept\b", code):
        violations.append("出现 try/except：会吞掉真实失败，禁止")
    if re.search(r"pytest\.mark\.skip|skip\(", code):
        violations.append("出现 skip：会把未通过伪装成跳过，禁止")

    return (not violations), violations, len(cases)


# ---------------------------------------------------------------------------
# 生成（含 1 次有界重试）
# ---------------------------------------------------------------------------
def generate(
    target: Target,
    requirements: list[dict] | None = None,
    n_cases: int = 3,
    max_repair: int = 1,
    feedback: str = "",
) -> GenerationResult:
    """生成测试代码。

    max_repair=1 是刻意的上限，理由同架构文档：
    只在"静态检查能判定"的违规上重试；重试次数越多，模型越会为了通过检查而敷衍。

    feedback 由自愈主循环（loop.py）在第二轮传入：上一轮跑失败的原因摘要。
    注意这跟 max_repair 是两件事：
      · max_repair —— **同一轮内**因静态检查不过而重写（秒级，不执行）
      · feedback   —— **跨轮**因实际跑失败而重写（分钟级，跑过了）
    """
    messages = build_messages(target, requirements or [], n_cases, feedback)
    violations: list[str] = []
    code = ""
    repaired = False
    raw = ""

    for attempt in range(max_repair + 1):
        raw = llm_client.chat_for("gen", messages, max_tokens=8000)
        code = extract_code(raw)
        ok, violations, count = validate_code(code, target)
        if ok:
            return GenerationResult(
                ok=True, code=code, case_count=count,
                endpoint_ids=used_endpoint_ids(code), repaired=repaired,
                raw_tail=raw[-300:],
            )
        if attempt >= max_repair:
            break
        # 把违规原因回喂给它，要求只改这些地方
        repaired = True
        messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "上面的代码有这些问题：\n- " + "\n- ".join(violations)
                + "\n请只修正这些问题，重新输出完整的测试函数代码块。",
            },
        ]

    return GenerationResult(
        ok=False, code=code, case_count=0, violations=violations,
        endpoint_ids=used_endpoint_ids(code), repaired=repaired, raw_tail=raw[-300:],
    )


# ---------------------------------------------------------------------------
# 落盘：模板头 + 模型写的函数
# ---------------------------------------------------------------------------
ARCHIVE_DIR = "_archive"

# 归档目录里的 conftest：告诉 pytest"别收集 _archive 里的文件"。
# 为什么需要它：`pytest evals/generated` 会**递归**收集子目录，
# 光把旧文件挪进 _archive/ 是不够的——它们照样会被执行，于是历史 bug 反复报红。
# 这是 pytest 的收集控制机制（collect_ignore_glob），比"换个目录名"更明确。
_ARCHIVE_CONFTEST = '''"""自动生成：禁止 pytest 收集本目录（这里是历史批次）。"""

collect_ignore_glob = ["*_archive/*", "_archive/*"]
'''


def _ensure_archive_conftest(out: Path) -> None:
    """确保产物目录有一份 conftest，让 pytest 跳过归档目录。"""
    conftest = out / "conftest.py"
    if not conftest.exists():
        conftest.write_text(_ARCHIVE_CONFTEST, encoding="utf-8")


def archive_previous(out_dir: Path | str = DEFAULT_OUT_DIR) -> list[Path]:
    """把上一批生成的文件挪进 `_archive/`，让 `evals/generated/` 里**只剩当前批次**。

    为什么必须做这件事（实测踩到）：
    你在终端很自然会敲 `pytest evals/generated`。如果历史批次留在原地，
    上一版带 bug 的文件也会被一起执行，于是出现"3 failed, 6 passed"——
    而失败的原因早就修好了，只是旧文件还躺在那儿。
    → **产物目录必须只有"当前一批"**，历史进 `_archive/`（保留证据，但不再执行）。
    """
    out = Path(out_dir)
    old = sorted(out.glob("test_generated_*.py"))
    if not old:
        return []
    dest = out / ARCHIVE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for src in old:
        target = dest / src.name
        if target.exists():                      # 同一秒生成过同名文件：加序号
            target = dest / f"{src.stem}-{time.strftime('%f')}{src.suffix}"
        src.replace(target)
        moved.append(target)
    return moved


def save(result: GenerationResult, target: Target, out_dir: Path | str = DEFAULT_OUT_DIR, tag: str = "") -> Path:
    """写出一个**完整可运行**的测试文件。

    注意：模板头是我们写的，模型只贡献函数部分——所以生成文件永远有正确的 import。
    落盘前会把上一批挪进 `_archive/`（见 archive_previous 的注释）。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _ensure_archive_conftest(out)
    archive_previous(out)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_tag = re.sub(r"\W+", "-", tag)[:20].strip("-")
    name = f"test_generated_{stamp}{('-' + safe_tag) if safe_tag else ''}.py"
    path = out / name

    header = TEMPLATE_HEADER.format(target_name=target.name)
    note = f"# 覆盖接口：{', '.join(result.endpoint_ids) or '（无）'}\n"
    path.write_text(header + note + result.code + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 命令行入口（第 3 晚先给一个能亲眼看到结果的入口；主循环 loop.py 之后再接）
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """python -m selfheal.generator --target rag-backend --n 3 --save

    做的事：读契约 → 检索需求 → 生成 → 静态检查 → （落盘）→ 自评
    """
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from . import critic
    from .kb import RequirementKB
    from .targets import load_target, probe

    parser = argparse.ArgumentParser(description="生成测试用例（并自评）")
    parser.add_argument("--target", default="rag-backend")
    parser.add_argument("--n", type=int, default=3, help="生成用例数量")
    parser.add_argument("--query", default="提问与出处", help="需求检索使用的查询")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--save", action="store_true", help="落盘到 evals/generated/")
    parser.add_argument("--no-critic", action="store_true", help="跳过自评（省 token）")
    args = parser.parse_args(argv)

    target = load_target(args.target)
    print(f"[1/5] 契约：{target.name}（{len(target.endpoints)} 个接口）")

    p = probe(target, retries=1)
    if not p.ok:
        print(f"[1/5] 探活失败：{p.detail}")
        print("      → 期望行为：此时不应生成/执行任何测试，直接停手。")
        return 1
    print(f"[1/5] 探活：{p.line()}")

    kb = RequirementKB()
    evidence = kb.search(args.query, top_k=args.top_k)
    print(f"[2/5] 需求检索：命中 {len(evidence)} 个片段")
    for e in evidence:
        print(f"      - {e['chunk_id']}  score={e['score']}")

    print(f"[3/5] 生成中（{args.n} 个用例，模型={llm_client.role_model('gen')}）…")
    result = generate(target, evidence, n_cases=args.n)
    print(f"[4/5] {result.summary()}")
    if not result.ok:
        for v in result.violations:
            print(f"      ✗ {v}")
        return 1
    print(f"      覆盖接口：{result.endpoint_ids}")

    if args.save:
        path = save(result, target, tag=args.query)
        print(f"[5/5] 已落盘：{path}")

    if not args.no_critic:
        print("[5/5] 自评（2 次采样取中位）…")
        scored = critic.score(result.code, samples=2)
        print("      " + scored.summary().replace("\n", "\n      "))

    print("\n---- 生成的代码 ----")
    print(result.code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

