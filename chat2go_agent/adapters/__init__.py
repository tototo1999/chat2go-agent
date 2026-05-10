"""Adapter 注册表 + dispatch_call。

模型字符串格式：`provider/model`，例如 `anthropic/claude-sonnet-4-5`。
"""

from __future__ import annotations

from ..config import Credentials
from .anthropic import AnthropicAdapter
from .base import ImageRef, Message, ModelAdapter
from .gemini import GeminiAdapter
from .openai_compatible import OpenAICompatibleAdapter

__all__ = [
    "ImageRef",
    "Message",
    "ModelAdapter",
    "build_adapters",
    "dispatch_call",
    "split_model",
]

# OpenAI 协议兼容厂商共用一个 adapter 类
OPENAI_COMPATIBLE_PROVIDERS = {"openai", "deepseek", "qwen", "kimi", "glm", "openrouter"}


def split_model(model: str) -> tuple[str, str]:
    """anthropic/claude-sonnet-4-5 → ('anthropic', 'claude-sonnet-4-5')"""
    if "/" not in model:
        # 兼容老 room.model 短名（无 provider 前缀），默认当 anthropic
        return "anthropic", model
    provider, _, name = model.partition("/")
    return provider.strip(), name.strip()


def build_adapters(creds: Credentials) -> dict[str, ModelAdapter]:
    """根据已配凭证构建 adapter 实例字典。"""
    adapters: dict[str, ModelAdapter] = {}

    if c := creds.get("anthropic"):
        adapters["anthropic"] = AnthropicAdapter(api_key=c.api_key)

    for provider in OPENAI_COMPATIBLE_PROVIDERS:
        if c := creds.get(provider):
            adapters[provider] = OpenAICompatibleAdapter(
                provider=provider, api_key=c.api_key, base_url=c.base_url
            )

    if c := creds.get("gemini"):
        adapters["gemini"] = GeminiAdapter(api_key=c.api_key)

    return adapters


async def dispatch_call(
    adapters: dict[str, ModelAdapter],
    model: str,
    system: str,
    messages: list[Message],
    max_tokens: int = 2048,
    timeout: int = 120,
) -> str:
    """根据 model 字符串路由到对应 adapter。"""
    provider, model_name = split_model(model)
    adapter = adapters.get(provider)
    if adapter is None:
        configured = ", ".join(sorted(adapters)) or "(无)"
        raise RuntimeError(
            f"找不到 provider {provider!r} 的 adapter（model={model}）。已配置：{configured}"
        )
    return await adapter.call(
        system=system,
        messages=messages,
        model=model_name,
        max_tokens=max_tokens,
        timeout=timeout,
    )
