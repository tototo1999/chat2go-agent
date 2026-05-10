"""Google Gemini adapter（原生协议，与 OpenAI 不兼容）。"""

from __future__ import annotations

import httpx

from .base import Message


class GeminiAdapter:
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    ):
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
            raise RuntimeError("Gemini API key 未配置")

        contents: list[dict] = []
        for m in messages:
            role = "user" if m.role == "user" else "model"
            parts: list[dict] = []
            if m.content:
                parts.append({"text": m.content})
            for img in m.images:
                parts.append({
                    "inline_data": {
                        "mime_type": img.mime_type,
                        "data": img.url,  # NOTE: Phase A 暂不下载图片转 base64，需要时后续补
                    }
                })
            contents.append({"role": role, "parts": parts or [{"text": ""}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            raise RuntimeError(f"Gemini API 错误 {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini 返回空 candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return text.strip()
