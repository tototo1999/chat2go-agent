"""Memory prefetch + sync_turn 写入（Phase B）。

Memory 表 schema（在 supabase/migrations/ 里）：
  id          uuid pk
  scope       'room' | 'expert' | 'user'
  scope_id    uuid (room_id / expert_id / user_id)
  content     text (markdown 一段事实)
  tags        text[]
  source_message_id  uuid → messages(id)
  created_at  timestamptz
  updated_at  timestamptz
"""

from __future__ import annotations

import asyncio
import json
import re


async def prefetch_memory(
    sb,
    room_id: str,
    expert_id: str,
    user_id: str | None = None,
    limit_per_scope: int = 10,
) -> str:
    """
    拉相关 memory 拼成给 LLM 看的 markdown 段落。
    返回空串 = 没有 memory 或 memories 表不存在（不阻断主流程）。
    """
    try:
        scopes = [("room", room_id), ("expert", expert_id)]
        if user_id:
            scopes.append(("user", user_id))

        sections: list[str] = []
        for scope, scope_id in scopes:
            r = (
                await sb.table("memories")
                .select("content,tags,created_at")
                .eq("scope", scope)
                .eq("scope_id", scope_id)
                .order("updated_at", desc=True)
                .limit(limit_per_scope)
                .execute()
            )
            rows = r.data or []
            if not rows:
                continue
            label = {"room": "本调试室记忆", "expert": "大咖个人记忆", "user": "小白个人记忆"}[scope]
            lines = [f"## {label}"]
            for row in rows:
                tags = row.get("tags") or []
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"- {row['content']}{tag_str}")
            sections.append("\n".join(lines))

        return "\n\n".join(sections)
    except Exception as e:
        print(f"[memory] prefetch 失败（忽略）：{e}")
        return ""


_EXTRACT_PROMPT = """你是一个知识提取助手。根据下面这段对话，判断大咖的发言是否包含值得长期记住的事实、规则或偏好。

判断标准（满足任一即提取）：
- 大咖纠正了 AI 的回答
- 大咖补充了专业知识或行业规则
- 大咖表达了明确的偏好（风格、用词、禁忌等）
- 大咖提到了关于这个房间/用户的重要背景信息

如果有值得记住的内容，以 JSON 数组输出，每条格式：
{"content": "一句话描述这个事实", "scope": "room|expert", "tags": ["标签1", "标签2"]}

scope 说明：
- room：只对这个调试室有效（如某用户的具体情况）
- expert：大咖个人的通用知识/偏好（跨房间有效）

如果没有值得记住的内容，输出空数组：[]

只输出 JSON，不要任何解释。

---对话---
{dialogue}
"""


async def sync_memory(
    sb,
    adapters: dict,
    model: str,
    room_id: str,
    expert_id: str,
    expert_message: str,
    ai_message: str,
    source_message_id: str | None = None,
) -> None:
    """
    大咖发言后异步调用：提取记忆并写入 memories 表。
    不阻断主流程，所有异常静默处理。
    """
    print(f"[memory] sync_memory 入口: room={room_id[:8]}… expert_msg={expert_message[:40]!r}")
    try:
        if not expert_message.strip():
            print(f"[memory] expert_msg 空,跳过")
            return

        dialogue = f"大咖：{expert_message}\nAI：{ai_message}" if ai_message else f"大咖：{expert_message}"

        from .adapters import dispatch_call, Message
        print(f"[memory] 准备调 LLM 提取,model={model}")
        # 起独立任务 + 用 wait() 而非 wait_for() —— wait_for 会 _cancel_and_wait
        # 等内部任务完全 cancel,如果内部卡在 thread pool 的 getaddrinfo 上,
        # _cancel_and_wait 自己也会卡(macOS Python 3.14 实测)。
        # wait() 不 await cancel 完成,15s 一到就放手让 zombie 继续在背景跑。
        llm_task = asyncio.create_task(dispatch_call(
            adapters=adapters,
            model=model,
            system="你是知识提取助手，只输出 JSON。",
            # 用 replace 而不是 .format:prompt 内含 JSON 示例 {"content": ...},
            # .format() 会把它当命名占位符,抛 KeyError: '"content"'。
            messages=[Message(role="user", content=_EXTRACT_PROMPT.replace("{dialogue}", dialogue))],
            max_tokens=2048,  # 512 太小,gemini 输出 JSON 还没闭合 ] 就被砍
            timeout=30,       # 内层 httpx ReadTimeout;给 Gemini 2.5 Pro 充足时间
        ))
        print(f"[memory] 进入 await asyncio.wait(timeout=25)")
        done, pending = await asyncio.wait({llm_task}, timeout=25)
        print(f"[memory] wait 返回:done={len(done)} pending={len(pending)} task_done={llm_task.done()}")
        if not done:
            print(f"[memory] LLM 调用硬超时（>25s,放弃,不阻塞主流程）")
            llm_task.cancel()  # 发个 cancel 信号,但不 await,zombie 自己跑
            return
        try:
            result = llm_task.result()
        except Exception as e:
            print(f"[memory] LLM 调用失败（忽略）：{type(e).__name__}: {e}")
            return

        raw = (getattr(result, "text", "") or "").strip()
        if not raw:
            print(f"[memory] LLM 返回空,跳过")
            return
        # 从输出里提取 JSON 数组（有时模型会包一层 markdown ```、或返回 {items:[...]}）
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            print(f"[memory] LLM 返回里找不到 [...] 数组,跳过; raw={raw[:500]!r}")
            return
        try:
            items = json.loads(m.group())
        except json.JSONDecodeError as e:
            print(f"[memory] JSON 解析失败（跳过）：{e}; raw={raw[:500]!r}")
            return

        if not isinstance(items, list):
            print(f"[memory] items 不是 list,跳过; type={type(items).__name__}")
            return
        if not items:
            print(f"[memory] LLM 判定无事实可记 (items=[]),跳过")
            return

        for item in items:
            try:
                if not isinstance(item, dict):
                    print(f"[memory] 跳过非 dict 条目：{item!r}")
                    continue
                content = (item.get("content") or "").strip()
                scope = item.get("scope", "room")
                tags = item.get("tags") or []
                if not content:
                    continue
                if scope not in ("room", "expert"):
                    scope = "room"
                scope_id = room_id if scope == "room" else expert_id

                # 简单去重：content 完全相同则跳过
                existing = (
                    await sb.table("memories")
                    .select("id")
                    .eq("scope", scope)
                    .eq("scope_id", scope_id)
                    .eq("content", content)
                    .limit(1)
                    .execute()
                )
                if existing.data:
                    print(f"[memory] 跳过重复记忆: {content[:40]}…")
                    continue

                row = {
                    "scope": scope,
                    "scope_id": scope_id,
                    "content": content,
                    "tags": tags,
                }
                if source_message_id:
                    row["source_message_id"] = source_message_id

                await sb.table("memories").insert(row).execute()
                print(f"[memory] 写入记忆 [{scope}]: {content[:60]}…")
            except Exception as item_e:
                # 单条失败不影响其他条目；打 raw item 便于诊断
                print(f"[memory] 单条记忆处理失败（跳过）：{item_e}; item={item!r}")

    except Exception as e:
        print(f"[memory] sync_memory 失败（忽略）：{e}")
