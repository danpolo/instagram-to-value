#!/usr/bin/env python3
"""unsupported -- the agent named a real desired action but no handler exists
yet. Never installs; logs to logs/unsupported_actions.jsonl, forming the
pilot's ranked backlog (design spec's "The pilot loop"). RISK inert."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "unsupported"
RISK = "inert"
SCHEMA = {
    "required": ["proposed_type", "payload_sketch", "rationale"],
    "types": {"proposed_type": str, "payload_sketch": dict, "rationale": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UNSUPPORTED_LOG = REPO_ROOT / "logs" / "unsupported_actions.jsonl"


def target_path(payload):
    return None


def describe(payload):
    return f"Unsupported: wants {payload['proposed_type']} — {payload['rationale']}"


def preview(payload):
    return json.dumps(payload, indent=2, ensure_ascii=False)


def collides(payload):
    return None


def install(payload, target=None, context=None):
    UNSUPPORTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "shortcode": (context or {}).get("shortcode"),
        "proposed_type": payload["proposed_type"],
        "payload_sketch": payload["payload_sketch"],
        "rationale": payload["rationale"],
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(UNSUPPORTED_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(UNSUPPORTED_LOG), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
