"""HermesBrain：把 chat2go 调试室对话委托给本地 hermes CLI。

调用方式：
  hermes chat -q "<拼好的 query 字符串>" -Q -v -m <provider/model> [-s <skill>]

  -Q  quiet：只输出最终回复 + Session: 行
  -v  verbose：把 LLM API 调用细节（含 token usage）打到 stderr
  -m  指定模型（可选；不指定走 hermes 自己的 default）
  -s  指定 skill（可选）

token usage 提取：
  hermes -v 在 stderr 里每次 LLM 调用打一行：
    Token usage: prompt=19,870, completion=8, total=19,878
  多 turn 时多行 → 累加。

⚠️ 已知限制（MVP 阶段接受）：
  - 图片附件暂不传给 hermes（只在 query 文本里带 URL 提示）
  - prompt token 包含 cache_read（hermes 用 prompt caching），轻微高估成本
  - hermes 实际用的模型可能与 ctx.model 不同（hermes config 优先），billing 仍按请求模型算
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..adapters.base import Usage
from . import BrainContext, BrainResult

# hermes -v 输出的 token usage 详细行（含 cache 信息）
# 例：API Response received - ... Usage: Usage(cache_creation=CacheCreation(...),
#     cache_creation_input_tokens=0, cache_read_input_tokens=19867, ...,
#     input_tokens=3, output_tokens=8, ...)
# 嵌套括号无法用简单 regex 匹配整块，改成"含 Usage( 的行 + input_tokens 必须出现"双条件判断
_USAGE_DETAIL_MARKER = "Usage("
_FIELD_RE = re.compile(r"\b(\w+)=(\d+)")
# 旧的简单 fallback：只有 prompt + completion
_USAGE_SIMPLE_RE = re.compile(
    r"Token usage:\s*prompt=([\d,]+),\s*completion=([\d,]+)"
)
# 匹配 hermes 输出末尾的 "session_id: <id>" 或 "Session: <id>" 行
_SESSION_FOOTER_RE = re.compile(r"\n*[Ss]ession[_ ]?id?:.*$", re.MULTILINE)
# init banner 起始 emoji（即使 -Q 模式 hermes 仍会打印）
_INIT_LINE_RE = re.compile(r"^\s*[⚠✅\U0001f916\U0001f511\U0001f6e0\U0001f4be\U0001f4ca\U0001f310\U0001f680]")
# init 结尾的标记行（包含 "Context limit:"）—— 之后是真正的 AI 回复
_INIT_END_MARKER = "Context limit:"


class HermesBrain:
    name = "hermes"

    def __init__(self, hermes_bin: str, timeout: int = 300):
        self.hermes_bin = hermes_bin
        self.timeout = timeout

    async def call(self, ctx: BrainContext) -> BrainResult:
        query = self._format_query(ctx)
        cmd = [
            self.hermes_bin, "chat",
            "-q", query,
            "-Q",        # 静默模式（只 stdout 最终回复）
            "-v",        # verbose（stderr 含 token usage）
            "--ignore-rules",  # 不需要 hermes 工程上下文
        ]
        if ctx.model:
            cmd += ["-m", ctx.model]

        # 把 chat2go 的 industry 当作 skill 名传给 hermes
        # （前提：大咖在 ~/.hermes/skills/ 里建了同名 skill；没建就忽略）
        industry = (ctx.room.get("industry") or "").strip()
        if industry and self._has_hermes_skill(industry):
            cmd += ["-s", industry]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"hermes 超时（>{self.timeout}s）")
        except FileNotFoundError:
            raise RuntimeError(f"找不到 hermes 二进制：{self.hermes_bin}")

        stderr = stderr_b.decode(errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(
                f"hermes 退出码 {proc.returncode}。stderr 末尾：{stderr[-500:]}"
            )

        text = extract_hermes_reply(stdout_b.decode(errors="replace"))
        usage = parse_usage_from_stderr(stderr)
        return BrainResult(text=text, usage=usage, model=ctx.model)

    @staticmethod
    def _has_hermes_skill(name: str) -> bool:
        """检查 ~/.hermes/skills/<name>/SKILL.md 是否存在。"""
        return (Path.home() / ".hermes" / "skills" / name / "SKILL.md").exists()

    @staticmethod
    def _format_query(ctx: BrainContext) -> str:
        """把对话历史 + 附件 + 当前消息拼成单 query 字符串给 hermes。"""
        parts: list[str] = []
        industry = (ctx.room.get("industry") or "通用").strip()
        parts.append(f"[Chat2GO.ai 调试室 · 行业={industry}]")

        if ctx.history:
            parts.append("\n【对话历史】")
            for m in ctx.history:
                role = m.get("role")
                label = {"user": "小白", "expert": "大咖", "ai": "你（AI）"}.get(role, str(role))
                content = (m.get("content") or "").strip()
                atts = m.get("attachments") or []
                if atts:
                    names = ", ".join(a.get("name", "?") for a in atts)
                    content = f"{content} [附件: {names}]"
                if content:
                    parts.append(f"{label}: {content}")

        if ctx.image_urls:
            parts.append("\n【小白本次发来图片】")
            for url, mime in ctx.image_urls:
                parts.append(f"- {mime}: {url}")

        if ctx.attachment_texts:
            parts.append("\n【小白本次上传的文件】")
            for fname, ftext in ctx.attachment_texts:
                parts.append(f"\n--- {fname} ---\n{ftext}\n--- 文件结束 ---")

        parts.append(f"\n【小白最新消息】\n{ctx.current_message}")
        parts.append("\n请回复小白的最新消息。")
        return "\n".join(parts)


def parse_usage_from_stderr(stderr: str) -> Usage:
    """
    从 hermes -v 的 stderr 提取 token 用量。
    多 turn 累加。优先用详细 'Usage(...)' 摘要（含 cache_*）；
    没有则退回简单的 'Token usage: prompt=X, completion=Y'（cache 全归零）。
    """
    total = Usage()
    matched_detail = False

    for line in stderr.splitlines():
        if _USAGE_DETAIL_MARKER not in line:
            continue
        fields = {k: int(v) for k, v in _FIELD_RE.findall(line)}
        # 必须有 input_tokens 才认为是有效的 Usage 行
        # （其他 ephemeral_* 内层字段不会满足这个条件）
        if "input_tokens" not in fields:
            continue
        matched_detail = True
        total.input_tokens += fields["input_tokens"]
        total.output_tokens += fields.get("output_tokens", 0)
        total.cache_creation_input_tokens += fields.get("cache_creation_input_tokens", 0)
        total.cache_read_input_tokens += fields.get("cache_read_input_tokens", 0)

    if matched_detail:
        return total

    # Fallback：简单格式（'prompt' 已含 cache_read，无法拆分 → 全当 fresh，会高估）
    for m in _USAGE_SIMPLE_RE.finditer(stderr):
        total.input_tokens += int(m.group(1).replace(",", ""))
        total.output_tokens += int(m.group(2).replace(",", ""))
    return total


def extract_hermes_reply(stdout: str) -> str:
    """
    从 hermes -Q 的 stdout 提取真正的 AI 回复。
    -Q 模式下 hermes 仍会打印 ⚠️/🤖/✅/🛠️/💾/📊 起始的 init banner，
    以及 'Context limit: ...' 结尾标记。真正的回复在 init 之后。

    策略：
      1. 找最后一个 'Context limit:' 行，取它之后的内容
      2. 找不到 → 退化为过滤已知 init emoji 行
      3. 末尾 strip 掉 'session_id: ...' / 'Session: ...'
    """
    text = _SESSION_FOOTER_RE.sub("", stdout).strip()
    lines = text.splitlines()

    # 优先：找 init 末尾标记
    marker = -1
    for i, line in enumerate(lines):
        if _INIT_END_MARKER in line:
            marker = i
    if marker >= 0:
        reply = "\n".join(lines[marker + 1:]).strip()
        if reply:
            return reply

    # Fallback：过滤 init emoji 行
    kept = [line for line in lines if not _INIT_LINE_RE.match(line)]
    return "\n".join(kept).strip()
