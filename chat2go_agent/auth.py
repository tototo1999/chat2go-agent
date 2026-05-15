"""Connection key → Supabase session 兑换。

流程：
  1. 大咖在 chat2go.ai 网页生成 c2g-key_xxx
  2. agent 调 Edge Function /functions/v1/agent-auth/exchange，传 key
  3. Edge 用 service_role + admin.generateLink 生成 OTP，返回 token_hash
  4. agent 用 supabase.auth.verify_otp(token_hash, type='magiclink') 拿到 session
  5. session 自带 access_token + refresh_token，supabase-py 自动续命

这是 Supabase 标准 auth 路径，不用手动签 JWT。
"""

from __future__ import annotations

import httpx

from .config import AGENT_AUTH_URL, SUPABASE_ANON_KEY


async def fetch_otp(connection_key: str, timeout: int = 30) -> dict:
    """
    用 c2g-key_xxx 调 Edge Function 拿 magiclink OTP token_hash。
    返回：{token_hash, email, expert_id, verification_type}
    """
    if not connection_key:
        raise RuntimeError("connection_key 为空")
    if not connection_key.startswith("c2g-key_"):
        raise RuntimeError(
            f"connection_key 格式错误（应以 c2g-key_ 开头）：{connection_key[:20]}…"
        )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            AGENT_AUTH_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "apikey": SUPABASE_ANON_KEY,
            },
            json={"key": connection_key},
        )

    if resp.status_code != 200:
        try:
            err = resp.json().get("error", resp.text[:300])
        except Exception:
            err = resp.text[:300]
        raise RuntimeError(f"agent-auth 失败 ({resp.status_code}): {err}")

    return resp.json()


async def login_with_connection_key(sb, connection_key: str) -> dict:
    """
    把 connection_key 转成 Supabase 的 session（access_token + refresh_token），
    并把 session 注入 supabase 客户端。返回 {expert_id, email, expires_at}。
    """
    otp = await fetch_otp(connection_key)
    token_hash = otp["token_hash"]
    email = otp["email"]
    expert_id = otp["expert_id"]

    # supabase-py 标准 verify_otp 流程，会自动管 refresh_token
    resp = await sb.auth.verify_otp({
        "token_hash": token_hash,
        "type": "magiclink",
    })

    # supabase-py 已把 session 设置进客户端，后续所有 .table() 调用都用 expert 身份
    return {
        "expert_id": expert_id,
        "email": email,
        "session": resp.session,
    }
