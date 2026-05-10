"""
Chat2GO · Agent Bridge (async)
================================

大咖在本地运行此进程。订阅 Supabase Realtime → 收到小白/大咖消息 →
按 SOUL + Skill + Memory 合成 prompt → dispatch 到对应 LLM adapter →
写回 AI 回复。
"""

from __future__ import annotations

import asyncio
import os
import re
import ssl
import sys
from typing import Optional

# ── 修复 Homebrew Python 的 SSL 证书路径问题 ──
# 必须在 import websockets / supabase 之前设置
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

    _orig_create_default_context = ssl.create_default_context

    def _patched_create_default_context(*args, **kwargs):
        if "cafile" not in kwargs and "capath" not in kwargs:
            kwargs["cafile"] = certifi.where()
        return _orig_create_default_context(*args, **kwargs)

    ssl.create_default_context = _patched_create_default_context  # type: ignore[assignment]
except ImportError:
    pass

from supabase import AsyncClient, acreate_client

from .adapters import dispatch_call, build_adapters, split_model
from .attachments import extract_attachment_text, split_image_and_text_attachments
from .config import (
    DEFAULT_EXPERT_EMAIL,
    DEFAULT_EXPERT_PASSWORD,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    Credentials,
)
from .memory import prefetch_memory
from .prompt_builder import build_messages, build_system_prompt
from .soul import Skill, load_skills, load_soul, select_skill_by_industry


def _looks_like_markdown(text: str) -> bool:
    return bool(
        re.search(r"^#{1,3} ", text, re.M)
        or re.search(r"\|.+\|.+\|", text)
        or re.search(r"^[-*] ", text, re.M)
    )


def _normalize_markdown(text: str) -> str:
    """压缩 AI 输出里多余的空行、列表项之间的空行。"""
    if not text:
        return text
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(\n[-*+] [^\n]+)\n\n(?=[-*+] )", r"\1\n", text)
    text = re.sub(r"(\n\d+\. [^\n]+)\n\n(?=\d+\. )", r"\1\n", text)
    text = re.sub(r"(\n#{1,4} [^\n]+)\n\n", r"\1\n", text)
    return text.strip()


