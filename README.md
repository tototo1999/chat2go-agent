# Chat2GO Agent

Chat2GO 的本地 AI Agent。大咖在自己电脑上跑这个进程，把 Claude / OpenAI / Gemini / DeepSeek / Qwen 等大模型接入 Chat2GO 调试室。

## 架构

```
小白发消息 → Supabase messages 表 → Realtime 推到本地 bridge
              ↓
       chat2go-agent (本地 Python 进程)
       ┌─────────────────────────────────┐
       │ SOUL.md  ← 大咖人格              │
       │ Skills   ← 行业 prompt 包         │
       │ Memory   ← 长期事实（Phase A 只读）│
       │ Adapters ← 多模型适配             │
       └─────────────────────────────────┘
              ↓
       LLM API（Anthropic / OpenAI / Gemini / ...）
              ↓
       AI 回复 → 写回 Supabase → Realtime 推给前端
```

## 安装

```bash
git clone https://github.com/<你>/chat2go-agent
cd chat2go-agent
pip install -e .
```

## 配置

**第 1 步：凭证**
```bash
mkdir -p ~/.chat2go
cp credentials.yaml.example ~/.chat2go/credentials.yaml
# 编辑 ~/.chat2go/credentials.yaml，填入你的 API key
```

或使用 `.env` 兜底（环境变量优先于 yaml）：
```bash
cp .env.example .env
# 编辑 .env
```

**第 2 步：大咖人格（可选）**
```bash
cp chat2go_agent/templates/SOUL.md.example ~/.chat2go/SOUL.md
# 编辑 ~/.chat2go/SOUL.md，写你的大咖人格
```

不填的话使用默认通用人格。

## 使用

```bash
# 默认 demo 大咖账号启动
chat2go-agent

# 指定大咖账号
chat2go-agent --email expert@example.com --password xxx

# 指定默认模型
chat2go-agent --model anthropic/claude-sonnet-4-5

# 房间级别设置 system prompt
chat2go-agent set-prompt <room_id> "你是 X 行业专家..."

# 房间级别设置模型
chat2go-agent set-model <room_id> deepseek/deepseek-chat
```

## 支持的模型

| Provider | 协议 | 模型示例 |
|---|---|---|
| anthropic | Anthropic 原生 | `anthropic/claude-sonnet-4-5`, `anthropic/claude-haiku-4-5` |
| openai | OpenAI 原生 | `openai/gpt-5`, `openai/gpt-5-mini` |
| deepseek | OpenAI 兼容 | `deepseek/deepseek-chat`, `deepseek/deepseek-reasoner` |
| qwen | OpenAI 兼容 | `qwen/qwen-max`, `qwen/qwen-plus` |
| kimi | OpenAI 兼容 | `kimi/moonshot-v1-128k` |
| glm | OpenAI 兼容 | `glm/glm-4-plus` |
| gemini | Google 原生 | `gemini/gemini-2-pro` |

## 内置行业 Skill

`chat2go_agent/skills/` 自带 6 个行业：外贸 / 健身 / 地产 / 教育 / 量化 / 医疗。

大咖可在 `~/.chat2go/skills/<name>/SKILL.md` 添加自己的行业包，会自动覆盖同名内置 skill。

## 路线图

- ✅ Phase A：SOUL + Skills + Memory（只读）+ Sessions + Multi-model Adapters
- ⏳ Phase B：Tool loop + Lessons 自动沉淀 + 服务端 PDF 生成
- ⏳ Phase 1：Multi-model Router（按任务复杂度自动选模型，省 60% 成本）
- ⏳ Phase 2：知识库 RAG（pgvector）+ Skills 系统升级
