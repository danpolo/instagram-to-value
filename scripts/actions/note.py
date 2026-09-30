#!/usr/bin/env python3
"""note -- a raw note filed in the global captured-knowledge skill (see
reference.py for the destination rationale). RISK inert."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register
from _knowledge_common import CAPTURED_KNOWLEDGE_ROOT, append_index_line, ensure_linked, slug

TYPE = "note"
RISK = "inert"
SCHEMA = {
    "required": ["title", "content"],
    "types": {"title": str, "content": str},
}


def target_path(payload):
    return CAPTURED_KNOWLEDGE_ROOT / "notes" / f"{slug(payload['title'])}.md"


def describe(payload):
    return f"Note: {payload['title']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    # Skill root derived from `target` itself (target == <root>/notes/<slug>.md),
    # not the module-level constant -- keeps this testable against a tmp_path
    # target without ever touching the real captured-knowledge skill.
    append_index_line(target.parent.parent / "SKILL.md", f"{payload['title']} — notes/{target.name}")
    note = ensure_linked(target.parent.parent)
    return {"ok": True, "path": str(target), "command": None, "exit_code": None,
            "output": None, "note": note, "error": None}


register(sys.modules[__name__])
