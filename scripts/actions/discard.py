#!/usr/bin/env python3
"""discard -- the post/action offers nothing worth keeping. Never writes to
an allowlisted path; appends one line to logs/discarded.jsonl so the reason
tunes the classifier later (PLAN.md sec 5's discard row, sec 8 risk 4). RISK
inert. Distinct from telegram_bot.py's "❌ Discard" button, which rejects a
whole *proposal* -- this is one *action* the agent itself proposed discarding
(e.g. "this carousel is a personal essay, nothing to capture")."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "discard"
RISK = "inert"
SCHEMA = {
    "required": ["reason"],
    "types": {"reason": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DISCARDED_LOG = REPO_ROOT / "logs" / "discarded.jsonl"


def target_path(payload):
    return None  # internal bookkeeping only, not an agent-drafted install path


def describe(payload):
    return f"Discard: {payload['reason']}"


def preview(payload):
    return payload["reason"]


def collides(payload):
    return None


def install(payload, target=None, context=None):
    DISCARDED_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "shortcode": (context or {}).get("shortcode"),
        "reason": payload["reason"],
        "excerpt": (context or {}).get("transcript_excerpt"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(DISCARDED_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(DISCARDED_LOG), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
