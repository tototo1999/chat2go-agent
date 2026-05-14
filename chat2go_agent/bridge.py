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

from .adapters import split_model
from .attachments import extract_attachment_text, split_image_and_text_attachments
from .auth import fetch_otp, login_with_connection_key
from .brains import BrainContext, build_brain, find_hermes_bin, resolve_brain_name
from .config import (
    DEFAULT_EXPERT_EMAIL,
    DEFAULT_EXPERT_PASSWORD,
    SUPABASE_ANON_KEY,
    SUPABASE_URL,
    Credentials,
)
from .memory import prefetch_memory
from .pricing import calculate_charge
from .soul import load_skills, load_soul, select_skill_by_industry


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


async def _fetch_history(sb, room_id: str, channel: str = "main", limit: int = 12) -> list:
    r = (
        await sb.table("messages")
        .select("role,content,attachments,created_at")
        .eq("room_id", room_id)
        .eq("channel", channel)
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
        self.skills = load_skills()
        self.soul = load_soul()
        self.hermes_bin = find_hermes_bin()
        # 预实例化两种 brain：避免每次房间消息都重建
        self._brain_cache: dict = {}
        self.sb: Optional[AsyncClient] = None
        self.expert_id: Optional[str] = None
        self.rooms: dict = {}
        self.processing: set = set()

    def get_brain(self, name: str):
        """缓存 brain 实例。"""
        if name not in self._brain_cache:
            self._brain_cache[name] = build_brain(self.creds, name)
        return self._brain_cache[name]

    async def login(self):
        self.sb = await acreate_client(SUPABASE_URL, SUPABASE_ANON_KEY)

        # 优先用 connection_key 换 magiclink session（正式大咖路径）
        if self.creds.connection_key:
            try:
                info = await login_with_connection_key(self.sb, self.creds.connection_key)
                self.expert_id = info["expert_id"]
                print(f"[bridge] 已通过 connection_key 登录："
                      f"{info['email']} (id={self.expert_id[:8]}…)")
                await self._sync_realtime_auth()
                return
            except Exception as e:
                print(f"[bridge] connection_key 登录失败：{e}")
                print(f"[bridge] 退回 email/password 路径…")

        # 退路：email/password（dev / 老配置兼容）
        resp = await self.sb.auth.sign_in_with_password(
            {"email": self.email, "password": self.password}
        )
        self.expert_id = resp.user.id
        print(f"[bridge] 已登录：{self.email}  (id={self.expert_id[:8]}…)")
        await self._sync_realtime_auth()

    async def _sync_realtime_auth(self):
        """把当前 JWT 显式同步给 realtime websocket，
        否则 RLS 收紧后 realtime 默认按 anon 判权限，消息推不过来。"""
        try:
            session = await self.sb.auth.get_session()
            token = getattr(session, "access_token", None) if session else None
            if token:
                await self.sb.realtime.set_auth(token)
                print(f"[bridge] realtime auth 已同步 (token={token[:12]}…)")
            else:
                print(f"[bridge][warn] 没拿到 access_token，realtime 可能按 anon 判 RLS")
        except Exception as e:
            print(f"[bridge][warn] realtime set_auth 失败: {e}")

    async def _refresh_session_loop(self):
        """后台续命：每 ~50 分钟主动刷新一次 access_token 并重新同步给 realtime。
        Supabase access_token 默认 1 小时过期；不刷新就会卡死在 'JWT expired'。"""
        while True:
            try:
                await asyncio.sleep(50 * 60)  # 50 分钟，提前 10 分钟刷
                resp = await self.sb.auth.refresh_session()
                new_token = getattr(getattr(resp, "session", None), "access_token", None)
                if new_token:
                    await self.sb.realtime.set_auth(new_token)
                    print(f"[bridge] 自动续命 token={new_token[:12]}…")
                else:
                    print(f"[bridge][warn] refresh_session 没返回新 token")
            except Exception as e:
                print(f"[bridge][warn] refresh_session 失败: {e}")
                # 失败重试间隔短一点
                await asyncio.sleep(60)

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
        channel = msg.get("channel") or "main"

        # Realtime payload 有时不含 attachments（jsonb 字段可能被省略）
        # 如果 attachments 为空，回查数据库补全
        if not attachments and msg_id:
            try:
                row = (await self.sb.table("messages").select("attachments").eq("id", msg_id).maybe_single().execute())
                if row and row.data:
                    attachments = row.data.get("attachments") or []
                    if attachments:
                        print(f"[bridge] 补全附件 from DB: {len(attachments)} 个")
            except Exception as e:
                print(f"[bridge][warn] 补全附件失败: {e}")

        if msg_id in self.processing:
            return
        self.processing.add(msg_id)

        # expert_user: 大咖 ↔ 小白 私聊，AI 不参与
        if channel == "expert_user":
            print(f"[bridge] skip msg in channel=expert_user (AI 不参与) id={msg_id}")
            self.processing.discard(msg_id)
            return

        room = self.rooms.get(room_id)
        if not room:
            self.processing.discard(msg_id)
            return

        # 真人互@ → AI 不响应（消息里 @ 的是非 AI 的人）
        import re as _re
        ai_name = (room.get("ai_name") or "AI 助手").strip()
        _AI_ALIASES = {ai_name, "AI 助手", "AI", "ai"}
        _mentions = _re.findall(r"@([^\s@,，。?？!！]+)", content or "")
        _human_mentions = [n for n in _mentions if n not in _AI_ALIASES]
        if _human_mentions:
            print(f"[bridge] skip msg (人类互@: {_human_mentions}) id={msg_id}")
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
            history = await _fetch_history(self.sb, room_id, channel=channel, limit=12)
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

            # 3. 选 brain（房间 > yaml 默认 > auto）
            brain_name = resolve_brain_name(self.creds, room)
            brain = self.get_brain(brain_name)
            print(f"[bridge] brain={brain_name}")

            # 4. 委托给 brain
            ctx = BrainContext(
                room=room,
                soul=self.soul,
                skill=skill,
                memory_ctx=memory_ctx,
                history=history,
                current_message=content,
                image_urls=image_urls,
                attachment_texts=attachment_texts,
                model=model,
            )
            result = await brain.call(ctx)
            ai_text = _normalize_markdown(result.text)
            if not ai_text:
                ai_text = f"（{brain_name}/{provider} 返回空回复）"

            print(
                f"[bridge] [{room['name']}] AI: "
                f"{ai_text[:80]}{'…' if len(ai_text) > 80 else ''}"
            )

            # 5. 写回 AI 消息（保持同一 channel）
            ai_resp = await self.sb.table("messages").insert(
                {
                    "room_id": room_id,
                    "user_id": self.expert_id,
                    "role": "ai",
                    "channel": channel,
                    "type": "markdown" if _looks_like_markdown(ai_text) else "text",
                    "content": ai_text,
                }
            ).execute()

            # 6. 写 model_usage（计费观测）
            try:
                ai_msg_id = (ai_resp.data or [{}])[0].get("id")
                commission = float(
                    room.get("commission_pct") or self.creds.default_commission_pct
                )
                rate = float(
                    room.get("exchange_rate_to_cny") or self.creds.default_exchange_rate
                )
                charge = calculate_charge(
                    model=model,
                    usage=result.usage,
                    commission_pct=commission,
                    exchange_rate=rate,
                    local_prices=self.creds.local_prices,
                )
                await self.sb.table("model_usage").insert(
                    {
                        "message_id": ai_msg_id,
                        "room_id": room_id,
                        "expert_id": self.expert_id,
                        "triggered_by": sender_user_id,
                        "model": model,
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cache_creation_input_tokens": result.usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": result.usage.cache_read_input_tokens,
                        "cost_source": charge.cost_source,
                        "cost_usd": round(charge.cost_usd, 6),
                        "commission_pct": commission,
                        "exchange_rate": rate,
                        "user_charge_cny": round(charge.user_charge_cny, 4),
                    },
                    returning="minimal",  # ★ 不要 RETURNING *（cost_usd 列被 GRANT 屏蔽，否则 42501）
                ).execute()
                u = result.usage
                cache_note = ""
                if u.cache_read_input_tokens or u.cache_creation_input_tokens:
                    cache_note = (
                        f" [fresh={u.input_tokens} "
                        f"cache_w={u.cache_creation_input_tokens} "
                        f"cache_r={u.cache_read_input_tokens}]"
                    )
                print(
                    f"[bridge] usage: in_total={u.total_input_tokens} "
                    f"out={u.output_tokens}{cache_note} "
                    f"cost=${charge.cost_usd:.4f} "
                    f"charge=¥{charge.user_charge_cny:.4f} ({charge.cost_source})"
                )
            except Exception as e:
                print(f"[bridge] 计费写入失败（不阻断主流程）：{e}")

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
            print(f"[bridge][debug] 收到空 payload: keys={list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")
            return

        role = msg.get("role")
        room_id = msg.get("room_id")
        channel = msg.get("channel", "main")

        print(f"[bridge][debug] realtime 收到事件: role={role} room={str(room_id)[:8]}… channel={channel} known_rooms={list(self.rooms.keys())[:3]}")

        if role == "ai":
            return
        if room_id not in self.rooms:
            print(f"[bridge][debug] 跳过：room {str(room_id)[:8]}… 不在我管理的房间列表里")
            return

        print(f"[bridge][debug] 派发 handle_message: id={str(msg.get('id'))[:8]}…")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.handle_message(msg))
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(self.handle_message(msg), self._loop)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        await self.login()
        await self.load_rooms()

        # 后台 token 续命：每 50 分钟刷新一次，防 JWT expired
        asyncio.create_task(self._refresh_session_loop())

        if not self.rooms:
            print("[bridge] 没有属于你的调试室。请先在网页上新建一个调试室。")
            return

        print(f"[bridge] 默认模型：{self.default_model}")
        print(f"[bridge] 默认 brain：{self.creds.default_brain}（hermes 二进制：{self.hermes_bin or '未装'}）")
        print(f"[bridge] 已配 provider：{', '.join(sorted(self.creds.configured_providers())) or '(无)'}")
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
                try:
                    await self.load_rooms()
                except Exception as e:
                    print(f"[bridge][poll] load_rooms 失败（网络抖动？）: {e}")
                    continue
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
        # 兼容老写法：无 provider 前缀时默认 anthropic；gemini 模型须显式带前缀
        model = f"anthropic/{model}"
    await sb.table("rooms").update({"model": model}).eq("id", room_id).execute()
    print(f"[bridge] room {room_id[:8]}… model 已更新为 {model}。")


