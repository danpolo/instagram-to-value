#!/usr/bin/env python3
"""On-disk staging contract for Phase 4a (design spec Component 5). Mirrors
jobs_lib.py's role: one place that knows the staging/<shortcode>/ layout, so
interpret.py, install_artifact.py and telegram_bot.py never hand-build paths.
See PLAN.md sec 7 Phase 4 and the design spec's "Data / schema additions"."""
import json
from datetime import datetime, timezone
from pathlib import Path

RISK_BADGES = {"inert": "🟢", "config": "🟡", "exec": "🔴"}

TYPE_CATEGORY = {
    "package_install": "tools", "git_repo": "tools", "binary_release": "tools",
    "script": "tools", "docker_stack": "tools", "model_download": "tools", "shell_snippet": "tools",
    "reference": "references", "note": "references", "bookmark": "references",
    "dataset_or_stat": "references", "reading_item": "references",
    "rule": "rules", "skill": "rules", "command": "rules", "subagent": "rules",
    "workflow": "rules", "settings_patch": "rules", "mcp_server": "rules", "plugin": "rules",
    "calendar_event": "time_people", "reminder": "time_people",
    "follow_account": "time_people", "queue_post": "time_people",
}
AUTO_DISCARD_CONFIDENCE_THRESHOLD = 0.8


def staging_dir(shortcode, staging_root):
    return Path(staging_root) / shortcode


def proposal_path(shortcode, staging_root):
    return staging_dir(shortcode, staging_root) / "proposal.json"


def write_proposal(shortcode, staging_root, proposal):
    path = proposal_path(shortcode, staging_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False))
    return path


def read_proposal(shortcode, staging_root):
    path = proposal_path(shortcode, staging_root)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def has_staging(shortcode, staging_root):
    """Used by interpret.py's backfill sweep to skip shortcodes already
    interpreted -- makes the sweep safe to re-run."""
    return staging_dir(shortcode, staging_root).exists()


def list_pending(staging_root):
    """Shortcodes with status == 'pending', oldest proposal.json first."""
    staging_root = Path(staging_root)
    if not staging_root.exists():
        return []
    candidates = []
    for d in staging_root.iterdir():
        p = d / "proposal.json"
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") == "pending":
                candidates.append((p.stat().st_mtime, d.name))
    candidates.sort()
    return [name for _, name in candidates]


def update_action_status(shortcode, staging_root, action_id, status, result=None):
    proposal = read_proposal(shortcode, staging_root)
    if proposal is None:
        raise FileNotFoundError(f"no proposal for {shortcode}")
    for action in proposal["actions"]:
        if action["id"] == action_id:
            action["status"] = status
            action["result"] = result
            action["decided_at"] = datetime.now(timezone.utc).isoformat()
            break
    else:
        raise ValueError(f"no action {action_id} in proposal for {shortcode}")
    if all(a["status"] != "pending" for a in proposal["actions"]):
        proposal["status"] = "decided"
    write_proposal(shortcode, staging_root, proposal)
    return proposal


def set_message_id(shortcode, staging_root, message_id):
    proposal = read_proposal(shortcode, staging_root)
    if proposal is None:
        return
    proposal["message_id"] = message_id
    write_proposal(shortcode, staging_root, proposal)


def record_reject_reason(shortcode, staging_root, reason, logs_root=None):
    """The whole-proposal '❌ Discard' button (design spec's 'Reject'
    section) -- distinct from the per-action `discard` action type. Marks
    every still-pending action 'skipped' and logs the reason."""
    logs_root = Path(logs_root) if logs_root else Path(staging_root).parent / "logs"
    proposal = read_proposal(shortcode, staging_root)
    logs_root.mkdir(parents=True, exist_ok=True)
    record = {
        "shortcode": shortcode, "reason": reason,
        "excerpt": (proposal or {}).get("summary", ""),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(logs_root / "discarded.jsonl", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if proposal is not None:
        for action in proposal["actions"]:
            if action["status"] == "pending":
                action["status"] = "skipped"
                action["decided_at"] = datetime.now(timezone.utc).isoformat()
        proposal["status"] = "decided"
        write_proposal(shortcode, staging_root, proposal)


def risk_badge(risk):
    return RISK_BADGES.get(risk, "❔")


def format_proposal_message(proposal, describe_fns):
    """describe_fns: {type: describe(payload) callable} -- the registry's
    per-handler describe(), passed in rather than imported here to keep this
    module free of an actions/ import (staging_lib is pure state, registry is
    classification -- the design spec keeps these as separate components)."""
    lines = [proposal.get("summary") or proposal["shortcode"]]
    resolver = proposal.get("resolver") or {}
    if resolver.get("status"):
        tool_bit = f" ({resolver['tool_name']})" if resolver.get("tool_name") else ""
        lines.append(f"Tool resolver: {resolver['status']}{tool_bit}")
    for action in proposal["actions"]:
        describe_fn = describe_fns.get(action["type"])
        text = describe_fn(action["payload"]) if describe_fn else action["type"]
        lines.append(f"{risk_badge(action['risk'])} [{action['id']}] `{action['type']}` — {text}")
    return "\n".join(lines)


def is_auto_discard_eligible(origin, action_type, confidence, threshold=AUTO_DISCARD_CONFIDENCE_THRESHOLD):
    """Design spec: backfill origin only, type == discard, confidence >=
    threshold. Never on a hand-sent URL (origin == 'telegram')."""
    return origin == "backfill" and action_type == "discard" and confidence >= threshold


def format_digest(proposals):
    """proposals: list of proposal dicts from one backfill sweep. Design
    spec's digest line: "14 proposals: 6 tools, 4 references, 2 rules, 2
    notes · 9 auto-discarded · ⚠️ 3 unsupported types"."""
    counts = {}
    auto_discarded = 0
    unsupported_posts = set()
    for proposal in proposals:
        for action in proposal["actions"]:
            if action["type"] == "discard" and action.get("status") == "auto_discarded":
                auto_discarded += 1
            elif action["type"] == "unsupported":
                unsupported_posts.add(proposal["shortcode"])
            else:
                category = TYPE_CATEGORY.get(action["type"])
                if category:
                    counts[category] = counts.get(category, 0) + 1
    parts = ", ".join(f"{n} {cat}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    line = f"{len(proposals)} proposals" + (f": {parts}" if parts else "")
    if auto_discarded:
        line += f" · {auto_discarded} auto-discarded"
    if unsupported_posts:
        line += f" · ⚠️ {len(unsupported_posts)} unsupported types"
    return line


def aggregate_unsupported(unsupported_log_path):
    """Counts of proposed_type across the whole logs/unsupported_actions.jsonl
    -- cumulative, not scoped to one sweep, matching the pilot's "ranked
    backlog" framing."""
    unsupported_log_path = Path(unsupported_log_path)
    counts = {}
    if not unsupported_log_path.exists():
        return counts
    for line in unsupported_log_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        proposed = record.get("proposed_type", "unknown")
        counts[proposed] = counts.get(proposed, 0) + 1
    return counts


def format_unsupported_summary(counts):
    if not counts:
        return ""
    total_posts = sum(counts.values())
    parts = ", ".join(f"{t} ×{n}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"⚠️ {total_posts} posts wanted actions I can't do yet: {parts}."
