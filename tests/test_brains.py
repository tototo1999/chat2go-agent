"""Brain 委托层测试。"""

from __future__ import annotations

from chat2go_agent.brains import (
    BrainContext,
    BRAIN_BUILDERS,
    resolve_brain_name,
)
from chat2go_agent.brains.hermes import parse_usage_from_stderr
from chat2go_agent.config import Credentials


def test_registry_has_builtin_and_hermes():
    assert "builtin" in BRAIN_BUILDERS
    assert "hermes" in BRAIN_BUILDERS


def test_resolve_room_override_wins():
    creds = Credentials(default_brain="builtin")
    name = resolve_brain_name(creds, room={"brain": "hermes"})
    assert name == "hermes"


def test_resolve_yaml_default():
    creds = Credentials(default_brain="builtin")
    name = resolve_brain_name(creds, room={})
    assert name == "builtin"


def test_resolve_unknown_room_brain_falls_through():
    creds = Credentials(default_brain="builtin")
    name = resolve_brain_name(creds, room={"brain": "nonsense"})
    assert name == "builtin"  # 未识别 → 走 yaml 默认


def test_parse_usage_single_turn():
    stderr = (
        "14:47:18 - root - DEBUG - Total message size: ~8,210 tokens\n"
        "14:47:19 - root - DEBUG - Token usage: prompt=19,870, completion=8, total=19,878\n"
    )
    usage = parse_usage_from_stderr(stderr)
    assert usage.input_tokens == 19870
    assert usage.output_tokens == 8


def test_parse_usage_multi_turn_sums():
    stderr = (
        "Token usage: prompt=1,000, completion=50, total=1,050\n"
        "next turn ...\n"
        "Token usage: prompt=2,500, completion=120, total=2,620\n"
    )
    usage = parse_usage_from_stderr(stderr)
    assert usage.input_tokens == 1000 + 2500
    assert usage.output_tokens == 50 + 120


def test_parse_usage_empty_stderr():
    usage = parse_usage_from_stderr("nothing here")
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_brain_context_has_sane_defaults():
    ctx = BrainContext(room={"industry": "外贸"})
    assert ctx.history == []
    assert ctx.image_urls == []
    assert ctx.attachment_texts == []
    assert ctx.current_message == ""
