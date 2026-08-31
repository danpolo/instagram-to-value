#!/usr/bin/env python3
"""rule -- an always-on constraint, written to ~/.claude/rules/<topic>.md.
RISK config: never automatic, per the design spec's staging-gate rationale."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "rule"
RISK = "config"
SCHEMA = {
    "required": ["topic", "content"],
    "types": {"topic": str, "content": str},
}
RULES_ROOT = Path.home() / ".claude" / "rules"


def _slug(topic):
    return re.sub(r"[^a-z0-9-]+", "-", topic.lower()).strip("-") or "rule"


def target_path(payload):
    return RULES_ROOT / f"{_slug(payload['topic'])}.md"


def describe(payload):
    return f"New global rule: {payload['topic']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
