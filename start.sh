#!/usr/bin/env bash
# 一键启动 Chat2GO Agent
set -e
cd "$(dirname "$0")"

# 杀掉已有 bridge 进程，避免重复
pkill -f "chat2go_agent" 2>/dev/null || true
pkill -f "chat2go/bridge\.py" 2>/dev/null || true   # 老仓库残留
sleep 0.5

if [ ! -f .env ] && [ ! -f "$HOME/.chat2go/credentials.yaml" ]; then
  echo "[start] ⚠️  没有凭证：请创建 .env 或 ~/.chat2go/credentials.yaml"
  echo "    最简单：echo 'ANTHROPIC_API_KEY=sk-ant-xxx' > .env"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[start] 初始化 venv..."
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import chat2go_agent" 2>/dev/null; then
  echo "[start] 安装 chat2go-agent..."
  .venv/bin/pip install -q -e .
fi

echo "[start] 启动 chat2go-agent..."
exec .venv/bin/python -m chat2go_agent "$@"
