"""
Chat2GO platform adapter for Hermes.

让 hermes 把 chat2go.cn 当成一个原生 IM 渠道（和 Discord/Telegram 平起平坐）。

启用方式：
  export CHAT2GO_TOKEN=c2g-key_xxx   # chat2go.cn 网页生成
  hermes gateway run

工作流：
  1. connect()：用 token 调 chat2go.cn /functions/v1/agent-auth/exchange
     拿到 magiclink token_hash → verify_otp 换 Supabase session
  2. 订阅 messages 表 Realtime，filter role != ai
  3. 收到 INSERT → handle_message(MessageEvent) 派给 hermes brain
  4. send() 把 hermes 回复 insert 到 messages 表，role=ai

依赖（从 pyproject 之外的）：
  - supabase-py（chat2go-agent 已装）
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# 默认 chat2go.cn Supabase 端点（覆盖通过 CHAT2GO_SUPABASE_URL / _ANON_KEY 环境变量）
DEFAULT_SUPABASE_URL = "https://qjnagbzqhoansixqharb.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFqbmFnYnpxaG9hbnNpeHFoYXJiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgzNDIxODIsImV4cCI6MjA5MzkxODE4Mn0"
    ".GpMUVTk6JvqeciXagXQiJunc8TLFMHg3_b9reIjJ2Y8"
)


def check_chat2go_requirements() -> bool:
    """检查 supabase-py 是否可用。"""
    try:
        import supabase  # noqa: F401
        return True
    except ImportError:
        logger.warning("Chat2GO: supabase-py 未安装（pip install supabase）")
        return False


class Chat2GoAdapter(BasePlatformAdapter):
    """Chat2GO 平台 adapter（订阅 Supabase Realtime + 写回消息）。"""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.CHAT2GO)

        self.token = config.token or os.getenv("CHAT2GO_TOKEN")
        self.supabase_url = (
            config.extra.get("supabase_url") or DEFAULT_SUPABASE_URL
        ).rstrip("/")
        self.supabase_anon_key = (
            config.extra.get("supabase_anon_key") or DEFAULT_SUPABASE_ANON_KEY
        )
        self.exchange_url = f"{self.supabase_url}/functions/v1/agent-auth/exchange"

        self._sb = None  # type: Any  # AsyncClient
        self._expert_id: Optional[str] = None
        self._expert_email: Optional[str] = None
        self._rooms: Dict[str, Dict[str, Any]] = {}  # room_id → row
        self._channel = None
        self._poll_task: Optional[asyncio.Task] = None
        self._processing: set[str] = set()
        # 防回环：记录我们刚发出去的 message_id，下次 Realtime 收到自己消息时跳过
        self._self_sent_ids: set[str] = set()

        logger.info(
            "Chat2GO initialized: url=%s token=%s***",
            self.supabase_url,
            (self.token or "")[:16],
        )

    # ── 1. 鉴权 ──
    async def _exchange_session(self):
        """用 connection_key 换 Supabase session。"""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.exchange_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.supabase_anon_key}",
                    "apikey": self.supabase_anon_key,
                },
                json={"key": self.token},
            )
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", resp.text[:200])
            except Exception:
                err = resp.text[:200]
            raise RuntimeError(f"agent-auth 失败 ({resp.status_code}): {err}")
        return resp.json()

    # ── 2. 连接生命周期 ──
    async def connect(self) -> bool:
        if not self.token:
            logger.error("CHAT2GO_TOKEN 未设置")
            return False

        try:
            from supabase import acreate_client
        except ImportError:
            logger.error("supabase-py 未安装")
            return False

        try:
            self._sb = await acreate_client(self.supabase_url, self.supabase_anon_key)
            otp = await self._exchange_session()
            await self._sb.auth.verify_otp({
                "token_hash": otp["token_hash"],
                "type": "magiclink",
            })
            self._expert_id = otp["expert_id"]
            self._expert_email = otp["email"]
            logger.info(
                "Chat2GO authenticated: %s (expert=%s)",
                self._expert_email,
                self._expert_id[:8],
            )

            await self._load_rooms()

            # 订阅 Realtime + 启动轮询兜底
            await self._subscribe_realtime()
            self._poll_task = asyncio.create_task(self._poll_loop())

            self._mark_connected()
            return True
        except Exception as e:
            logger.exception("Chat2GO connect 失败")
            return False

    async def disconnect(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        try:
            if self._channel:
                await self._channel.unsubscribe()
        except Exception:
            pass
        self._mark_disconnected()

    async def _load_rooms(self):
        r = await self._sb.table("rooms").select("*").eq(
            "expert_id", self._expert_id
        ).execute()
        self._rooms = {row["id"]: row for row in (r.data or [])}
        logger.info("Chat2GO: loaded %d rooms", len(self._rooms))

    async def _subscribe_realtime(self):
        self._channel = self._sb.realtime.channel("chat2go-hermes")
        self._channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table="messages",
            callback=self._on_realtime_insert,
        )
        try:
            await self._channel.subscribe()
        except Exception as e:
            logger.warning("Chat2GO realtime subscribe 失败（用轮询兜底）: %s", e)

    def _on_realtime_insert(self, payload):
        """同步回调：派到 async 任务。"""
        msg = (
            (payload or {}).get("record")
            or (payload or {}).get("new")
            or (payload or {}).get("data", {}).get("record")
            or {}
        )
        if not msg:
            return
        # 跳过 AI 自己的消息（防回环）
        if msg.get("role") == "ai":
            return
        if msg.get("id") in self._self_sent_ids:
            return
        room_id = msg.get("room_id")
        if room_id not in self._rooms:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._dispatch_inbound(msg))
        except RuntimeError:
            pass  # no loop

    async def _poll_loop(self):
        """每 5 秒轮询：捕捉 Realtime 漏掉的消息 + 刷新房间列表。"""
        last_seen: dict[str, str] = {}
        try:
            while True:
                await asyncio.sleep(5)
                try:
                    await self._load_rooms()
                    for rid in list(self._rooms.keys()):
                        r = (
                            await self._sb.table("messages")
                            .select("*")
                            .eq("room_id", rid)
                            .neq("role", "ai")
                            .order("created_at", desc=True)
                            .limit(3)
                            .execute()
                        )
                        for m in r.data or []:
                            if m["id"] in self._processing:
                                continue
                            ts = m["created_at"]
                            if last_seen.get(rid) and ts <= last_seen[rid]:
                                continue
                            # 看这条之后是否已经有 AI 回复
                            ai = (
                                await self._sb.table("messages")
                                .select("id")
                                .eq("room_id", rid)
                                .eq("role", "ai")
                                .gt("created_at", ts)
                                .limit(1)
                                .execute()
                            )
                            if ai.data:
                                last_seen[rid] = ts
                                continue
                            asyncio.create_task(self._dispatch_inbound(m))
                            last_seen[rid] = ts
                except Exception as e:
                    logger.debug("Chat2GO poll error: %s", e)
        except asyncio.CancelledError:
            pass

    # ── 3. 入站消息 → MessageEvent → hermes ──
    async def _dispatch_inbound(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if msg_id in self._processing:
            return
        self._processing.add(msg_id)
        try:
            room_id = msg.get("room_id")
            room = self._rooms.get(room_id) or {}
            content = msg.get("content") or ""
            role = msg.get("role", "user")  # 'user' | 'expert'
            user_label = "小白" if role == "user" else "大咖"

            event = MessageEvent(
                text=content,
                message_type=MessageType.TEXT,
                source=self.build_source(
                    chat_id=room_id,
                    chat_name=room.get("name") or "调试室",
                    chat_type="group",  # chat2go 房间是三方对话（小白+大咖+AI）
                    user_id=msg.get("user_id"),
                    user_name=user_label,
                    chat_topic=room.get("industry"),
                ),
                message_id=str(msg_id),
                timestamp=datetime.fromisoformat(
                    msg["created_at"].replace("Z", "+00:00")
                ) if msg.get("created_at") else datetime.now(),
            )
            await self.handle_message(event)
        finally:
            self._processing.discard(msg_id)

    # ── 4. 出站 ──
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            r = await self._sb.table("messages").insert(
                {
                    "room_id": chat_id,
                    "user_id": self._expert_id,
                    "role": "ai",
                    "type": "text",
                    "content": content,
                }
            ).execute()
            new_id = (r.data or [{}])[0].get("id")
            if new_id:
                self._self_sent_ids.add(new_id)
                # 限制集合大小
                if len(self._self_sent_ids) > 500:
                    self._self_sent_ids = set(list(self._self_sent_ids)[-200:])
            return SendResult(success=True, message_id=new_id)
        except Exception as e:
            logger.exception("Chat2GO send 失败")
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # chat2go.cn 网页有 typing 指示器（前端 typingIndicator），
        # 但底层是按"AI 在写消息"的状态推断，不需要显式发 typing 事件。
        # 留空即可。
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # MVP：暂不支持原生图片发送，把 URL 当成文本带过去
        text = f"{caption or ''}\n{image_url}".strip()
        return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        room = self._rooms.get(chat_id)
        if room:
            return {
                "name": room.get("name") or chat_id,
                "type": "group",
                "chat_id": chat_id,
            }
        # 缓存里没有，去 DB 查一次
        try:
            r = await self._sb.table("rooms").select("name,industry").eq("id", chat_id).maybe_single().execute()
            if r.data:
                return {
                    "name": r.data.get("name") or chat_id,
                    "type": "group",
                    "chat_id": chat_id,
                }
        except Exception:
            pass
        return {"name": chat_id, "type": "group", "chat_id": chat_id}
