"""统一的 LLM 调用层：一个 chat() 函数，所有"要模型说话"的地方都走它。

为什么要抽这一层（而不是每个模块自己写 urllib）：
1. 重试、代理、307 重定向、4xx 立刻失败 —— 这些坑只踩一次（见 EVAL.md 坑 5/6）；
2. 换模型/换网关只改这里；
3. 测试里可以把它替换掉，不需要联网。

这里只做"一问一答"，不做 RAG、不做业务 —— 业务在 qa_agent / rag 里。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .env import urllib_opener

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def available() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def model_name() -> str:
    return os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL


def base_url() -> str:
    return (os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def role_model(role: str = "gen") -> str:
    """按用途选模型（可用环境变量覆盖）。

    实测依据（scripts/check_models.py + 生成实测）：
    - 本账号两个模型都是推理模型，会先"思考"再输出；
    - 同一生成任务：flash 思考 1447 token / 13 秒；v4-pro 思考 4425 token / 61 秒；
    - 所以"批量生成"这类任务用 flash，复杂评审再上 pro。
    """
    if role == "judge":
        return os.getenv("QA_JUDGE_MODEL") or os.getenv("DEEPSEEK_FAST_MODEL") or "deepseek-flash"
    return os.getenv("QA_GEN_MODEL") or os.getenv("DEEPSEEK_FAST_MODEL") or "deepseek-flash"


def chat_for(role: str, messages: list[dict], max_tokens: int = 8000) -> str:
    """按角色选模型 + 账号没有该模型时自动回退到 DEEPSEEK_MODEL。"""
    preferred = role_model(role)
    try:
        return chat(messages, model=preferred, max_tokens=max_tokens)
    except RuntimeError as exc:
        if "HTTP 4" in str(exc) and preferred != model_name():
            # 账号没开放这个模型：回退到默认模型，并把预算放大（推理模型吃 token）
            return chat(messages, model=model_name(), max_tokens=max(max_tokens, 16000))
        raise


def chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 6000,
    retries: int = 3,
    backoff: float = 1.0,
) -> str:
    """发一次对话请求，返回模型回复的纯文本。

    temperature 默认 0：评估/生成类任务要可比较，别让它自由发挥。

    ⚠️ max_tokens 默认给 6000 的原因：本机账号的两个模型（deepseek-flash /
    deepseek-v4-pro）**都是推理模型**，会先输出 reasoning_content 思考内容。
    实测 max_tokens=2500 时思考就把预算吃光了，正文返回空字符串，
    报错只显示"模型没有返回任何代码"——极难排查（详见 scripts/check_models.py）。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY（写进 .env 或环境变量）")

    payload = {
        "model": model or model_name(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    url = f"{base_url()}/chat/completions"

    # 分类重试：4xx 是"你写错了"（key/model/参数），重试没意义；其余指数退避
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
        try:
            with urllib_opener().open(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                # 把"空正文"翻译成人能看懂的话——这是推理模型最坑的失败模式
                finish = choice.get("finish_reason")
                usage = data.get("usage") or {}
                reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                if finish == "length":
                    raise RuntimeError(
                        f"模型正文为空：max_tokens={max_tokens} 被推理过程吃光"
                        f"（reasoning_tokens={reasoning}）。请调大 max_tokens，或换非推理模型。"
                    )
                raise RuntimeError(f"模型返回空内容（finish_reason={finish}）")
            return content
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:200]
            if 400 <= exc.code < 500:
                raise RuntimeError(f"LLM 返回 HTTP {exc.code}：{detail}") from exc
            location = exc.headers.get("Location") if 300 <= exc.code < 400 else None
            if location:
                url = urllib.parse.urljoin(url, location)
                last_error = RuntimeError(f"HTTP {exc.code} → 跟随重定向到 {url}")
            else:
                last_error = RuntimeError(f"HTTP {exc.code}：{detail}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(backoff * (2 ** (attempt - 1)))
    raise RuntimeError(f"LLM 调用失败（已重试 {retries} 次）：{last_error}")
