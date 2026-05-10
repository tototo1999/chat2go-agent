"""Brain 委托层。

chat2go-agent 把"如何回答"这件事抽象成 Brain：
  - BuiltinBrain：自己拼 system prompt + 调 LLM adapter（chat2go 默认）
  - HermesBrain：shell out 到本地 hermes CLI，复用大咖在 ~/.hermes 里的所有配置

bridge.py 不关心 brain 内部用什么 skills/tools/memory/model，
只关心 (BrainContext) → (BrainResult: text + usage)。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Protocol

from ..adapters.base import ImageRef, Message, Usage
from ..soul import Skill


@dataclass
class BrainContext:
    """收集好的对话上下文，交给 brain 处理。"""
    room: dict
    soul: str = ""
    skill: Skill | None = None
    memory_ctx: str = ""
    history: list[dict] = field(default_factory=list)
    current_message: str = ""
    image_urls: list[tuple[str, str]] = field(default_factory=list)
    attachment_texts: list[tuple[str, str]] = field(default_factory=list)
    model: str = ""  # provider/name 格式，已 resolve


@dataclass
class BrainResult:
    text: str
    usage: Usage
    model: str = ""  # 实际用了什么模型（可能与 ctx.model 不同）


class BrainAdapter(Protocol):
    name: str

    async def call(self, ctx: BrainContext) -> BrainResult: ...


# 注册表：name → 构造函数
def _build_builtin(creds, hermes_bin: str | None) -> "BrainAdapter":
    from .builtin import BuiltinBrain
    from ..adapters import build_adapters
    return BuiltinBrain(llm_adapters=build_adapters(creds))


def _build_hermes(creds, hermes_bin: str | None) -> "BrainAdapter":
    from .hermes import HermesBrain
    if not hermes_bin:
        raise RuntimeError("hermes 二进制找不到，请先 brew install / 装好 hermes")
    return HermesBrain(hermes_bin=hermes_bin)


BRAIN_BUILDERS = {
    "builtin": _build_builtin,
    "hermes": _build_hermes,
}


def find_hermes_bin() -> str | None:
    """探测本机有没有装 hermes。"""
    return (
        shutil.which("hermes")
        or (str(_p) if (_p := __import__("pathlib").Path.home() / ".local" / "bin" / "hermes").exists() else None)
    )


def resolve_brain_name(creds, room: dict) -> str:
    """
    选 brain 的优先级：
      1. room.brain（房间级显式指定）
      2. credentials.yaml defaults.brain
      3. 自动：装了 hermes → 'hermes'，否则 'builtin'
    """
    name = (room.get("brain") or "").strip().lower()
    if name and name in BRAIN_BUILDERS:
        return name

    name = (getattr(creds, "default_brain", "") or "auto").strip().lower()
    if name in BRAIN_BUILDERS:
        return name

    # auto
    if find_hermes_bin():
        return "hermes"
    return "builtin"


def build_brain(creds, name: str) -> BrainAdapter:
    builder = BRAIN_BUILDERS.get(name)
    if not builder:
        raise RuntimeError(f"未知 brain: {name!r}（已注册：{list(BRAIN_BUILDERS)}）")
    return builder(creds, find_hermes_bin())


__all__ = [
    "BrainAdapter",
    "BrainContext",
    "BrainResult",
    "build_brain",
    "find_hermes_bin",
    "resolve_brain_name",
]
