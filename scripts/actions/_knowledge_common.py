#!/usr/bin/env python3
"""Shared helpers for the knowledge-family handlers (reference, note) that
install into the canonical captured-knowledge skill -- see that skill's
SKILL.md for the on-disk layout this maintains."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _skills_root

# Canonical, Git-versioned, and readable by Claude, Codex and Antigravity alike
# once synced -- ~/.claude/skills/captured-knowledge is now just a symlink to it.
CAPTURED_KNOWLEDGE_ROOT = _skills_root.SKILLS_ROOT / "captured-knowledge"
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


def ensure_linked(skill_root):
    """Link captured-knowledge into the agent directories the first time a
    capture creates it. Cheap afterwards (one stat), and a tmp_path root in
    tests never reaches the CLI. Returns a note for the result line, or None."""
    skill_root = Path(skill_root)
    if skill_root != CAPTURED_KNOWLEDGE_ROOT:
        return None
    if (Path.home() / ".claude" / "skills" / skill_root.name).exists():
        return None
    return _skills_root.sync()
