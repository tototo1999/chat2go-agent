"""OpenAI 协议兼容 adapter —— OpenAI / DeepSeek / Qwen / Kimi / GLM / 本地（Ollama 等）共用。"""

from __future__ import annotations

import httpx

from .base import Message, Result, Usage


class OpenAICompatibleAdapter:
    """
    一个 adapter 覆盖所有 OpenAI 协议兼容厂商。
    通过 base_url + api_key 区分不同 provider。
    本地模型（如 Ollama）允许空 api_key。
    """

    def __init__(self, provider: str, api_key: str, base_url: str):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def call(
        self,
        system: str,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> Result:
        # local provider（Ollama 等）不要求 api_key
        if not self.api_key and self.provider != "local":
            raise RuntimeError(f"{self.provider} API key 未配置")
        if not self.base_url:
            raise RuntimeError(f"{self.provider} base_url 未配置")

        api_messages: list[dict] = []
        if system:
            api_messages.append({"role": "system", "content": system})

        for m in messages:
            api_messages.append({"role": m.role, "content": _build_openai_content(m)})

        payload = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
        }

        headers: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"{self.provider} API 错误 {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.provider} 返回空 choices: {data}")
        text = (choices[0].get("message", {}).get("content") or "").strip()
        u = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens", 0) or 0),
            output_tokens=int(u.get("completion_tokens", 0) or 0),
        )
        return Result(text=text, usage=usage)


def _build_openai_content(m: Message):
    """图片走 OpenAI 多模态格式：[{type:'text'},{type:'image_url'}]"""
    if not m.images:
        return m.content
    blocks: list[dict] = []
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for img in m.images:
        blocks.append({"type": "image_url", "image_url": {"url": img.url}})
    return blocks
