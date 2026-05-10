"""CLI 入口：`chat2go-agent ...` / `python -m chat2go_agent ...`"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .bridge import Chat2GOBridge, cmd_rooms, cmd_send, cmd_set_model, cmd_set_prompt
from .config import (
    DEFAULT_EXPERT_EMAIL,
    DEFAULT_EXPERT_PASSWORD,
    load_credentials,
    load_dotenv,
)


def main() -> None:
    load_dotenv()  # 优先级：env > yaml
    creds = load_credentials()

    parser = argparse.ArgumentParser(prog="chat2go-agent", description="Chat2GO 本地 Agent")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument(
        "--model",
        default="",
        help="默认模型（provider/name 格式，如 anthropic/claude-sonnet-4-5）。"
        "未指定时用 ~/.chat2go/credentials.yaml 的 defaults.model。",
    )

    sub = parser.add_subparsers(dest="cmd")

    p_prompt = sub.add_parser("set-prompt", help="设置房间 system prompt")
    p_prompt.add_argument("room_id")
    p_prompt.add_argument("prompt")
    p_prompt.add_argument("--email")
    p_prompt.add_argument("--password")

    p_model = sub.add_parser("set-model", help="设置房间默认模型")
    p_model.add_argument("room_id")
    p_model.add_argument("model")
    p_model.add_argument("--email")
    p_model.add_argument("--password")

    p_rooms = sub.add_parser("rooms", help="列出当前大咖的所有调试室")
    p_rooms.add_argument("--email")
    p_rooms.add_argument("--password")

    p_send = sub.add_parser("send", help="以大咖身份往房间发消息（可被 hermes / shell 调用）")
    p_send.add_argument("room", help="房间 id（前缀也行）或 name")
    p_send.add_argument("content", help="消息内容")
    p_send.add_argument("--silent", action="store_true",
                        help="以 AI 身份发，bridge 不再触发 AI 回复（避免循环）")
    p_send.add_argument("--role", choices=["expert", "ai", "user"], default="expert",
                        help="发言角色（默认 expert）。--silent 等价于 --role ai")
    p_send.add_argument("--email")
    p_send.add_argument("--password")

    args = parser.parse_args()

    email = args.email or DEFAULT_EXPERT_EMAIL
    password = args.password or DEFAULT_EXPERT_PASSWORD

    if args.cmd == "set-prompt":
        asyncio.run(cmd_set_prompt(args.room_id, args.prompt, email, password))
        return
    if args.cmd == "set-model":
        asyncio.run(cmd_set_model(args.room_id, args.model, email, password))
        return
    if args.cmd == "rooms":
        asyncio.run(cmd_rooms(email, password))
        return
    if args.cmd == "send":
        asyncio.run(cmd_send(args.room, args.content, email, password,
                             role=args.role, silent=args.silent))
        return

    if not creds.configured_providers():
        print("[bridge] ⚠️  没有任何 provider 配置了 API key。")
        print("[bridge]    请在 ~/.chat2go/credentials.yaml 或环境变量里设置至少一个。")
        print("[bridge]    最简单：export ANTHROPIC_API_KEY=sk-ant-xxx")
        sys.exit(1)

    print(f"[bridge] 已配 provider：{', '.join(creds.configured_providers())}")
    print(f"[bridge] 默认模型：{args.model or creds.default_model}")

    bridge = Chat2GOBridge(
        email=email,
        password=password,
        creds=creds,
        default_model=args.model,
    )

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n[bridge] 已退出。")


if __name__ == "__main__":
    main()
