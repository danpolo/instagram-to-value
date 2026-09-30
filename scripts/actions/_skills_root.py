#!/usr/bin/env python3
"""Where a skill actually lives, shared by every handler that writes one.

Skills on this machine have a single source of truth: the Git-versioned
~/agent-skills/skills/<name>/ repo. `agent-skills sync` then fans each skill
out as an *individual symlink* into every agent's native discovery directory
(~/.claude/skills, ~/.codex/skills, ~/.gemini/config/skills, ~/.agents/skills).

These handlers used to write straight into ~/.claude/skills/, which is now a
consumer directory: a skill landing there is unversioned, invisible to Codex
and Antigravity, and liable to be shuffled by the next sync. So every
skill-writing handler targets the canonical root and then syncs."""
import subprocess
import sys
from pathlib import Path

AGENT_SKILLS_ROOT = Path.home() / "agent-skills"
SKILLS_ROOT = AGENT_SKILLS_ROOT / "skills"
# The repo's own entrypoint, not a PATH lookup: worker.py runs non-interactive
# with whatever PATH systemd/cron handed it, and ~/.local/bin is exactly the
# kind of thing that is missing there. (~/.local/bin/agent-skills is a symlink
# to this file anyway.)
SYNC_BIN = AGENT_SKILLS_ROOT / "bin" / "agent-skills"
SYNC_TIMEOUT_S = 120


def sync():
    """Link the canonical skills into every agent directory.

    Returns None on success, or a short note for the Telegram result line when
    the sync could not run -- a skill written but unlinked is invisible to
    every agent, and must not be reported as a silent success. Never raises:
    this runs inside an approval tap.
    """
    if not SYNC_BIN.exists():
        return f"written to {SKILLS_ROOT}, but {SYNC_BIN} is missing — not linked into any agent directory"
    try:
        # cwd pinned to $HOME on purpose: agent-skills walks up from cwd looking
        # for a project root, and a worker started inside this repo would
        # otherwise also sync this project's skills as a side effect.
        result = subprocess.run([str(SYNC_BIN), "sync"], cwd=str(Path.home()),
                                 capture_output=True, text=True, timeout=SYNC_TIMEOUT_S)
    except Exception as e:  # timeout, missing interpreter, permissions
        return f"agent-skills sync failed: {e}"
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-300:]
        return f"agent-skills sync exit {result.returncode}: {tail}"
    return None


if __name__ == "__main__":  # tiny manual check: `python3 _skills_root.py`
    print(sync() or f"synced {SKILLS_ROOT}")
    sys.exit(0)
