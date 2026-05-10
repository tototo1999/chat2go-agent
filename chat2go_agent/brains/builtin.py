"""BuiltinBrain：chat2go 自己组装 prompt + 调 LLM adapter。"""

from __future__ import annotations

from ..adapters import dispatch_call
from ..adapters.base import ModelAdapter
from ..prompt_builder import build_messages, build_system_prompt
from . import BrainContext, BrainResult


class BuiltinBrain:
    name = "builtin"

    def __init__(self, llm_adapters: dict[str, ModelAdapter]):
        self.llm_adapters = llm_adapters

    async def call(self, ctx: BrainContext) -> BrainResult:
        system = build_system_prompt(
            room=ctx.room,
            soul=ctx.soul,
            skill=ctx.skill,
            memory_context=ctx.memory_ctx,
        )
        messages = build_messages(
            history=ctx.history,
            current_user_msg=ctx.current_message,
            image_urls=ctx.image_urls,
            attachment_texts=ctx.attachment_texts,
        )
        result = await dispatch_call(
            self.llm_adapters,
            model=ctx.model,
            system=system,
            messages=messages,
        )
        return BrainResult(text=result.text, usage=result.usage, model=ctx.model)
