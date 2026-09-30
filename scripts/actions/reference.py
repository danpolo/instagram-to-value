#!/usr/bin/env python3
"""reference -- a fact or pointer, filed in the global captured-knowledge
skill rather than the per-project auto-memory directory (design spec's
"Where knowledge actions install": the auto-memory path is per-$HOME-session,
not global). RISK inert: a markdown file nothing executes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register
from _knowledge_common import CAPTURED_KNOWLEDGE_ROOT, append_index_line, ensure_linked, slug

TYPE = "reference"
RISK = "inert"
SCHEMA = {
    "required": ["title", "content"],
    "types": {"title": str, "content": str},
}


def target_path(payload):
    return CAPTURED_KNOWLEDGE_ROOT / "facts" / f"{slug(payload['title'])}.md"


def describe(payload):
    return f"Reference: {payload['title']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    # Skill root derived from `target` itself (target == <root>/facts/<slug>.md),
    # not the module-level constant -- keeps this testable against a tmp_path
    # target without ever touching the real captured-knowledge skill.
    append_index_line(target.parent.parent / "SKILL.md", f"{payload['title']} — facts/{target.name}")
    note = ensure_linked(target.parent.parent)
    return {"ok": True, "path": str(target), "command": None, "exit_code": None,
            "output": None, "note": note, "error": None}


register(sys.modules[__name__])