async def _resolve_room(sb, expert_id: str, ref: str) -> dict | None:
    """ref 可以是 room id（前缀也行）或 name。返回 room 行 or None。"""
    rooms = (await sb.table("rooms").select("*").eq("expert_id", expert_id).execute()).data or []
    ref_l = ref.strip().lower()
    # 先按 id 全匹配
    for r in rooms:
        if r["id"].lower() == ref_l:
            return r
    # 按 id 前缀（>= 4 位）
    if len(ref_l) >= 4:
        prefix_matches = [r for r in rooms if r["id"].lower().startswith(ref_l)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    # 按 name 完全匹配
    for r in rooms:
        if (r.get("name") or "").lower() == ref_l:
            return r
    # 按 name 前缀
    name_matches = [r for r in rooms if (r.get("name") or "").lower().startswith(ref_l)]
    if len(name_matches) == 1:
        return name_matches[0]
    return None


async def cmd_rooms(email: str, password: str) -> None:
    """列出当前大咖的所有调试室。"""
    sb = await _login_for_admin(email, password)
    expert_id = (await sb.auth.get_user()).user.id
    rooms = (await sb.table("rooms").select("id,name,industry,brain,model,created_at")
             .eq("expert_id", expert_id)
             .order("created_at", desc=True)
             .execute()).data or []
    if not rooms:
        print("(无调试室)")
        return
    print(f"{'id':<10} {'name':<20} {'industry':<10} {'brain':<10} {'model':<35} created_at")
    for r in rooms:
        print(
            f"{r['id'][:8]:<10} "
            f"{(r.get('name') or '')[:18]:<20} "
            f"{(r.get('industry') or '-'):<10} "
            f"{(r.get('brain') or '-'):<10} "
            f"{(r.get('model') or '-')[:33]:<35} "
            f"{r['created_at'][:19]}"
        )


async def cmd_connect(connection_key: str) -> None:
    """
    验证 connection_key 并写入 ~/.chat2go/credentials.yaml。
    """
    import yaml as _yaml
    from .config import CHAT2GO_HOME

    print(f"[connect] 验证 key …")
    try:
        otp = await fetch_otp(connection_key)
    except Exception as e:
        print(f"[connect] ❌ 验证失败：{e}")
        return

    print(f"[connect] ✅ 验证成功！")
    print(f"[connect]    email：{otp['email']}")
    print(f"[connect]    expert_id：{otp['expert_id'][:8]}…")

    # 写入 yaml（合并已有内容）
    CHAT2GO_HOME.mkdir(parents=True, exist_ok=True)
    yaml_file = CHAT2GO_HOME / "credentials.yaml"
    data = {}
    if yaml_file.exists():
        try:
            data = _yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError:
            print(f"[connect] ⚠️  现有 yaml 解析失败，会覆盖")

    data.setdefault("chat2go", {})
    data["chat2go"]["connection_key"] = connection_key

    yaml_file.write_text(
        _yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"[connect] 已写入 {yaml_file}")
    print(f"[connect] 下一步：启动 bridge → chat2go-agent（或 launchctl load ...）")


async def cmd_whoami(email: str, password: str, creds=None) -> None:
    """显示当前 chat2go-agent 用谁的身份连接。"""
    if creds and creds.connection_key:
        sb = await acreate_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        try:
            info = await login_with_connection_key(sb, creds.connection_key)
            print(f"[whoami] 模式：connection_key")
            print(f"[whoami] email：{info['email']}")
            print(f"[whoami] expert_id：{info['expert_id']}")
            r = await sb.table("profiles").select("role,display_name").eq(
                "user_id", info["expert_id"]).maybe_single().execute()
            if r.data:
                print(f"[whoami] role：{r.data.get('role')}")
                print(f"[whoami] 名字：{r.data.get('display_name')}")
        except Exception as e:
            print(f"[whoami] connection_key 验证失败：{e}")
        return
    print(f"[whoami] 模式：email/password")
    print(f"[whoami] email：{email}")


async def cmd_send(room_ref: str, content: str, email: str, password: str,
                   role: str = "expert", silent: bool = False) -> None:
    """
    以大咖身份往房间发消息。

    role='expert'（默认）：bridge 会响应，AI 给小白回复
    role='ai'：直接以 AI 身份发，bridge 不会再触发回复（仅供脚本/skill 使用）
    silent=True 等价于 role='ai'（一个直观别名）
    """
    sb = await _login_for_admin(email, password)
    expert_id = (await sb.auth.get_user()).user.id
    room = await _resolve_room(sb, expert_id, room_ref)
    if room is None:
        print(f"[bridge] 找不到房间 {room_ref!r}（试 chat2go-agent rooms 列出全部）")
        return
    if silent:
        role = "ai"
    r = await sb.table("messages").insert({
        "room_id": room["id"],
        "user_id": expert_id,
        "role": role,
        "type": "text",
        "content": content,
    }).execute()
    msg_id = (r.data or [{}])[0].get("id", "?")
    print(f"[bridge] 已发到「{room['name']}」({room['id'][:8]}…) "
          f"role={role} id={msg_id[:8]}…")
    if role == "expert":
        print("[bridge] 注意：bridge 会把这条当作大咖发言，触发 AI 回复（如不希望，加 --silent）")
