"""Memory prefetch（Phase A 只读，Phase B 加 sync_turn 写入）。

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


async def prefetch_memory(
    sb,
    room_id: str,
    expert_id: str,
    user_id: str | None = None,
    limit_per_scope: int = 10,
) -> str:
    """
    拉相关 memory 拼成给 LLM 看的 markdown 段落。
    Phase A：直接拉最近 N 条。Phase B 升级为相关性检索。

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
        # memories 表可能还没创建，或者 RLS 拦了，不阻断主流程
        print(f"[memory] prefetch 失败（忽略）：{e}")
        return ""
