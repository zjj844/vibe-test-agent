"""M12 · 契约引导生成（bootstrap.py）—— 让 AI 读源码，起草契约

=====================================================================
本模块解决哪一步的重复劳动
=====================================================================
之前"加一个被测项目"要手写 60 行 JSON：把 5 个 Controller 里的
`@RequestMapping` 一条条抄成 `path`，还要想 id、想 notes。**这是纯体力活。**

这个模块把它变成：把源码（或 Swagger JSON）丢进来 → 模型起草契约 →
代码校验形状 → 人看一眼点保存。

=====================================================================
三条纪律（和 generator 完全一致）
=====================================================================
1. **模型只起草**：它输出的是 JSON 草案，**永远不自动落盘**。
   契约是"人签字"的东西 —— 签错一次，后面所有用例都按错的标准测。

2. **代码验形状**：草案合不合格，不靠模型自评，而是直接喂给
   `targets.parse_target()` —— **用加载器当校验器**。
   这样"什么算合法契约"只有一份定义，模型输出不合格就把报错原文回喂重试一次。
   （如果另写一套校验，两份规则迟早会打架。）

3. **语言无关**：不写 Java/Python 解析器，因为"模型读代码"这件事本身与语言无关。
   实测一次调用 10~20 秒，比写两套 AST 解析器便宜两个数量级，
   而且 C#、Go、Spring、Flask 都能用。

=====================================================================
它不做什么（很重要）
=====================================================================
- 不探测被测服务是否可达（那是 probe 的事）
- 不发明期望值（`expect_status` 默认 200，只有源码明确写了才取别的）
- 不写需求文档（那要人给，或者从 PRD 里粘）
- 不覆盖已有契约（要覆盖必须显式 force）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import llm_client, targets

# 单次喂给模型的源码上限（字符）。超了截断并告知模型，避免把上下文撑爆。
MAX_SOURCE_CHARS = 60_000

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

BOOTSTRAP_PROMPT = """你是资深测试开发工程师。下面是某被测服务的**接口源码**（或接口文档片段），
请从中提取出一份接口契约草案，供自动化测试工具使用。

只输出一个 JSON 对象，结构必须严格如下（字段名不许改）：

{{
  "name": "{name}",
  "base_url": "{base_url}",
  "health": {{"id": "<最适合做健康检查的接口 id>"}},
  "endpoints": [
    {{
      "id": "小写短横线命名的唯一标识，如 order-detail",
      "method": "GET|POST|PUT|DELETE",
      "path": "/api/xxx（严格来自源码，含路径变量时用具体值，如 /api/orders/42）",
      "expect_status": 200,
      "expect_fields": ["响应体顶层必有字段"],
      "notes": "这个接口的业务含义与关键约束（一句话，中文）"
    }}
  ]
}}

硬性要求：
1. **path 必须严格来自源码**里的映射（@RequestMapping/@GetMapping/@PostMapping、@app.route、[HttpPost] 等）；
   绝对不许编造源码里不存在的路径。
2. 路径变量（如 /orders/{{id}}）要换成**一个具体可用的值**（如 /orders/42）。
3. `expect_status` 除非源码里明确写了别的（如查不到返回 404），一律填 200。
4. `expect_fields` 只填**顶层**字段名（如 code/msg/data），不要填嵌套路径。
5. 需要登录才能访问的接口，在 notes 里写明"需要登录"。
6. `id` 全局唯一，只用小写字母、数字、连字符。
7. 只输出 JSON，不要任何解释文字，不要代码围栏以外的内容。

源码如下：

