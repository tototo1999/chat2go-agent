# Hermes 平台插件（chat2go）

让 hermes 把 chat2go.cn 当成原生 IM 渠道（和 Discord/Telegram 平起平坐），
而不是通过 chat2go-agent 中转。

## 工作模式对比

| 模式 | 说明 | 延迟 | 是否需要 chat2go-agent |
|---|---|---|---|
| chat2go-agent + hermes subprocess | 默认（chat2go-agent 当桥，每条消息 fork 一次 hermes） | ~30s | ✅ 必需 |
| **hermes native plugin（本目录）** | hermes 直接订阅 chat2go.cn Realtime，常驻进程 | ~2-5s | ❌ 可不用 |

## 安装步骤

### 1. 拷贝插件文件到 hermes（4 个改动点）

```bash
# 假设 hermes 安装在 ~/.hermes/hermes-agent/
HERMES=~/.hermes/hermes-agent

# (1) adapter 主体
cp hermes_plugin/chat2go.py $HERMES/gateway/platforms/chat2go.py

# (2)(3)(4) 需要手动 patch 三个文件（见下面）
```

### 2. 手动 patch 三处

**`gateway/config.py`**：
- `Platform` enum 加 `CHAT2GO = "chat2go"`
- `_apply_env_overrides()` 末尾加 `CHAT2GO_TOKEN` 环境变量加载

**`gateway/run.py`**：
- `_create_adapter()` 末尾加 `elif platform == Platform.CHAT2GO:` 分支

**`hermes_cli/gateway.py`**：
- 平台 list 末尾加 chat2go 项

具体 diff 见 `patches/` 目录（见 chat2go-agent 仓库的提交记录）。

### 3. 装 supabase-py 到 hermes venv

```bash
$HERMES/venv/bin/python -m pip install supabase
```

如果 hermes venv 没 pip：
```bash
curl -sSL https://bootstrap.pypa.io/get-pip.py | $HERMES/venv/bin/python -
$HERMES/venv/bin/python -m pip install supabase
```

### 4. 配置 token

到 chat2go.cn → 大咖设置 → Agent 连接密钥 → 生成新密钥（一次性显示）

```bash
export CHAT2GO_TOKEN=c2g-key_xxxxxxxx
```

或写入 `.env`：
```
CHAT2GO_TOKEN=c2g-key_xxxxxxxx
```

### 5. 启动

```bash
hermes gateway run
```

或用 setup wizard：
```bash
hermes gateway setup
# 选 17. Chat2GO.ai（大咖营业窗口）
# 粘贴 token
```

## 验证

```bash
hermes gateway status
# 应显示 chat2go 在 Connected platforms 里
```

到 chat2go.cn 网页给某个房间发条消息，hermes 日志里应该有：
```
[gateway] Chat2GO.ai authenticated: lirui88888862@gmail.com (expert=fbb9ab4b)
[gateway] Chat2GO.ai: loaded 2 rooms
[handle_message] platform=chat2go room=58e76f63 ...
```

## hermes 升级后重新装

hermes 是上游项目，升级时本插件会被覆盖。重新装：

```bash
chat2go-agent install-hermes-plugin    # 自动重新拷贝（TODO：未实现）
```

或手动重做上面 1-3 步。

## 与 chat2go-agent 的关系

启用 hermes native plugin 后：
- chat2go-agent 的 launchd / start.sh 不再需要（hermes 接管了 bridge 角色）
- chat2go-agent CLI 仍然可用（`chat2go-agent rooms / send / connect / whoami`）
- 你可以**同时**跑两个，但会重复回复（不推荐）。建议二选一。

推荐迁移路径：
1. 先在 hermes 跑通（保留 chat2go-agent 不动）
2. 验证 hermes 能稳定收发消息
3. `launchctl unload ~/Library/LaunchAgents/ai.chat2go.bridge.plist` 关掉 chat2go-agent
4. 完全切到 hermes
