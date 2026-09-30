#!/usr/bin/env python3
"""skill -- a repeatable procedure, written to the canonical skill repo at
~/agent-skills/skills/<name>/SKILL.md with required frontmatter (name,
description), then linked into every agent by `agent-skills sync` (see
_skills_root). RISK config: changes future session behaviour, never
auto-installed."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _skills_root
from registry import register

TYPE = "skill"
RISK = "config"
SCHEMA = {
    "required": ["name", "description", "content"],
    "types": {"name": str, "description": str, "content": str},
}
SKILLS_ROOT = _skills_root.SKILLS_ROOT


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
    # Writing the file is only half the install: until `agent-skills sync`
    # links it, no agent can see it. Skipped for a test/tmp target outside the
    # canonical root, which nothing is meant to link.
    note = None
    if target.is_relative_to(_skills_root.SKILLS_ROOT):
        note = _skills_root.sync()
    return {"ok": True, "path": str(target), "command": None, "exit_code": None,
            "output": None, "note": note, "error": None}


register(sys.modules[__name__])
