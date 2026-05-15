"""
Chat2GO.Ai platform adapter for Hermes.

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
    cache_image_from_url,
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
        logger.warning("Chat2GO.Ai: supabase-py 未安装（pip install supabase）")
        return False


class Chat2GoAdapter(BasePlatformAdapter):
    """Chat2GO.Ai 平台 adapter（订阅 Supabase Realtime + 写回消息）。"""

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
        # 每个房间最近的图片记录，给后续 text-only 消息当上下文（防止 AI 说「我没收到截图」）
        # room_id -> list of {name, ts(iso str), cached_path}
        self._room_image_log: Dict[str, list] = {}
        self._IMG_LOG_MAX = 10           # 最多保留每房间最近 10 张
        self._IMG_LOG_TTL_SEC = 3600     # 只追溯 1 小时内的图片
        # 记录每房间最后一条触发消息（msg_id + user_id），写 model_usage 时用
        self._last_trigger: Dict[str, Dict[str, str]] = {}
        # AI 写回 chat2go 用的默认模型标识（前端 shortModelName 会美化）
        # 真正的 token 数 hermes brain 没回传给 adapter，这里写 stub 0 让 UI 能渲染。
        self._stub_model = os.getenv("CHAT2GO_STUB_MODEL", "anthropic/claude-sonnet-4-6")

        logger.info(
            "Chat2GO.Ai initialized: url=%s token=%s***",
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
                "Chat2GO.Ai authenticated: %s (expert=%s)",
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
            logger.exception("Chat2GO.Ai connect 失败")
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
        logger.info("Chat2GO.Ai: loaded %d rooms", len(self._rooms))

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
            logger.warning("Chat2GO.Ai realtime subscribe 失败（用轮询兜底）: %s", e)

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
                    logger.debug("Chat2GO.Ai poll error: %s", e)
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
            attachments = msg.get("attachments") or []

            # 处理图片附件：下载到 hermes 本地 cache，让 vision 工具能读
            media_urls: list[str] = []
            media_types: list[str] = []
            non_image_attachments: list[tuple[str, str]] = []   # (name, url) 非图片走文本提示
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                url = att.get("url") or ""
                mime = (att.get("mime_type") or "").lower()
                name = att.get("name") or "file"
                if not url:
                    continue
                is_image = mime.startswith("image/") or any(
                    name.lower().endswith(e)
                    for e in (".png", ".jpg", ".jpeg", ".gif", ".webp")
                )
                if is_image:
                    # 从 url 提取扩展名供 cache_image_from_url 用
                    ext = "." + (mime.split("/", 1)[1] if "/" in mime else "jpg")
                    if ext == "./" or ext == ".jpeg":
                        ext = ".jpg"
                    try:
                        cached_path = await cache_image_from_url(url, ext=ext)
                        media_urls.append(cached_path)
                        media_types.append(mime or "image/jpeg")
                        # 记到房间图片历史里，给后续 text-only 消息提供上下文
                        log = self._room_image_log.setdefault(room_id, [])
                        log.append({
                            "name": name,
                            "ts": msg.get("created_at") or "",
                            "cached_path": cached_path,
                        })
                        if len(log) > self._IMG_LOG_MAX:
                            del log[: len(log) - self._IMG_LOG_MAX]
                        logger.info(
                            "Chat2GO.Ai: cached image %s → %s", name, cached_path
                        )
                    except Exception as e:
                        logger.warning(
                            "Chat2GO.Ai: cache image %s 失败：%s", name, e
                        )
                else:
                    non_image_attachments.append((name, url))

            # 非图片附件：直接预提取文本（PDF/DOCX/TXT 等）追加到 content
            # 不让 AI 用 execute_code 解析，避免触发 hermes 的命令审批拦截
            if non_image_attachments:
                lines = ["", "【附件内容】"]
                for name, url in non_image_attachments:
                    text = await _extract_attachment_text(name, url)
                    lines.append(f"\n--- 文件：{name} ---\n{text}\n--- 文件结束 ---")
                content = content + "\n".join(lines)

            # 当前消息没新图，但本房间最近 1 小时有过图 → 附加 system 提示
            # 防止 AI 跨轮丢失上下文后说「我没收到截图」
            if not media_urls:
                from datetime import timezone
                now = datetime.now(timezone.utc)
                recent = []
                for entry in self._room_image_log.get(room_id, []):
                    try:
                        ets = datetime.fromisoformat(
                            entry["ts"].replace("Z", "+00:00")
                        )
                        if (now - ets).total_seconds() <= self._IMG_LOG_TTL_SEC:
                            recent.append(entry)
                    except Exception:
                        pass
                if recent:
                    items = "\n".join(
                        f"  · {e['name']}（路径：{e['cached_path']}）"
                        for e in recent[-5:]
                    )
                    content = (content or "") + (
                        "\n\n[系统提示｜本房间最近图片历史]\n"
                        f"小白/大咖此前发过 {len(recent)} 张图，已由网关 vision_analyze 处理过。"
                        "你的对话历史里能找到对应的 tool_result（图片分析结果），那就是图。\n"
                        "最近 5 张：\n" + items + "\n"
                        "**绝对不要说「我没有收到截图」**——图确实已传入并已被分析。"
                        "如果记不清细节，回滚查上一轮 AI 回复，或再次调用 vision_analyze 看缓存路径。"
                    )

            # 记下本房间最近的触发消息，send() 时写 model_usage stub 用
            self._last_trigger[room_id] = {
                "msg_id": str(msg_id),
                "user_id": msg.get("user_id") or "",
            }

            event = MessageEvent(
                text=content,
                message_type=MessageType.PHOTO if media_urls else MessageType.TEXT,
                source=self.build_source(
                    chat_id=room_id,
                    chat_name=room.get("name") or "调试室",
                    chat_type="group",  # chat2go 房间是三方对话（小白+大咖+AI）
                    user_id=msg.get("user_id"),
                    user_name=user_label,
                    chat_topic=room.get("industry"),
                ),
                message_id=str(msg_id),
                media_urls=media_urls,
                media_types=media_types,
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
            # 写一行 model_usage stub，让 chat2go web UI 能渲染模型名/用量徽章
            # hermes brain 不回传 token 计数，这里用 0；前端 fallback context window
            try:
                trigger = self._last_trigger.get(chat_id) or {}
                if trigger.get("user_id") and self._expert_id:
                    # returning="minimal" 跳过 RETURNING；
                    # 列级 GRANT 上 cost_usd / cost_source 不可读，否则 INSERT...RETURNING 会触发 permission denied
                    await self._sb.table("model_usage").insert({
                        "message_id": new_id,
                        "room_id": chat_id,
                        "expert_id": self._expert_id,
                        "triggered_by": trigger["user_id"],
                        "model": self._stub_model,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_source": "online",
                    }, returning="minimal").execute()
            except Exception as me:
                logger.warning("Chat2GO.Ai model_usage stub 失败: %s", me)
            return SendResult(success=True, message_id=new_id)
        except Exception as e:
            logger.exception("Chat2GO.Ai send 失败")
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


async def _extract_attachment_text(name: str, url: str, max_chars: int = 30000) -> str:
    """
    下载非图片附件并提取文本。失败返回描述字符串（不抛异常，让 AI 看到原因）。
    支持：txt/md/csv/json/html/xml/log、pdf、docx
    """
    name_lower = name.lower()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        return f"[下载失败: {e}]"

    text = ""

    # 文本类
    text_exts = (".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml", ".log")
    if name_lower.endswith(text_exts):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")

    # PDF
    elif name_lower.endswith(".pdf"):
        try:
            from io import BytesIO
            import pypdf
            reader = pypdf.PdfReader(BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except ImportError:
            return "[PDF 解析需要 pypdf：在 hermes venv 装 pip install pypdf]"
        except Exception as e:
            return f"[PDF 解析失败: {e}]"

    # DOCX
    elif name_lower.endswith(".docx"):
        try:
            from io import BytesIO
            import docx as _docx
            doc = _docx.Document(BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return "[DOCX 解析需要 python-docx]"
        except Exception as e:
            return f"[DOCX 解析失败: {e}]"

    else:
        return f"[不支持的文件类型，可访问 URL: {url}]"

    text = text.strip()
    if not text:
        return "[文件文本为空 / 无法提取]"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... 文件过长，已截断到 {max_chars} 字符 ...]"
    return text
