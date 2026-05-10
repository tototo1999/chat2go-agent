"""Brain 委托层测试。"""

from __future__ import annotations

from chat2go_agent.brains import (
    BrainContext,
    BRAIN_BUILDERS,
    resolve_brain_name,
)
from chat2go_agent.brains.hermes import extract_hermes_reply, parse_usage_from_stderr
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


def test_extract_reply_uses_context_limit_marker():
    """hermes -Q 真实输出片段：init 在前，Context limit 是末标记，回复在后。"""
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-sonnet-4-5' to 'claude-sonnet-4-5' for anthropic.\n"
        "🤖 AI Agent initialized with model: claude-sonnet-4-5 (Anthropic native)\n"
        "✅ Enabled toolset 'browser': browser_back, browser_navigate\n"
        "🛠️  Final tool selection (29 tools): browser_back, ...\n"
        "💾 Prompt caching: ENABLED\n"
        "📊 Context limit: 200,000 tokens (compress at 50% = 100,000)\n"
        "Hi! How can I help you today?\n"
        "\n"
        "session_id: 20260510_150143_8cb605\n"
    )
    assert extract_hermes_reply(stdout) == "Hi! How can I help you today?"


def test_extract_reply_multiline():
    stdout = (
        "📊 Context limit: 200,000 tokens\n"
        "第一行回复\n"
        "第二行回复\n"
        "session_id: 20260510_xxx\n"
    )
    assert extract_hermes_reply(stdout) == "第一行回复\n第二行回复"


def test_extract_reply_fallback_when_no_marker():
    """没有 Context limit 标记时，过滤已知 emoji 行。"""
    stdout = (
        "⚠️  some warning\n"
        "✅ ok\n"
        "Real reply here\n"
    )
    assert extract_hermes_reply(stdout) == "Real reply here"


def test_brain_context_has_sane_defaults():
    ctx = BrainContext(room={"industry": "外贸"})
    assert ctx.history == []
    assert ctx.image_urls == []
    assert ctx.attachment_texts == []
    assert ctx.current_message == ""
