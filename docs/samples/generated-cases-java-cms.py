"""自动生成的测试用例（由 selfheal.generator 生成，请勿手改）。

文件头（import / TARGET / api 助手 / check_contract）由模板固定，模型只填测试函数。
"""

import os

from selfheal.http import request_json
from selfheal.targets import load_target

TARGET = load_target(os.getenv("SELFHEAL_TARGET", "java-cms"))


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

# 覆盖接口：login, category-tree, category-tree-anon
def test_login_success_returns_token_and_user():
    """REQ-001 鉴权与登录（正常流程）：种子账号 admin/123456 登录应返回 code=200，并下发 token 与 user。"""
    endpoint, r = api("login")
    check_contract(endpoint, r)
    body = r.json_body

    assert body["code"] == 200
    assert "成功" in (body.get("msg") or ""), f"登录成功提示异常: {body.get('msg')!r}"

    data = body.get("data")
    assert isinstance(data, dict), "登录成功的 data 应为对象，包含 token 与 user"

    token = data.get("token")
    assert isinstance(token, str) and token.strip(), "登录成功必须下发非空 token"

    user = data.get("user")
    assert isinstance(user, dict), "登录成功的 data.user 应为对象"
    assert user.get("username") == "admin", f"登录返回的用户身份不符: {user!r}"


def test_category_tree_returns_array_with_children():
    """REQ-002 内容接口 FR-2（边界）：分类树登录可访问，data 必须是数组（空时为空数组而非 null），节点含 id/name/children。"""
    endpoint, r = api("category-tree")
    check_contract(endpoint, r)
    body = r.json_body

    assert body["code"] == 200

    data = body.get("data")
    assert isinstance(data, list), f"分类树 data 必须是数组，不能是 null，实际为 {type(data).__name__}"

    def _assert_nodes(nodes, depth=0):
        for node in nodes:
            assert isinstance(node, dict), f"第 {depth} 层分类节点应为对象: {node!r}"
            assert "id" in node and node["id"] is not None, f"分类节点缺少 id: {node!r}"
            assert isinstance(node.get("name"), str) and node["name"].strip(), f"分类节点 name 非法: {node!r}"
            assert "children" in node, f"分类节点缺少 children 字段: {node!r}"
            children = node["children"]
            assert isinstance(children, list), f"分类节点 children 必须是数组（空则为 []）: {node!r}"
            _assert_nodes(children, depth + 1)

    _assert_nodes(data)


def test_category_tree_anonymous_must_be_rejected():
    """REQ-001 FR-3（异常/鉴权边界）：未登录访问分类树不得执行业务逻辑——HTTP 仍为 200，但 body.code 必须为 401 且 data 为 null。"""
    endpoint, r = api("category-tree-anon")
    check_contract(endpoint, r)
    body = r.json_body

    # 拦截器直接写 JSON，未改状态码：所以必须靠 body.code 判断，而不是靠 HTTP 状态码
    assert r.status == 200, f"未登录拦截为写 JSON 方式，HTTP 状态码应仍为 200，实际 {r.status}"
    assert body["code"] == 401, f"未登录访问分类树必须返回 code=401，实际 {body.get('code')!r}"
    assert "登录" in (body.get("msg") or ""), f"未登录提示语不符: {body.get('msg')!r}"
    assert body.get("data") is None, f"被拒绝时 data 必须为 null，实际 {body.get('data')!r}"
