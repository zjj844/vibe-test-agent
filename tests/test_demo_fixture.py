"""示例被测服务（fixture）的自检。

两条断言看起来像是"给测试写测试"，其实都有明确目的：

1. **契约要和需求一致**：订单不存在时契约必须要求 404（而不是 200），
   否则"服务该返回什么"这件事自己就先乱了，演示出来的结论也不可信。
2. **Bug 必须还在**：这个 fixture 的价值就在于它是坏的。
   哪天有人"顺手修好"了它，缺陷报告的演示就变成了一条假证据 ——
   这种"证据失效"是最难发现的，所以用测试把它钉死。
"""
from __future__ import annotations

from pathlib import Path

from selfheal import targets

ROOT = Path(__file__).resolve().parent.parent
DEMO_SERVICE = ROOT / "demo" / "buggy_service.py"


def test_demo_contract_parses_and_matches_requirements():
    target = targets.load_target("demo-buggy")

    assert target.base_url.endswith("8090")
    assert target.requirements_dir is not None
    assert target.requirements_dir.name == "demo-buggy"

    order = target.endpoint("order-detail")
    assert order is not None
    # 需求 FR-3：订单不存在必须返回 404，不得 500
    assert order.expect_status == 404
    assert order.expect_json == {"code": 404}


def test_demo_service_still_contains_the_deliberate_bug():
    """确保 fixture 还是坏的（见模块开头第 2 条理由）。"""
    source = DEMO_SERVICE.read_text(encoding="utf-8")

    # Bug 的形态：直接下标取订单，没有存在性判断
    assert "order = _ORDERS[order_id]" in source
    for guard in ("_ORDERS.get(", "if order_id not in _ORDERS", "if order is None"):
        assert guard not in source, f"有人加了保护逻辑（{guard}），缺陷演示会失效"


def test_demo_requirements_dir_exists():
    target = targets.load_target("demo-buggy")
    docs = list(target.requirements_dir.glob("*.md"))
    assert docs, "示例项目必须有需求文档，否则生成阶段读不到需求"