async def _fetch_history(sb, room_id: str, limit: int = 12) -> list:
    r = (
        await sb.table("messages")
        .select("role,content,attachments,created_at")
        .eq("room_id", room_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(r.data or []))


class Chat2GOBridge:
    def __init__(
        self,
        email: str,
        password: str,
        creds: Credentials,
        default_model: str = "",
    ):
        self.email = email
        self.password = password
        self.creds = creds
        self.default_model = default_model or creds.default_model
        self.adapters = build_adapters(creds)
        self.skills = load_skills()
        self.soul = load_soul()
        self.sb: Optional[AsyncClient] = None
        self.expert_id: Optional[str] = None
        self.rooms: dict = {}
        self.processing: set = set()

    async def login(self):
        self.sb = await acreate_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        resp = await self.sb.auth.sign_in_with_password(
            {"email": self.email, "password": self.password}
        )
        self.expert_id = resp.user.id
        print(f"[bridge] 已登录：{self.email}  (id={self.expert_id[:8]}…)")

    async def load_rooms(self):
        result = (
            await self.sb.table("rooms").select("*").eq("expert_id", self.expert_id).execute()
        )
        new_rooms = {r["id"]: r for r in (result.data or [])}
        if set(new_rooms) != set(self.rooms):
            print(f"[bridge] 调试室列表：{[r['name'] for r in new_rooms.values()] or '(空)'}")
        self.rooms = new_rooms

    def resolve_model(self, room: dict) -> str:
        """房间级别 > 启动默认 > credentials.yaml 默认。
        兼容老的短名（无 provider 前缀）：默认补 anthropic/。
        """
        m = (room.get("model") or "").strip() or self.default_model
        if "/" not in m and m:
            m = f"anthropic/{m}"
        return m

    async def handle_message(self, msg: dict):
        msg_id = msg.get("id")
        room_id = msg.get("room_id")
        content = msg.get("content", "")
        attachments = msg.get("attachments") or []
        sender_role = msg.get("role", "user")  # 'user' | 'expert'
        sender_user_id = msg.get("user_id")

        if msg_id in self.processing:
            return
        self.processing.add(msg_id)

        room = self.rooms.get(room_id)
        if not room:
            self.processing.discard(msg_id)
            return

        model = self.resolve_model(room)
        provider, _ = split_model(model)

        att_summary = f" [附件 {len(attachments)} 个]" if attachments else ""
        sender_label = "大咖" if sender_role == "expert" else "小白"
        print(
            f"[bridge] [{room['name']}] {sender_label}: "
            f"{content[:60]}{'…' if len(content) > 60 else ''}{att_summary}"
        )
        print(f"[bridge] → {model}")

        try:
            # 1. 附件分类：图片走 vision、文本类下载提取
            image_urls, text_atts = split_image_and_text_attachments(attachments)
            attachment_texts: list[tuple[str, str]] = []
            for att in text_atts:
                name = att.get("name", "file")
                print(f"[bridge] 读取附件: {name}")
                text = await extract_attachment_text(att)
                attachment_texts.append((name, text))
                print(f"[bridge] 附件 {name} 提取了 {len(text)} 字符")
            for url, _ in image_urls:
                print(f"[bridge] 图片附件: {url[:60]}…")

            # 2. 历史 + skill + memory
            history = await _fetch_history(self.sb, room_id, limit=12)
            history = [
                m for m in history if m.get("content") != content or m.get("role") != sender_role
            ]
            skill = select_skill_by_industry(self.skills, room.get("industry") or "")
            memory_ctx = await prefetch_memory(
                self.sb,
                room_id=room_id,
                expert_id=self.expert_id,
                user_id=sender_user_id if sender_role == "user" else None,
            )

            # 3. 合成
            system = build_system_prompt(
                room, soul=self.soul, skill=skill, memory_context=memory_ctx
            )
            messages = build_messages(
                history,
                current_user_msg=content,
                image_urls=image_urls,
                attachment_texts=attachment_texts,
            )

            # 4. dispatch
            ai_text = await dispatch_call(
                self.adapters,
                model=model,
                system=system,
                messages=messages,
            )
            ai_text = _normalize_markdown(ai_text)
            if not ai_text:
                ai_text = f"（{provider} 返回空回复）"

            print(
                f"[bridge] [{room['name']}] AI: "
                f"{ai_text[:80]}{'…' if len(ai_text) > 80 else ''}"
            )

            # 5. 写回
            await self.sb.table("messages").insert(
                {
                    "room_id": room_id,
                    "user_id": self.expert_id,
                    "role": "ai",
                    "type": "markdown" if _looks_like_markdown(ai_text) else "text",
                    "content": ai_text,
                }
            ).execute()

        except Exception as e:
            err_str = str(e)
            print(f"[bridge] AI 调用失败 (model={model})：{err_str}")
            try:
                await self.sb.table("messages").insert(
                    {
                        "room_id": room_id,
                        "user_id": self.expert_id,
                        "role": "ai",
                        "content": f"⚠️ AI 调用失败 (model={model}): {err_str[:300]}",
                    }
                ).execute()
            except Exception:
                pass
        finally:
            self.processing.discard(msg_id)

    def on_realtime_message(self, payload):
        """Realtime 回调（同步）。把 user/expert 消息派发到 async 任务。"""
        msg = {}
        if isinstance(payload, dict):
            msg = (
                payload.get("record")
                or payload.get("new")
                or (payload.get("data") or {}).get("record")
                or (payload.get("payload") or {}).get("record")
                or {}
            )

        if not msg:
            return

        role = msg.get("role")
        room_id = msg.get("room_id")

        if role == "ai" or room_id not in self.rooms:
            return

        print(f"[bridge][debug] 收到 INSERT: role={role} room={str(room_id)[:8]}…")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.handle_message(msg))
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(self.handle_message(msg), self._loop)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        await self.login()
        await self.load_rooms()

        if not self.rooms:
            print("[bridge] 没有属于你的调试室。请先在网页上新建一个调试室。")
            return

        print(f"[bridge] 默认模型：{self.default_model}")
        print(f"[bridge] 已配 provider：{', '.join(sorted(self.adapters)) or '(无)'}")
        print(f"[bridge] 已加载 skill：{', '.join(s.display_name for s in self.skills.values()) or '(无)'}")
        print(f"[bridge] SOUL.md：{'已加载' if self.soul else '未配置（用通用人格）'}")
        print(f"[bridge] 监听中… 按 Ctrl+C 退出\n")

        channel = self.sb.realtime.channel("chat2go-bridge")
        channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table="messages",
            callback=self.on_realtime_message,
        )

        def on_subscribed(status, *args, **kwargs):
            print(f"[bridge][debug] realtime channel status: {status}  args={args}")

        try:
            await channel.subscribe(on_subscribed)
        except TypeError:
            await channel.subscribe()
            print("[bridge][debug] realtime channel subscribed (no callback)")

        # 兜底：每 5 秒轮询一次 messages 表，捕捉漏掉的新消息
        last_seen_ts: dict = {}
        try:
            while True:
                await asyncio.sleep(5)
                await self.load_rooms()
                for room_id in list(self.rooms.keys()):
                    try:
                        q = (
                            self.sb.table("messages")
                            .select("*")
                            .eq("room_id", room_id)
                            .eq("role", "user")
                            .order("created_at", desc=True)
                            .limit(3)
                        )
                        r = await q.execute()
                        for m in r.data or []:
                            if m["id"] in self.processing:
                                continue
                            ts = m["created_at"]
                            if last_seen_ts.get(room_id) and ts <= last_seen_ts[room_id]:
                                continue
                            ai_q = (
                                await self.sb.table("messages")
                                .select("id")
                                .eq("room_id", room_id)
                                .eq("role", "ai")
                                .gt("created_at", ts)
                                .limit(1)
                                .execute()
                            )
                            if ai_q.data:
                                last_seen_ts[room_id] = ts
                                continue
                            print(f"[bridge][poll] 发现未处理的 user 消息 {m['id'][:8]}…")
                            asyncio.create_task(self.handle_message(m))
                            last_seen_ts[room_id] = ts
                    except Exception as e:
                        print(f"[bridge][poll] 轮询出错: {e}")
        except asyncio.CancelledError:
            pass


# ── 子命令 ──
async def _login_for_admin(email: str, password: str) -> AsyncClient:
    sb = await acreate_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    await sb.auth.sign_in_with_password({"email": email, "password": password})
    return sb


async def cmd_set_prompt(room_id: str, prompt: str, email: str, password: str) -> None:
    sb = await _login_for_admin(email, password)
    await sb.table("rooms").update({"system_prompt": prompt}).eq("id", room_id).execute()
    print(f"[bridge] room {room_id[:8]}… system_prompt 已更新。")


async def cmd_set_model(room_id: str, model: str, email: str, password: str) -> None:
    sb = await _login_for_admin(email, password)
    if "/" not in model:
        model = f"anthropic/{model}"
    await sb.table("rooms").update({"model": model}).eq("id", room_id).execute()
    print(f"[bridge] room {room_id[:8]}… model 已更新为 {model}。")
