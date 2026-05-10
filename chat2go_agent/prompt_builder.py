"""System prompt + messages 合成。

合成顺序（system prompt）：
  1. 全局 chat2go 人格（产品定位 + 输出风格）
  2. 大咖 SOUL.md（如有）
  3. 房间 active skill prompt（按 room.industry 匹配）
  4. room.system_prompt（大咖在房间级别的覆盖）
  5. memory context（Phase A 暂时为空，留接口）
"""

from __future__ import annotations

from .adapters.base import ImageRef, Message
from .soul import Skill


GLOBAL_PERSONA = (
    "你是 Chat2GO 平台的 AI 助手，工作在【{industry}】行业的调试室里。"
    "三方在线：小白（你的服务对象）、大咖（行业老师，会偶尔指点你）、你（AI 助手）。\n\n"
    "【输出风格 - 严格遵守】\n"
    "1. 默认简短：日常对话 1-3 句话，绝不列长清单或多个标题。\n"
    "2. **绝对不要**在列表项之间加空行，bullet/编号列表必须紧贴排列：\n"
    "   ✅ 正确格式：\n"
    "   - 项目一\n"
    "   - 项目二\n"
    "   - 项目三\n"
    "   ❌ 错误格式（不要这样写）：\n"
    "   - 项目一\n"
    "   \n"
    "   - 项目二\n"
    "3. 段落之间最多一个空行，不要连续多个空行。\n"
    "4. 不要每段开头加 emoji，不要写「很高兴为您服务」这类客套话。\n"
    "5. 只在小白明确要求合同/报告/方案/规格表时才输出长篇 Markdown 文档。\n"
    "6. 长文档用紧凑的 Markdown：标题下直接接内容，表格代替长列表。\n"
)


def build_system_prompt(
    room: dict,
    soul: str = "",
    skill: Skill | None = None,
    memory_context: str = "",
) -> str:
    industry = (room.get("industry") or "").strip() or "通用"
    parts: list[str] = [GLOBAL_PERSONA.format(industry=industry)]

    if soul:
        parts.append(f"【大咖人格】\n{soul}")

    if skill:
        parts.append(f"【行业能力包：{skill.display_name}】\n{skill.body}")

    extra = (room.get("system_prompt") or "").strip()
    if extra:
        parts.append(f"【本调试室的大咖补充指令】\n{extra}")

    if memory_context:
        parts.append(f"<memory-context>\n{memory_context}\n</memory-context>")

    return "\n\n".join(parts)


def build_messages(
    history: list[dict],
    current_user_msg: str,
    image_urls: list[tuple[str, str]] | None = None,
    attachment_texts: list[tuple[str, str]] | None = None,
) -> list[Message]:
    """
    把 Supabase messages 表的历史 + 当前消息转成 adapter 期望的 Message 列表。

    Phase A 不做 tool_use，所以历史只保留 user/expert/ai 文本。
    expert 的消息当作 user 角色（同样是给 AI 的"输入"），但加个标签让 AI 区分。
    """
    out: list[Message] = []

    for m in history:
        role = m.get("role", "user")
        content = m.get("content") or ""
        atts = m.get("attachments") or []
        if atts:
            names = ", ".join(a.get("name", "?") for a in atts)
            content = f"{content} [附件: {names}]"

        if role == "ai":
            out.append(Message(role="assistant", content=content))
        elif role == "expert":
            out.append(Message(role="user", content=f"[大咖]: {content}"))
        else:
            out.append(Message(role="user", content=f"[小白]: {content}"))

    # 当前消息：拼附件文本
    parts = [f"[小白最新消息]: {current_user_msg}"]
    if attachment_texts:
        parts.append("\n【小白上传的文件内容（请仔细阅读，作为生成内容的参考模板/资料）】")
        for fname, ftext in attachment_texts:
            parts.append(f"\n--- 文件：{fname} ---\n{ftext}\n--- 文件结束 ---")

    images = [ImageRef(url=u, mime_type=mt) for u, mt in (image_urls or [])]
    out.append(Message(role="user", content="\n".join(parts), images=images))

    return out
