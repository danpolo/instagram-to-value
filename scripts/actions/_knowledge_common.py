#!/usr/bin/env python3
"""Shared helpers for the knowledge-family handlers (reference, note) that
install into ~/.claude/skills/captured-knowledge/ -- see that skill's
SKILL.md for the on-disk layout this maintains."""
import re
from pathlib import Path

CAPTURED_KNOWLEDGE_ROOT = Path.home() / ".claude" / "skills" / "captured-knowledge"
INDEX_MARKER = "## Index"


def slug(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "item"


def append_index_line(skill_md_path, line):
    """Append one bullet under '## Index' in SKILL.md, creating the file with
    a minimal frontmatter + Index section if it's somehow missing (defensive
    only -- the real one is created explicitly, not by this fallback).
    Mirrors the pattern this Claude installation's own auto-memory
    MEMORY.md already uses: one line per fact, content lives in its own
    file."""
    skill_md_path = Path(skill_md_path)
    if skill_md_path.exists():
        text = skill_md_path.read_text()
    else:
        skill_md_path.parent.mkdir(parents=True, exist_ok=True)
        text = f"---\nname: captured-knowledge\ndescription: Captured knowledge.\n---\n\n{INDEX_MARKER}\n"
    if INDEX_MARKER not in text:
        text = text.rstrip() + f"\n\n{INDEX_MARKER}\n"
    text = text.rstrip("\n") + f"\n- {line}\n"
    skill_md_path.write_text(text)
