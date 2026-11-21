"""自动生成的测试用例（由 selfheal.generator 生成，请勿手改）。

文件头（import / TARGET / api 助手 / check_contract）由模板固定，模型只填测试函数。
"""

import os

from selfheal.http import request_json
from selfheal.targets import load_target

TARGET = load_target(os.getenv("SELFHEAL_TARGET", "demo-buggy"))


def api(endpoint_id):
    """按契约里的接口 id 发一次请求，返回 (endpoint, HttpResult)。

    测试里**不许自己拼 URL**——URL 只能来自 target 契约。
    登录态（token）由 request_json 自动注入，用例不用管。
    """
    endpoint = TARGET.endpoint(endpoint_id)
    assert endpoint is not None, f"契约里没有接口 {endpoint_id}"
    return endpoint, request_json(TARGET, endpoint)


def check_contract(endpoint, r):
    """通用契约断言：状态码 → 必填字段 → 期望字段值。

    为什么把它放进模板（而不是让模型每次自己写）：
    这三条是**每条用例都要做的机械检查**，让模型重复写只会写漏、写错。
    和"模板头由我们写死"是同一个道理：不变的交给代码，变化的交给模型。
    """
    assert r.status == endpoint.expect_status, (
        f"状态码 {r.status} != 契约 {endpoint.expect_status}"
    )
    missing = r.missing_fields(endpoint.expect_fields)
    assert not missing, f"响应缺字段 {missing}"
    bad = r.json_mismatches(endpoint.expect_json)
    assert not bad, f"响应字段值与契约不符：{bad}"

# 覆盖接口：items, order-detail, order-total
def test_items_list_normal_flow():
    """覆盖商品列表正常流程：FR-1 商品列表接口应返回 200 且 code=200。"""
    endpoint, r = api("items")
    check_contract(endpoint, r)
    body = r.json_body
    assert body["code"] == 200
    assert body["msg"] is not None
    assert body["data"] is not None


def test_order_detail_not_found_returns_404_not_500():
    """覆盖订单不存在异常：FR-3 42 号订单不存在时必须返回 404，不得 500。"""
    endpoint, r = api("order-detail")
    check_contract(endpoint, r)
    body = r.json_body
    assert body["code"] == 404
    assert body["msg"] == "订单不存在"
    assert body.get("data") is None


def test_order_total_must_equal_unit_price_times_quantity():
    """覆盖订单总价计算：FR-4 41 号订单 2 件单价 399，total 必须为 798。"""
    endpoint, r = api("order-total")
    check_contract(endpoint, r)
    body = r.json_body
    assert body["code"] == 200
    assert body["data"]["order_id"] == 41
    assert body["data"]["total"] == 798
    assert body["data"]["total"] == 399 * 2
