"""pricing.calculate_charge 单元测试。"""

from __future__ import annotations

import pytest

from chat2go_agent.adapters.base import Usage
from chat2go_agent.pricing import calculate_charge, ONLINE_PRICES


def test_online_anthropic_sonnet():
    # claude-sonnet-4-5: $3 input / $15 output per Mtok
    # 1000 in + 500 out = 0.001 * 3 + 0.0005 * 15 = 0.003 + 0.0075 = $0.0105
    c = calculate_charge(
        model="anthropic/claude-sonnet-4-5",
        usage=Usage(input_tokens=1000, output_tokens=500),
        commission_pct=0.15,
        exchange_rate=7.20,
    )
    assert c.cost_source == "online"
    assert abs(c.cost_usd - 0.0105) < 1e-9
    # ¥ = 0.0105 × 1.15 × 7.20 = 0.086940
    assert abs(c.user_charge_cny - 0.0105 * 1.15 * 7.20) < 1e-9


def test_local_qwen():
    c = calculate_charge(
        model="local/qwen2.5-72b",
        usage=Usage(input_tokens=10_000, output_tokens=5_000),
        commission_pct=0.20,
        exchange_rate=7.0,
        local_prices={"qwen2.5-72b": (0.10, 0.10)},
    )
    assert c.cost_source == "local"
    # 0.01 + 0.005 = $0.0015
    assert abs(c.cost_usd - 0.0015) < 1e-9
    assert abs(c.user_charge_cny - 0.0015 * 1.20 * 7.0) < 1e-9


def test_local_missing_price_raises():
    with pytest.raises(ValueError, match="未在.*配置成本"):
        calculate_charge(
            model="local/unknown-model",
            usage=Usage(input_tokens=100, output_tokens=100),
            local_prices={},  # 空
        )


def test_unknown_online_returns_zero():
    """未知在线模型不阻断（返回 0 成本）。"""
    c = calculate_charge(
        model="anthropic/some-future-model",
        usage=Usage(input_tokens=1000, output_tokens=500),
    )
    assert c.cost_source == "online"
    assert c.cost_usd == 0.0
    assert c.user_charge_cny == 0.0


def test_zero_usage():
    c = calculate_charge(
        model="anthropic/claude-sonnet-4-5",
        usage=Usage(input_tokens=0, output_tokens=0),
    )
    assert c.cost_usd == 0.0
    assert c.user_charge_cny == 0.0


def test_online_prices_table_format():
    """所有 ONLINE_PRICES 条目都是 (input, output) 二元组。"""
    for model, prices in ONLINE_PRICES.items():
        assert isinstance(prices, tuple), f"{model}: 不是 tuple"
        assert len(prices) == 2, f"{model}: 应该有 2 个值"
        assert all(isinstance(p, (int, float)) and p >= 0 for p in prices), f"{model}: 价格非法"
        assert "/" in model, f"{model}: 缺少 provider/ 前缀"
