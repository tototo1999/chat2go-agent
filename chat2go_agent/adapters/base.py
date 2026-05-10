"""ModelAdapter 协议 + 共用类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ImageRef:
    url: str
    mime_type: str = "image/png"


@dataclass
class Message:
    role: str  # 'user' | 'assistant'
    content: str
    images: list[ImageRef] = field(default_factory=list)


class ModelAdapter(Protocol):
    """所有 provider adapter 的统一接口。"""

    provider: str

    async def call(
        self,
        system: str,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> str:
        """同步式调用：吃 messages 吐文本。Phase A 不做流式，不做 tool_use。"""
        ...
