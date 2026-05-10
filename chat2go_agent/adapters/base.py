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


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Result:
    """所有 adapter 的统一返回。"""
    text: str
    usage: Usage


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
    ) -> Result:
        """同步式调用：吃 messages 吐文本 + token 用量。Phase A 不做流式 / tool_use。"""
        ...
