"""Adapter 注册表 + dispatch_call。

模型字符串格式：`provider/model`，例如 `anthropic/claude-sonnet-4-5`。
本地模型用 `local/<name>`，走 OpenAI 兼容协议（Ollama 等）。
"""

from __future__ import annotations

from ..config import Credentials
from .anthropic import AnthropicAdapter
from .base import ImageRef, Message, ModelAdapter, Result, Usage
from .gemini import GeminiAdapter
from .openai_compatible import OpenAICompatibleAdapter

__all__ = [
    "ImageRef",
    "Message",
    "ModelAdapter",
    "Result",
    "Usage",
    "build_adapters",
    "dispatch_call",
    "split_model",
]

# OpenAI 协议兼容厂商共用一个 adapter 类（local 也走这条）
OPENAI_COMPATIBLE_PROVIDERS = {
    "openai", "deepseek", "qwen", "kimi", "glm", "openrouter", "local",
}


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
        c = creds.providers.get(provider)
        if not c:
            continue
        # local 允许空 api_key（Ollama 默认无认证），但需要至少配置一个本地模型成本
        if provider == "local":
            if not c.base_url or not creds.local_prices:
                continue
        elif not c.api_key:
            continue
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
) -> Result:
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
