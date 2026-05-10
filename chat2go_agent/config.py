"""配置加载：环境变量 > ~/.chat2go/credentials.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── Supabase（与 chat.html / login.html 保持一致）──
SUPABASE_URL = "https://qjnagbzqhoansixqharb.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFqbmFnYnpxaG9hbnNpeHFoYXJiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgzNDIxODIsImV4cCI6MjA5MzkxODE4Mn0"
    ".GpMUVTk6JvqeciXagXQiJunc8TLFMHg3_b9reIjJ2Y8"
)

# Demo 默认大咖账号
DEFAULT_EXPERT_EMAIL = "lirui88888862@gmail.com"
DEFAULT_EXPERT_PASSWORD = "123456"

# 用户配置目录
CHAT2GO_HOME = Path(os.environ.get("CHAT2GO_HOME") or Path.home() / ".chat2go")

# 环境变量到 provider 的映射
ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "QWEN_API_KEY",
    "kimi": "KIMI_API_KEY",
    "glm": "GLM_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# 各 provider 的默认 base_url（用于 OpenAI 兼容 adapter）
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "openrouter": "https://openrouter.ai/api/v1",
}


@dataclass
class ProviderCreds:
    api_key: str = ""
    base_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class Credentials:
    providers: dict[str, ProviderCreds] = field(default_factory=dict)
    default_model: str = "anthropic/claude-sonnet-4-5"

    def get(self, provider: str) -> ProviderCreds | None:
        creds = self.providers.get(provider)
        return creds if creds and creds.configured else None

    def configured_providers(self) -> list[str]:
        return [name for name, c in self.providers.items() if c.configured]


def load_dotenv(path: Path | None = None) -> None:
    """Best-effort .env 加载。
    已有非空环境变量优先（保留 export 覆盖能力）。
    已有空值视作"未设置"，会被 .env 里的值替换。
    """
    candidates = [path] if path else [Path(".env"), CHAT2GO_HOME / ".env"]
    for f in candidates:
        if not f or not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not os.environ.get(k):  # 未设置 or 空值
                os.environ[k] = v


def load_credentials() -> Credentials:
    """
    合并三个来源（优先级从高到低）：
      1. 环境变量（ANTHROPIC_API_KEY 等）
      2. ~/.chat2go/credentials.yaml
      3. 内置默认 base_url
    """
    yaml_data: dict[str, Any] = {}
    yaml_file = CHAT2GO_HOME / "credentials.yaml"
    if yaml_file.exists():
        try:
            yaml_data = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print(f"[config] 解析 {yaml_file} 失败：{e}")

    creds = Credentials()
    creds.default_model = (
        yaml_data.get("defaults", {}).get("model")
        or creds.default_model
    )

    for provider, env_key in ENV_KEY_MAP.items():
        yaml_block = yaml_data.get(provider, {}) or {}
        api_key = os.environ.get(env_key) or yaml_block.get("api_key", "") or ""
        base_url = (
            yaml_block.get("base_url")
            or DEFAULT_BASE_URLS.get(provider, "")
        )
        if api_key and api_key.startswith("sk-xxx"):  # example placeholder
            api_key = ""
        creds.providers[provider] = ProviderCreds(api_key=api_key.strip(), base_url=base_url)

    return creds
