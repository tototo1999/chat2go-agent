"""Token 成本与计费。

两个成本来源：
  - 在线模型：厂商定价（写死在 ONLINE_PRICES，定期更新）
  - 本地模型：大咖在 ~/.chat2go/credentials.yaml 自报硬件 + 电费分摊

最终账单 = token 成本 (USD) × (1 + 大咖佣金率) × USD→CNY 汇率
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters.base import Usage

# USD per 1M tokens, (input_rate, output_rate)
# 价格定期对照厂商官网更新（最后更新 2026-05-10）
ONLINE_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "anthropic/claude-sonnet-4-5":  (3.00, 15.00),
    "anthropic/claude-haiku-4-5":   (1.00,  5.00),
    "anthropic/claude-opus-4-5":   (15.00, 75.00),

    # OpenAI
    "openai/gpt-5":                 (5.00, 15.00),
    "openai/gpt-5-mini":            (0.40,  1.60),

    # DeepSeek（OpenAI 兼容）
    "deepseek/deepseek-chat":       (0.27,  1.10),
    "deepseek/deepseek-reasoner":   (0.55,  2.19),

    # 通义千问（OpenAI 兼容）
    "qwen/qwen-max":                (1.40,  5.60),
    "qwen/qwen-plus":               (0.40,  1.20),

    # Kimi / Moonshot（OpenAI 兼容）
    "kimi/moonshot-v1-128k":        (8.40,  8.40),

    # 智谱 GLM（OpenAI 兼容）
    "glm/glm-4-plus":               (0.70,  0.70),

    # Gemini
    "gemini/gemini-2-pro":          (1.25,  5.00),
}

# 默认值（房间没设时使用）
DEFAULT_COMMISSION_PCT = 0.15  # 大咖佣金率
DEFAULT_EXCHANGE_RATE = 7.20  # USD → CNY


@dataclass
class CostBreakdown:
    cost_source: str  # 'online' | 'local'
    cost_usd: float
    user_charge_cny: float


def calculate_charge(
    model: str,
    usage: Usage,
    commission_pct: float = DEFAULT_COMMISSION_PCT,
    exchange_rate: float = DEFAULT_EXCHANGE_RATE,
    local_prices: dict[str, tuple[float, float]] | None = None,
) -> CostBreakdown:
    """
    根据 token 数算成本和账单。

    本地模型（model 以 'local/' 开头）必须在 local_prices 里有定价，否则报错。
    在线模型未知时返回 0 成本（不阻断，记日志在调用方处理）。
    """
    if model.startswith("local/"):
        name = model.split("/", 1)[1]
        rates = (local_prices or {}).get(name)
        if rates is None:
            raise ValueError(
                f"本地模型 {model!r} 未在 ~/.chat2go/credentials.yaml 的 "
                f"local.models.{name} 配置成本"
            )
        in_rate, out_rate = rates
        source = "local"
    else:
        rates = ONLINE_PRICES.get(model)
        if rates is None:
            in_rate, out_rate = 0.0, 0.0
        else:
            in_rate, out_rate = rates
        source = "online"

    cost_usd = (usage.input_tokens * in_rate + usage.output_tokens * out_rate) / 1_000_000
    user_charge_cny = cost_usd * (1 + commission_pct) * exchange_rate
    return CostBreakdown(
        cost_source=source,
        cost_usd=cost_usd,
        user_charge_cny=user_charge_cny,
    )
