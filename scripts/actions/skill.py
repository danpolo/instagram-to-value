#!/usr/bin/env python3
"""skill -- a repeatable procedure, written to
~/.claude/skills/<name>/SKILL.md with required frontmatter (name,
description). RISK config: changes future session behaviour, never
auto-installed."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "skill"
RISK = "config"
SCHEMA = {
    "required": ["name", "description", "content"],
    "types": {"name": str, "description": str, "content": str},
}
SKILLS_ROOT = Path.home() / ".claude" / "skills"


def _slug(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"


def target_path(payload):
    return SKILLS_ROOT / _slug(payload["name"]) / "SKILL.md"


def describe(payload):
    return f"New skill: {payload['name']} — {payload['description']}"


def _build_skill_md(payload):
    return f"---\nname: {_slug(payload['name'])}\ndescription: {payload['description']}\n---\n\n{payload['content']}\n"


def preview(payload):
    return _build_skill_md(payload)


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_build_skill_md(payload))
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