{source}
"""


@dataclass
class DraftResult:
    """一次起草的结果。注意 contract 只是**草案**，落盘要人确认。"""

    ok: bool
    contract: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)   # 形状不合格的原因
    endpoint_count: int = 0
    repaired: bool = False                                # 是否触发过重试
    raw_tail: str = ""

    def summary(self) -> str:
        if self.ok:
            return (
                f"✅ 起草成功：{self.endpoint_count} 个接口"
                f"{'（含 1 次重试）' if self.repaired else ''}"
            )
        return "❌ 起草未通过校验：" + "；".join(self.violations[:3])


# ---------------------------------------------------------------------------
# 输入整理
# ---------------------------------------------------------------------------
def merge_sources(files: list[dict]) -> str:
    """把多个源码文件拼成一段带文件名的文本。

    为什么保留文件名：模型需要知道"这是哪个 Controller"，
    而且人复核草案时也要能对上原始文件。
    """
    parts: list[str] = []
    budget = MAX_SOURCE_CHARS
    for item in files:
        name = str(item.get("path") or "（未命名）")
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        head = f"\n===== 文件：{name} =====\n"
        chunk = head + content
        if len(chunk) > budget:
            parts.append(chunk[:budget] + "\n…（以下因超出长度上限被截断）")
            budget = 0
            break
        parts.append(chunk)
        budget -= len(chunk)
    return "".join(parts).strip()


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("回复里没有 JSON 对象")
    return json.loads(m.group(0))


def _normalize(contract: dict, name: str, base_url: str) -> dict:
    """把模型的输出"掰"成我们能接受的样子。

    为什么要掰：模型常犯三种小毛病 —— 少 name、base_url 抄错、
    id 里带空格或大写。这些**不影响语义**，机械修正即可；
    真正影响语义的问题（path 编造、id 重复）留给 parse_target 去拒绝。
    """
    out = dict(contract or {})
    out["name"] = name
    out["base_url"] = base_url

    eps = out.get("endpoints")
    if isinstance(eps, list):
        fixed: list[dict] = []
        for idx, ep in enumerate(eps, 1):
            if not isinstance(ep, dict):
                continue
            item = dict(ep)
            raw_id = str(item.get("id") or f"ep-{idx}")
            item["id"] = re.sub(r"[^a-z0-9-]+", "-", raw_id.strip().lower()).strip("-") or f"ep-{idx}"
            item["method"] = str(item.get("method") or "GET").upper()
            if not isinstance(item.get("expect_status"), int):
                item["expect_status"] = 200
            if not isinstance(item.get("expect_fields"), list):
                item["expect_fields"] = []
            fixed.append(item)
        out["endpoints"] = fixed

    # health 指向不存在的接口是最常见的小毛病：直接指向第一个接口。
    # 为什么不算"编造"：health 只是一个探活入口，任何能返回 200 的接口都行，
    # 语义上没有"正确的那一个"，所以机械兜底是合理的。
    ids = [ep["id"] for ep in out.get("endpoints", [])]
    health = out.get("health")
    if ids and (not isinstance(health, dict) or health.get("id") not in ids):
        out["health"] = {"id": ids[0]}
    return out


def validate_contract(contract: dict) -> tuple[bool, list[str]]:
    """用加载器校验草案形状。**这是本模块的核心设计**：不另写校验规则。"""
    try:
        parsed = targets.parse_target(contract, source_file=None)
    except Exception as exc:                                  # noqa: BLE001
        return False, [f"{type(exc).__name__}: {exc}"]
    problems: list[str] = []
    if parsed.requirements_dir is not None:                   # 草案不该自带这个字段
        problems.append("草案里不该出现 requirements_dir（应由人确认后手填）")
    return (not problems), problems


# ---------------------------------------------------------------------------
# 鉴权识别 + 契约体检（实测踩过的坑）
# ---------------------------------------------------------------------------
# 现象（真实发生过）：起草出的 18 个接口 notes 里都写着"需要登录"，
# 但契约里**没有 auth 配置** → 每个接口都拿到 body.code=401 → 用例断言失败
# → 两轮自愈全在改断言 → 最终 gave_up。0% 通过率，看起来像"服务坏了"。
#
# 根因不是模型没识别（它写进 notes 了），而是**起草流程没把"要登录"翻译成配置**。
# 所以这里补两件事：
#   1. detect_auth()：源码里有鉴权痕迹 + 契约里有登录接口 → 自动补 auth 草案
#   2. contract_warnings()：保存前做体检，把"大概率跑不通"的配置问题指出来
_AUTH_HINTS = (
    "Authorization",
    "Bearer",
    "TokenManager",
    "token",
    "JWT",
    "jwt",
    "session",
    "@RequirePermission",
)

_LOGIN_HINTS = ("login", "signin", "sign-in", "auth-login", "token")


def detect_auth(source: str, contract: dict) -> dict | None:
    """判断"这个项目需要登录态吗"，需要就返回 auth 草案。

    保守判断：源码出现 Authorization/Bearer/JWT/TokenManager 等痕迹，
    **且**契约里有像"登录"的接口，才返回配置。宁可漏判，不要瞎猜。
    """
    if not source or not contract:
        return None
    endpoints = contract.get("endpoints") or []
    if not endpoints:
        return None
    if not any(hint in source for hint in _AUTH_HINTS):
        return None

    login_ep = next(
        (ep for ep in endpoints if any(h in ep["id"].lower() for h in _LOGIN_HINTS)), None
    )
    if login_ep is None:
        return None
    return {
        "endpoint": login_ep["id"],
        "token_path": "data.token",
        "header": "Authorization",
        "prefix": "Bearer ",
    }


def contract_warnings(contract: dict) -> list[str]:
    """契约体检：保存前指出"大概率跑不通"的配置问题。

    为什么不阻断保存：契约是人的东西，工具只负责提醒。
    但**不提醒就是坑** —— 用户会跑出 0% 通过率还不知道为什么。
    """
    warns: list[str] = []
    endpoints = contract.get("endpoints") or []
    auth = contract.get("auth")

    need_login = [
        ep for ep in endpoints
        if "需要登录" in (ep.get("notes") or "") or "登录态" in (ep.get("notes") or "")
    ]
    if need_login and not auth:
        warns.append(
            f"{len(need_login)} 个接口的说明里写着「需要登录」，但契约没有 auth 配置 → "
            f"这些接口会拿到 code=401（被当成断言失败，白白消耗自愈轮数）"
        )

    if auth:
        login_id = auth.get("endpoint")
        login_ep = next((ep for ep in endpoints if ep["id"] == login_id), None)
        if login_ep is None:
            warns.append(f"auth.endpoint='{login_id}' 不在接口列表里")
        elif not login_ep.get("body"):
            warns.append(
                f"登录接口 `{login_id}` 没有请求体(body)：裸调用大概率返回 400/500，"
                f'建议填上真实账号密码，如 {{"username": "admin", "password": "123456"}}'
            )
        if not auth.get("token_path"):
            warns.append("auth.token_path 为空：无法从登录响应里取 token（常见值 data.token）")

    health_id = (contract.get("health") or {}).get("id")
    if health_id:
        health_ep = next((ep for ep in endpoints if ep["id"] == health_id), None)
        if health_ep is not None and (health_ep.get("method") or "GET").upper() != "GET":
            warns.append(
                f"health 指向 `{health_id}`（{health_ep.get('method')}）："
                f"探活最好用一个不需要登录、不需要参数的 GET 接口"
            )
    return warns


# ---------------------------------------------------------------------------
# 起草（唯一调模型的地方）
# ---------------------------------------------------------------------------
def build_messages(name: str, base_url: str, source: str) -> list[dict]:
    return [
        {"role": "system", "content": BOOTSTRAP_PROMPT.format(name=name, base_url=base_url, source=source)},
        {"role": "user", "content": f"请为 `{name}`（{base_url}）起草契约，只输出 JSON。"},
    ]


def draft(
    name: str,
    base_url: str,
    files: list[dict],
    max_repair: int = 1,
) -> DraftResult:
    """从源码起草契约。校验不合格时把报错回喂一次。

    name / base_url 由**人**给定并被强制写进草案 —— 不让模型猜。
    """
    if not _NAME_RE.match(name or ""):
        return DraftResult(ok=False, violations=[
            "项目名只能用 小写字母/数字/连字符，且以字母或数字开头（如 my-app）"
        ])
    if not (base_url or "").startswith(("http://", "https://")):
        return DraftResult(ok=False, violations=["base_url 必须以 http:// 或 https:// 开头"])

    source = merge_sources(files)
    if not source:
        return DraftResult(ok=False, violations=["没有读到任何源码内容"])

    messages = build_messages(name, base_url, source)
    violations: list[str] = []
    contract: dict = {}
    repaired = False
    raw = ""

    for attempt in range(max_repair + 1):
        try:
            raw = llm_client.chat_for("gen", messages, max_tokens=6000)
        except Exception as exc:                              # noqa: BLE001
            return DraftResult(ok=False, violations=[f"模型调用失败：{type(exc).__name__}: {exc}"])

        try:
            parsed = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            violations = [f"模型没给出合法 JSON：{exc}"]
        else:
            contract = _normalize(parsed, name, base_url)
            # 起草阶段就把鉴权翻译成配置：这一步是踩过坑之后加的 ——
            # 否则 notes 里写着"需要登录"、契约里却没有 auth，跑起来全是 401。
            if not contract.get("auth"):
                guessed = detect_auth(source, contract)
                if guessed:
                    contract["auth"] = guessed
                    contract["_auth_note"] = (
                        "自动推断：源码里有 Authorization/Bearer 等痕迹。"
                        "请核对 login 接口的 body 是否填了真实账号密码。"
                    )
            ok, violations = validate_contract(contract)
            if ok:
                return DraftResult(
                    ok=True, contract=contract, endpoint_count=len(contract["endpoints"]),
                    repaired=repaired, raw_tail=raw[-300:],
                )

        if attempt >= max_repair:
            break
        repaired = True
        messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "上面的 JSON 有这些问题：\n- " + "\n- ".join(violations)
                + "\n请只修正这些问题，重新输出完整 JSON。",
            },
        ]

    return DraftResult(ok=False, contract=contract, violations=violations,
                       endpoint_count=0, repaired=repaired, raw_tail=raw[-300:])


# ---------------------------------------------------------------------------
# 落盘（人确认后调用）
# ---------------------------------------------------------------------------
def save_contract(contract: dict, config_dir: Path | str | None = None,
                  force: bool = False) -> Path:
    """把**人确认过的**契约写进 configs/。

    拒绝覆盖已有文件（除非 force）——测过的契约被悄悄覆盖，历史报告就对不上了。
    """
    directory = Path(config_dir or targets.CONFIG_DIR)
    ok, problems = validate_contract(contract)
    if not ok:
        raise targets.TargetConfigError("契约不合格，不能保存：" + "；".join(problems))

    name = contract["name"]
    path = directory / f"{name}.json"
    if path.exists() and not force:
        raise FileExistsError(f"契约已存在：{path.name}（要覆盖请显式选择覆盖）")

    directory.mkdir(parents=True, exist_ok=True)
    # 保留可读性：缩进 2、中文不转义 —— 这个文件是给人核对的
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 命令行入口（不经过前端也能用）
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """从源码文件起草契约。

    python -m selfheal.bootstrap --name my-app --base-url http://127.0.0.1:9000 \
        --file src/main/java/com/x/FooController.java --save
    """
    import argparse

    ap = argparse.ArgumentParser(description="从源码起草接口契约（AI 起草 + 代码校验）")
    ap.add_argument("--name", required=True, help="项目名（小写字母/数字/连字符）")
    ap.add_argument("--base-url", required=True, dest="base_url")
    ap.add_argument("--file", action="append", default=[],
                    help="源码文件，可重复；也支持 --file 目录（会读里面所有 .java/.py/.cs）")
    ap.add_argument("--save", action="store_true", help="校验通过后直接写入 configs/")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有契约")
    args = ap.parse_args(argv)

    files: list[dict] = []
    for raw in args.file:
        p = Path(raw)
        candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in candidates:
            if f.is_file() and f.suffix in {".java", ".py", ".cs", ".js", ".ts", ".go"}:
                try:
                    files.append({"path": str(f), "content": f.read_text(encoding="utf-8", errors="replace")})
                except OSError as exc:
                    print(f"跳过 {f}：{exc}")

    result = draft(args.name, args.base_url, files)
    print(result.summary())
    if result.ok:
        print(json.dumps(result.contract, ensure_ascii=False, indent=2))
        if args.save:
            try:
                path = save_contract(result.contract, force=args.force)
                print(f"\n已写入：{path}")
            except (FileExistsError, targets.TargetConfigError) as exc:
                print(f"\n未写入：{exc}")
                return 1
    for v in result.violations:
        print(f"  · {v}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
