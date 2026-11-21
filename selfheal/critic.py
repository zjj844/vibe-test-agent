"""③ 用例自评：让模型按固定 rubric 打分，多次采样取中位。

为什么必须"多次采样 + 取中位"：
单次打分对 temperature=0 也可能因为网关/版本差异抖动几十分；
两次采样取中位能把单次异常拉回来（真实模型上实测抖动明显）。

为什么用 rubric 而不是"你觉得这用例好吗"：
自由评价 → 无法比较、无法复现。固定维度和锚点后，分数才有意义。
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field

from . import llm_client

# 5 个维度，每维 1-5 分。锚点写清楚，模型才会给出一致的分。
RUBRIC: dict[str, str] = {
    "可执行性": "1=引用了不存在的接口/选择器；3=能跑但可能需要微调；5=直接可跑",
    "断言明确性": "1=只断言没报错；3=有断言但偏弱（用了 in / 非空）；5=断言了具体文案或数量",
    "需求覆盖": "1=与需求无关；3=覆盖一个需求点；5=覆盖需求中的关键约束（含边界/异常）",
    "独立性": "1=用例之间互相依赖；3=共享前置但可单独跑；5=完全隔离，可任意单跑",
    "可维护性": "1=硬编码魔法值、重复代码；3=可读但可再抽象；5=复用 Page Object、命名清晰",
}

CRITIC_PROMPT = """你是严格的测试评审专家。请给下面这份 pytest 用例打分。

评分维度与锚点：
{rubric}

要求：
1. 每个维度给 1-5 的整数分；
2. 只输出一个 JSON 对象，不要任何解释文字；
3. 字段名必须完全一致，另加一个 "评语" 字段（一句话指出最大的问题）。

输出示例：
{{"可执行性": 4, "断言明确性": 3, "需求覆盖": 4, "独立性": 5, "可维护性": 4, "评语": "边界用例缺失"}}
"""


@dataclass
class ScoreResult:
    scores: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    samples: int = 0
    comment: str = ""
    raw_samples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        dims = "  ".join(f"{k}={v:.1f}" for k, v in self.scores.items())
        return f"总分 {self.total:.2f}/5（{self.samples} 次采样取中位）\n{dims}\n评语：{self.comment}"


def _extract_json(text: str) -> dict:
    """从模型回复里抠出第一个 JSON 对象（容忍前后有废话/代码围栏）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"回复里没有 JSON：{text[:120]}")
    return json.loads(m.group(0))


def score_once(code: str) -> dict:
    rubric_text = "\n".join(f"- {k}：{v}" for k, v in RUBRIC.items())
    messages = [
        {"role": "system", "content": CRITIC_PROMPT.format(rubric=rubric_text)},
        {"role": "user", "content": f"待评分的用例：\n\n```python\n{code}\n```"},
    ]
    raw = llm_client.chat_for("judge", messages, max_tokens=3000)
    data = _extract_json(raw)
    return {"scores": data, "raw": raw}


def score(code: str, samples: int = 2) -> ScoreResult:
    """多次采样，逐维度取中位数。samples<=0 时按 1 次处理。"""
    samples = max(1, samples)
    collected: list[dict] = []
    raws: list[str] = []
    for _ in range(samples):
        try:
            out = score_once(code)
        except (ValueError, RuntimeError, json.JSONDecodeError):
            continue  # 单次采样失败不影响整体（这就是多次采样的容错价值）
        collected.append(out["scores"])
        raws.append(out["raw"])

    if not collected:
        raise RuntimeError("所有采样都失败，请检查网络/key")

    scores: dict[str, float] = {}
    for dim in RUBRIC:
        values = [float(s[dim]) for s in collected if dim in s]
        if values:
            scores[dim] = round(statistics.median(values), 2)

    comments = [s.get("评语", "") for s in collected if s.get("评语")]
    total = round(sum(scores.values()) / len(scores), 2) if scores else 0.0
    return ScoreResult(
        scores=scores, total=total, samples=len(collected),
        comment=comments[0] if comments else "", raw_samples=raws,
    )
