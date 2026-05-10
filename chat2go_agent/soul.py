"""SOUL.md + Skill 加载。

加载顺序：
  1. 内置 skills/  ← 包内自带（外贸/健身/地产/教育/量化/医疗）
  2. ~/.chat2go/skills/  ← 大咖私有，覆盖同名内置 skill
  3. ~/.chat2go/SOUL.md  ← 大咖人格（可选）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import CHAT2GO_HOME

PACKAGE_ROOT = Path(__file__).resolve().parent
BUILTIN_SKILLS_DIR = PACKAGE_ROOT / "skills"
USER_SKILLS_DIR = CHAT2GO_HOME / "skills"
USER_SOUL_FILE = CHAT2GO_HOME / "SOUL.md"


@dataclass
class Skill:
    name: str
    display_name: str
    industry_trigger: str  # 触发的 room.industry 值
    keywords: list[str]
    body: str  # SKILL.md 去掉 frontmatter 之后的正文
    source: str  # 'builtin' | 'user'


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_skill(skill_md: Path, source: str) -> Skill | None:
    text = skill_md.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None

    name = meta.get("name") or skill_md.parent.name
    triggers = meta.get("triggers", {}) or {}
    return Skill(
        name=name,
        display_name=meta.get("display_name", name),
        industry_trigger=str(triggers.get("industry", "")).strip(),
        keywords=list(triggers.get("keywords") or []),
        body=text[m.end():].strip(),
        source=source,
    )


def load_skills() -> dict[str, Skill]:
    """合并内置 + 用户 skill。同名时用户优先。"""
    skills: dict[str, Skill] = {}

    for skill_dir in [BUILTIN_SKILLS_DIR, USER_SKILLS_DIR]:
        if not skill_dir.exists():
            continue
        for sub in sorted(skill_dir.iterdir()):
            if not sub.is_dir():
                continue
            skill_md = sub / "SKILL.md"
            if not skill_md.exists():
                continue
            source = "user" if skill_dir == USER_SKILLS_DIR else "builtin"
            skill = _parse_skill(skill_md, source=source)
            if skill:
                skills[skill.name] = skill  # 用户的覆盖同名内置

    return skills


def select_skill_by_industry(skills: dict[str, Skill], industry: str) -> Skill | None:
    """按 room.industry 字段匹配 skill。"""
    industry = (industry or "").strip()
    if not industry:
        return None
    for skill in skills.values():
        if skill.industry_trigger == industry:
            return skill
    return None


def load_soul() -> str:
    """加载大咖人格。没有就返回空串，prompt_builder 会用通用 fallback。"""
    if USER_SOUL_FILE.exists():
        return USER_SOUL_FILE.read_text(encoding="utf-8").strip()
    return ""
