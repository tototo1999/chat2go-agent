"""烟雾测试：不调外部 API，只验证模块装配。"""

from __future__ import annotations


def test_skills_load_six():
    from chat2go_agent.soul import load_skills

    skills = load_skills()
    expected = {"foreign-trade", "fitness", "real-estate", "education", "quant", "medical"}
    assert expected.issubset(skills.keys()), f"missing skills: {expected - skills.keys()}"


def test_skill_industry_match():
    from chat2go_agent.soul import load_skills, select_skill_by_industry

    skills = load_skills()
    s = select_skill_by_industry(skills, "外贸")
    assert s is not None
    assert s.name == "foreign-trade"
    assert "FOB" in s.body or "外贸" in s.body


def test_split_model():
    from chat2go_agent.adapters import split_model

    assert split_model("anthropic/claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")
    assert split_model("deepseek/deepseek-chat") == ("deepseek", "deepseek-chat")
    # 兼容老短名
    assert split_model("claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")


def test_build_adapters_with_only_anthropic():
    from chat2go_agent.adapters import build_adapters
    from chat2go_agent.config import Credentials, ProviderCreds

    creds = Credentials(providers={
        "anthropic": ProviderCreds(api_key="test-key"),
        "deepseek": ProviderCreds(api_key=""),  # 未配
    })
    adapters = build_adapters(creds)
    assert "anthropic" in adapters
    assert "deepseek" not in adapters


def test_prompt_builder_composes():
    from chat2go_agent.prompt_builder import build_system_prompt
    from chat2go_agent.soul import load_skills, select_skill_by_industry

    skills = load_skills()
    skill = select_skill_by_industry(skills, "外贸")
    room = {"industry": "外贸", "system_prompt": "请用英文回复"}
    sp = build_system_prompt(room, soul="我是张大咖", skill=skill)

    # 包含所有 4 层
    assert "Chat2GO" in sp  # 全局
    assert "张大咖" in sp  # SOUL
    assert "外贸" in sp  # skill
    assert "请用英文回复" in sp  # room override


def test_build_messages_history_and_attachments():
    from chat2go_agent.prompt_builder import build_messages

    history = [
        {"role": "user", "content": "你好"},
        {"role": "ai", "content": "你好，请问需要什么帮助？"},
    ]
    msgs = build_messages(
        history,
        current_user_msg="帮我写报价单",
        image_urls=[("https://x/y.png", "image/png")],
        attachment_texts=[("产品资料.txt", "型号 ABC，售价 100")],
    )
    assert len(msgs) == 3  # 2 历史 + 1 当前
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"
    assert msgs[2].role == "user"
    assert "产品资料.txt" in msgs[2].content
    assert len(msgs[2].images) == 1
