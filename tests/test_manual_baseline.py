"""第 2 晚 · 手工基线测试（人写的 3 条，用来当"标准答案"）

=====================================================================
本模块职责
=====================================================================
用手写（不是 AI 生成）的 3 条测试，验证被测服务的核心契约。

=====================================================================
在整个系统里的位置
=====================================================================
    这是 baseline（基线）：**先有人写的正确答案，才有资格谈"AI 生成的能不能信"**。

    没有基线的话，AI 生成的测试跑绿了，你不知道是因为"接口好"
    还是因为"AI 写的断言太弱"。基线就是那把尺子。

=====================================================================
为什么这 3 条测试能"换项目就用"
=====================================================================
它们不写死 URL、不写死字段，而是**从契约（target json）里读**：
    - 换成 java-cms.json → 同一个文件立刻能测 Java 项目
    - 找不到对应接口 id → skip 并说明，而不是报错

这就是"契约驱动"的好处：**测试逻辑不变，只有配置变。**
=====================================================================
"""
from __future__ import annotations

import os

import pytest

from selfheal.http import request_json
from selfheal.targets import Target, load_target, probe

# 用环境变量切换被测项目：默认 RAG 后端，测 Java 时
#   $env:SELFHEAL_TARGET="java-cms"
TARGET_NAME = os.getenv("SELFHEAL_TARGET", "rag-backend")

pytestmark = pytest.mark.baseline


# ---------------------------------------------------------------------------
# 夹具：读契约 + 探活。探活失败就 skip（不是 fail）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def target() -> Target:
    return load_target(TARGET_NAME)


@pytest.fixture(scope="session")
def alive(target: Target) -> Target:
    """服务不可达时跳过整套基线测试。

    为什么 skip 而不是 fail：
    "服务没启动"不是测试用例的错，也不是接口的错。
    把它标成失败，会污染通过率统计——而通过率是我们要拿去写进简历的数字。
    （对应架构里的分流类别①：服务不可达 → 一个字节都不许改）
    """
    result = probe(target, retries=2)
    if not result.ok:
        pytest.skip(f"被测服务不可达，跳过基线：{result.detail}")
    return target


def _endpoint(target: Target, endpoint_id: str):
    """从契约里取接口；这个项目没有这个接口就跳过（方便换项目）。"""
    ep = target.endpoint(endpoint_id)
    if ep is None:
        pytest.skip(f"契约 {target.name} 里没有接口 '{endpoint_id}'，跳过")
    return ep


# ---------------------------------------------------------------------------
# 基线 1：健康检查 —— 服务活着，且形态正确
# ---------------------------------------------------------------------------
def test_health_contract(alive: Target):
    """GET /api/health：状态码对、必填字段齐、关键字段值合理。

    这是最便宜的一条：不调模型、不花钱、毫秒级。
    它失败只有两种可能：服务挂了，或者接口被改坏了。
    """
    ep = _endpoint(alive, "health")
    r = request_json(alive, ep)

    assert not r.network_failed, f"请求失败：{r.error}"
    assert r.status == ep.expect_status, f"状态码 {r.status} != 契约 {ep.expect_status}"

    missing = r.missing_fields(ep.expect_fields)
    assert not missing, f"响应缺少契约要求的字段：{missing}（实际字段 {sorted(r.field_names())}）"

    # 值层面的检查：状态必须是 ok（契约里 notes 写明了这一点）
    assert r.json_body["status"] == "ok", f"status 应为 ok，实际 {r.json_body['status']!r}"
    assert r.json_body["chunk_count"] > 0, "chunk_count 应大于 0（语料没入库？）"


# ---------------------------------------------------------------------------
# 基线 2：核心接口 —— 答案必须带合法出处
# ---------------------------------------------------------------------------
def test_ask_returns_citable_answer(alive: Target):
    """POST /api/ask：语料内问题必须答得出来，且引用合法。

    这条是整个项目的"核心验收"：
    - 答案非空（不能答了个寂寞）
    - 出处非空（可溯源）
    - citation_valid 为 true（引用编号不越界）
    - 不是拒答（这问题明明在语料里）
    """
    ep = _endpoint(alive, "ask")
    r = request_json(alive, ep)

    assert not r.network_failed, f"请求失败：{r.error}"
    assert r.status == ep.expect_status, f"状态码 {r.status} != 契约 {ep.expect_status}"
    assert r.json_body is not None, f"响应不是 JSON：{r.text[:200]}"

    body = r.json_body
    missing = r.missing_fields(ep.expect_fields)
    assert not missing, f"响应缺少字段：{missing}"

    assert body["refused"] is False, f"语料内的问题被误拒答了：{body.get('answer')!r}"
    assert body["answer"].strip(), "答案是空的"

    citations = body["citations"]
    assert isinstance(citations, list) and citations, "没有出处（可溯源这条验收失败）"
    assert body["citation_valid"] is True, "引用校验未通过（编号越界或缺失）"
    # 每条出处都要能被追溯：编号 + 文档 + 块
    for c in citations:
        assert {"index", "doc_id", "chunk_id"} <= set(c), f"出处字段不全：{c}"


# ---------------------------------------------------------------------------
# 基线 3：边界 —— 空问题必须 400，不能 200 也不能 500
# ---------------------------------------------------------------------------
def test_empty_question_returns_400(alive: Target):
    """POST /api/ask（空问题）：必须是 400 + error 字段。

    为什么要测边界：
    "正常情况能用"是及格线；**"异常输入不崩"才是工程能力**。
    空字符串、纯空格、超长文本、缺少字段——这些是线上最常见的输入。
    """
    ep = _endpoint(alive, "ask-empty")
    r = request_json(alive, ep)

    assert not r.network_failed, f"请求失败：{r.error}"
    assert r.status == ep.expect_status, (
        f"空问题应返回 {ep.expect_status}，实际 {r.status}"
        f"（200=没校验，500=崩了，都不对）"
    )
    assert r.json_body is not None, "错误响应也应该是 JSON"
    assert "error" in r.json_body, f"错误响应缺少 error 字段：{r.json_body}"
