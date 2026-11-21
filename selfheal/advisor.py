"""M11 · AI 辅助归因（advisor.py）—— 让模型只在"规则判不了"的地方出场

=====================================================================
本模块职责
=====================================================================
规则表（triage.py）判不出责任归属时，让模型读一遍失败消息与 traceback，
给出一份**仅供参考的判断**：属于哪一类、依据是什么、建议怎么处理。

=====================================================================
它为什么不推翻"能确定性判定的不问模型"
=====================================================================
因为它是**兜底**，不是主路径：

    规则命中  → 立刻返回，模型一步都不介入（99% 的失败都走这条路）
    规则判不了 → 才问模型，而且**只出一条建议**，不产生任何动作

模型在这里**没有任何权限**：它不能改测试、不能改契约、不能决定重跑。
它产出的是一段给人看的文字，进报告，仅此而已。
这也是为什么它不会破坏"可复现"：规则层的结论永远不依赖模型，
把模型关掉，系统行为完全不变，只是报告少一段建议。

=====================================================================
它专门补的那一类
=====================================================================
`契约与实现不一致`（HTTP 200 但数据不符）：可能是服务错、契约错、用例错，
三者只有人能分清 —— 这正是模型擅长、规则无能为力的地方。

=====================================================================
调用时机
=====================================================================
只对那些动作为 MANUAL / HOLD 的失败调用。一次运行通常 0~2 次调用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import llm_client
from .runner import CaseResult
from .triage import Action, Verdict

# 允许模型回答的类别（故意与 FailureKind 的值对齐，方便直接展示）
ADVICE_KINDS = (
    "服务端缺陷",
    "契约写错",
    "用例写错",
    "环境问题",
    "无法判断",
)

ADVISOR_PROMPT = """你是测试负责人。下面是一条失败的接口自动化用例，规则引擎没能判定责任归属。
请你判断"到底是谁的问题"，只输出一个 JSON 对象，不要任何解释文字。

可选的责任判定（字段 responsibility 必须取其中之一）：
- "服务端缺陷"：服务返回的数据确实不符合需求/契约，测试写得对
- "契约写错"：契约里的期望值本身就不对（业务规则并非如此）
- "用例写错"：断言方式/取值路径写错了，虽然契约是对的
- "环境问题"：服务没起、依赖缺失、超时、鉴权失效等外部原因
- "无法判断"：信息不足以判断

输出示例：
{"responsibility": "服务端缺陷", "reason": "契约要求 total=2，服务返回 0，且需求文档写明 total 应为订单数量之和", "suggestion": "按缺陷提单，不要去改测试"}

要求：
1. reason 必须引用报错里的具体内容，不要空泛；
2. suggestion 是给**人**的建议，不要假设你能改代码；
3. 只输出 JSON。
"""


@dataclass
class Advice:
    """一条 AI 建议。注意：它**不产生动作**，只是给人看的文字。"""

    nodeid: str
    responsibility: str = ""      # 见 ADVICE_KINDS
    reason: str = ""
    suggestion: str = ""
    ok: bool = False              # 是否成功拿到建议
    error: str = ""               # 失败原因（网络/解析）
    raw_tail: str = ""            # 模型原始回复尾部（排错用）

    def line(self) -> str:
        if not self.ok:
            return f"[AI 未给出建议] {self.error}"
        return f"[AI 判断] {self.responsibility} —— {self.reason}"


def should_consult(verdict: Verdict) -> bool:
    """只有"规则判不了 / 判出来也不能自动改"的才值得花一次模型调用。"""
    return verdict.action in (Action.MANUAL, Action.HOLD)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("回复里没有 JSON")
    return json.loads(m.group(0))


def build_messages(case: CaseResult, verdict: Verdict) -> list[dict]:
    detail = (case.detail or "")[-2000:]        # traceback 截尾，够用且省 token
    return [
        {"role": "system", "content": ADVISOR_PROMPT},
        {
            "role": "user",
            "content": (
                f"用例：{case.name}\n"
                f"规则引擎的初步判定：{verdict.kind.value}（动作 {verdict.action.value}）\n"
                f"规则给出的原因：{verdict.reason}\n\n"
                f"失败消息：\n{case.message or '（无）'}\n\n"
                f"traceback：\n{detail or '（无）'}"
            ),
        },
    ]


def advise(case: CaseResult, verdict: Verdict) -> Advice:
    """问模型"这是谁的问题"。任何失败都返回一个 Advice，不抛异常。"""
    try:
        raw = llm_client.chat_for("judge", build_messages(case, verdict), max_tokens=1200)
    except Exception as exc:                       # noqa: BLE001 建议失败不该影响主流程
        return Advice(nodeid=case.nodeid, ok=False, error=f"{type(exc).__name__}: {exc}")

    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return Advice(nodeid=case.nodeid, ok=False, error=f"解析失败：{exc}", raw_tail=raw[-200:])

    responsibility = str(data.get("responsibility", "")).strip()
    if responsibility not in ADVICE_KINDS:
        # 模型给了个不在选项里的词：保留原文但标记为"无法判断"，别让报告出现奇怪措辞
        responsibility = "无法判断"
    return Advice(
        nodeid=case.nodeid,
        responsibility=responsibility,
        reason=str(data.get("reason", "")).strip(),
        suggestion=str(data.get("suggestion", "")).strip(),
        ok=True,
        raw_tail=raw[-200:],
    )
