#!/usr/bin/env python3
"""On-disk staging contract for Phase 4a (design spec Component 5). Mirrors
jobs_lib.py's role: one place that knows the staging/<shortcode>/ layout, so
interpret.py, install_artifact.py and telegram_bot.py never hand-build paths.
See PLAN.md sec 7 Phase 4 and the design spec's "Data / schema additions"."""
import json
import re
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
    interpreted -- makes the sweep safe to re-run. Checks for proposal.json
    specifically, not mere directory existence: a staging dir holding only
    agent-error.txt means a PREVIOUS attempt failed (e.g. an agent-call
    error, or -- found live during the Task 13 real sweep -- hitting the
    backend's own API rate/spend limit mid-run) and must stay retryable, not
    be mistaken for a completed interpretation."""
    return proposal_path(shortcode, staging_root).exists()


def list_pending(staging_root):
    """Shortcodes with status == 'pending', oldest proposal creation first.

    New proposals persist ``created_at``. Older proposal files fall back to
    their filesystem timestamp, so changing mutable fields such as
    ``message_id`` never reorders modern pending work.
    """
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
                created_at = data.get("created_at")
                if not created_at:
                    created_at = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
                candidates.append((created_at, d.name))
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
    # The shortcode leads, always. It used to appear only as a *fallback* for a
    # missing summary, which meant every real proposal rendered without its id
    # -- and a shortcode is the only handle chat, the docs, logs/discarded.jsonl
    # and staging/ all share. Dan could read /pending on his phone and still had
    # no way to tell which message was the one being discussed. First position
    # so a column of proposals is scannable without reading each summary.
    # Still one message per post (new posts arrive per video), but each pending
    # action renders as the same card /pending uses, labelled with its id so
    # Pick… buttons stay mappable.
    summary = proposal.get("summary")
    lines = [f"{proposal['shortcode']} — {summary}" if summary else proposal["shortcode"]]
    for action in proposal["actions"]:
        if action.get("status", "pending") != "pending":
            continue
        lines += ["", format_action_card(proposal, action, describe_fns.get(action["type"]), label=action["id"])]
    return "\n".join(lines)


def pending_action_items(staging_root):
    """Every pending action across pending proposals, oldest post first, as
    (shortcode, proposal, action). /pending lists these flat; the shortcode
    keeps each action tied to the post it came from."""
    items = []
    for shortcode in list_pending(staging_root):
        proposal = read_proposal(shortcode, staging_root)
        items.extend((shortcode, proposal, a) for a in proposal["actions"] if a.get("status") == "pending")
    return items


INSTALL_TYPE_PRIORITY = {"skill_install": 0, "package_install": 1, "git_repo": 2}


def action_key(action):
    """Pure: what an action installs, so the same tool suggested by several
    posts -- even as different action types (a skill_install and a git_repo of
    the same repo) -- is one decision. None for actions that never merge."""
    payload = action.get("payload") or {}
    action_type = action.get("type")
    if action_type not in INSTALL_TYPE_PRIORITY:
        return None
    # Decided actions were never enriched, so the repo is also read straight
    # from a GitHub URL or an `npx skills add owner/repo` command.
    raw_repo = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", (payload.get("url") or "").strip()) \
        or re.search(r"\bskills\s+add\s+([\w.-]+/[\w.-]+)", payload.get("command") or "")
    repo = ((action.get("evidence") or {}).get("repo")
            or (payload.get("source") if action_type == "skill_install" else None)
            or (raw_repo.group(1) if raw_repo else None))
    if repo:
        return f"repo:{repo.lower()}"
    if action_type == "skill_install":
        return f"skill:{(payload.get('name') or '').lower()}"
    if action_type == "package_install":
        return f"pkg:{payload.get('manager')}:{payload.get('package')}"
    return f"url:{(payload.get('url') or '').lower().rstrip('/').removesuffix('.git')}"


def _all_proposals(staging_root):
    root = Path(staging_root)
    for path in sorted(root.glob("*/proposal.json")) if root.exists() else []:
        try:
            yield json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue


def pending_action_groups(staging_root):
    """Pending actions merged by action_key, in order of first appearance.
    Each group: {key, items: [(shortcode, proposal, action)], installed_elsewhere}.
    items[0] is the one to run -- a skill_install beats a package_install
    beats a plain clone of the same repo."""
    installed = {action_key(a) for p in _all_proposals(staging_root) for a in p.get("actions", [])
                 if a.get("status") == "installed"} - {None}
    groups, by_key = [], {}
    for position, item in enumerate(pending_action_items(staging_root)):
        key = action_key(item[2])
        group = by_key.get(key) if key else None
        if group is None:
            group = {"key": key, "items": [], "installed_elsewhere": key in installed}
            groups.append(group)
            if key:
                by_key[key] = group
        group["items"].append((position, item))
    for group in groups:
        group["items"] = [item for _, item in sorted(
            group["items"], key=lambda pi: (INSTALL_TYPE_PRIORITY.get(pi[1][2]["type"], 9), pi[0]))]
    return groups


_WARNINGS = {"low_adoption": "very few stars", "solo_author": "one-person project",
             "stale": "no updates in over a year", "not_in_directory": "not on skills.sh",
             "no_licence": "no licence", "archived": "archived by its owner"}


def plain_warning(flags, evidence, installed_elsewhere=False):
    """One ⚠️ line in plain words from enrich.py's flags, or '' when nothing looks off."""
    flags = flags or []
    phrases = ["already installed from another post"] if installed_elsewhere else []
    source = ((evidence or {}).get("provenance") or {}).get("source")
    if "self_promoted_in_comment" in flags:
        phrases.append("only recommended in a comment that links its own repo")
    elif "not_in_video" in flags:
        phrases.append("only mentioned in a comment, not the post itself" if source == "comment"
                       else "not named in the post (the agent inferred it)")
    phrases += [text for flag, text in _WARNINGS.items() if flag in flags]
    if not phrases:
        return ""
    line = "; ".join(phrases)
    return f"⚠️ {line[0].upper()}{line[1:]}"


def format_action_card(proposal, action, describe_fn, label=None, posts=1, installed_elsewhere=False):
    """One action: its stars, what it is, why it was suggested, and a warning
    only if something looks off. /pending sends one per message; a new post's
    notification stacks them (with `label` = action id)."""
    evidence = action.get("evidence") or {}
    github = evidence.get("github") or {}
    title = describe_fn(action["payload"]) if describe_fn else action["type"]
    head = f"{risk_badge(action.get('risk'))} " + (f"[{label}] " if label else "") + title
    if github.get("status") == "ok" and github.get("stars") is not None:
        head += f" · {_compact(github['stars'])}★"
    if posts > 1:
        head += f" · suggested in {posts} posts"
    explanation = action.get("explanation") or {}
    what = explanation.get("what") or github.get("description") or (evidence.get("readme") or {}).get("head")
    why = explanation.get("why") or proposal.get("summary")
    lines = [head]
    if what:
        lines.append(f"What: {what}")
    if why:
        lines.append(f"Why: {why}")
    warning = plain_warning(action.get("flags"), evidence, installed_elsewhere)
    if warning:
        lines.append(warning)
    return "\n".join(lines)


def _compact(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 100_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _provenance_phrase(provenance):
    source = provenance.get("source") or "unknown"
    if source == "agent_inferred":
        return "not named in the post (agent-inferred)"
    if source == "comment" and provenance.get("comment_author"):
        return f"from comment by @{provenance['comment_author']}"
    return f"from {source}"


def format_evidence_block(action):
    """The full evidence for Show full, including the quoted comment and its author."""
    evidence = action.get("evidence")
    if not evidence:
        return ""
    provenance = evidence.get("provenance") or {}
    line = f"Provenance: {_provenance_phrase(provenance)}"
    if provenance.get("matched") and provenance.get("source") != "comment":
        line += f' (named as "{provenance["matched"]}")'
    lines = ["Evidence:", line]
    if provenance.get("source") == "comment":
        if provenance.get("comment_links_repo"):
            lines.append("  That comment itself links the repo it recommends.")
        lines.append(f"  “{provenance.get('comment_text') or ''}”")
    repo = evidence.get("repo")
    if repo:
        lines.append(f"Repo: https://github.com/{repo}")
    github = evidence.get("github")
    if github and github.get("status") == "ok":
        contributors = github.get("contributors")
        lines.append(f"GitHub: {github.get('stars')}★, {github.get('forks')} forks, "
                     f"{contributors} contributor{'' if contributors == 1 else 's'}, "
                     f"created {github.get('created_at')}, "
                     f"last push {github.get('pushed_at')}, licence {github.get('licence') or 'none'}"
                     + (", ARCHIVED" if github.get("archived") else ""))
        if github.get("description"):
            lines.append(f"  {github['description']}")
    elif github:
        lines.append(f"GitHub: unavailable ({github.get('error')})")
    directory = evidence.get("directory")
    if directory and directory.get("status") == "ok":
        if directory.get("listed"):
            lines.append(f"skills.sh: listed, {directory.get('installs')} installs"
                         + (", dominant for this name" if directory.get("dominant") else ""))
        else:
            lines.append("skills.sh: not listed")
    elif directory:
        lines.append(f"skills.sh: unavailable ({directory.get('error')})")
    readme = evidence.get("readme")
    if readme and readme.get("status") == "ok" and readme.get("head"):
        lines.append(f"README: {readme['head']}")
    lines.append(f"Flags: {', '.join(action.get('flags') or []) or 'none'}")
    lines.append(f"Checked: {evidence.get('checked_at')}")
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
