"""Anthropic Claude adapter（原生协议）。"""

from __future__ import annotations

import httpx

from .base import Message, ImageRef


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def call(
        self,
        system: str,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Anthropic API key 未配置")

        api_messages = [
            {
                "role": m.role,
                "content": _build_anthropic_content(m),
            }
            for m in messages
        ]

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API 错误 {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        # 多个 content block 时，把所有 text block 拼起来
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()


def _build_anthropic_content(m: Message) -> list[dict] | str:
    if not m.images:
        return m.content
    blocks: list[dict] = []
    for img in m.images:
        blocks.append({
            "type": "image",
            "source": {"type": "url", "url": img.url},
        })
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    return blocks
