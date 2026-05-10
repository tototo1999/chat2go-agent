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
    """
    LLM 一次调用的 token 用量。

    Anthropic 启用 prompt caching 时，input 拆三份：
      - input_tokens: 没命中 cache 的全新 input（按基础价计费）
      - cache_creation_input_tokens: 写入 cache 的部分（基础价 × 1.25）
      - cache_read_input_tokens: 命中 cache 的部分（基础价 × 0.10）

    其它 provider（OpenAI/Gemini/...）暂不分解，cache 字段为 0。
    总输入 token = input_tokens + cache_creation + cache_read。
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


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
