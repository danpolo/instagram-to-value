#!/usr/bin/env python3
"""Plain-English "what is this" and "why was it suggested" for every pending
action, written once by an agent and cached in proposal.json.

/pending shows a flat list of actions, one card each, not grouped by post --
so every card has to stand on its own: what the tool or artifact is, why it
was suggested, its stars, and a warning only when something looks off. The
facts come from scripts/enrich.py; this module turns them into two sentences.

One agent call per post (covering all of that post's pending actions), at
interpret time and via --pending for the existing backlog. Never raises: a
failed call leaves the action unexplained and the card falls back to the repo
description and the post summary.

Usage:
    python3 scripts/explain.py --pending [--staging-root DIR]
        [--extracted-root DIR] [--media-root DIR] [--backend claude|codex]
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agents_lib
import enrich
import staging_lib

MAX_CHARS = 300
EXPLAIN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["actions"],
    "properties": {"actions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "what", "why"],
        "properties": {"id": {"type": "string"}, "what": {"type": "string"}, "why": {"type": "string"}}}}},
}


def needs_explanation(action):
    return action.get("status") == "pending" and not action.get("explanation")


def _facts(action):
    evidence = action.get("evidence") or {}
    github = evidence.get("github") or {}
    provenance = evidence.get("provenance") or {}
    return {"id": action["id"], "type": action["type"], "payload": action.get("payload"),
            "repo": evidence.get("repo"), "repo_description": github.get("description"),
            "readme_head": (evidence.get("readme") or {}).get("head"), "stars": github.get("stars"),
            "named_in": provenance.get("source"), "comment_author": provenance.get("comment_author"),
            "comment_text": provenance.get("comment_text"), "flags": action.get("flags")}


def build_prompt(summary, context, actions):
    """Pure: one prompt covering every action that needs explaining."""
    return f"""You are writing short explanations for a personal approval list. The reader
approves or skips each action on its own, without seeing the post, so each
explanation must stand alone.

For EACH action below write:
- "what": one plain-English sentence (max 25 words) saying what the tool,
  package, repo, note or rule actually is and does. No marketing, no star
  counts, no dates.
- "why": one sentence (max 25 words) saying why it was suggested -- what the
  post showed or claimed about it. If it was named only in a comment
  (named_in = "comment") or not named at all (named_in = "agent_inferred"),
  say so plainly.
Use only the facts given; if unsure what something is, say what is known.

Post summary: {summary}
Caption: {(context.get('caption') or '')[:800]}
Transcript/OCR: {(context.get('transcript') or '')[:2500]}

Actions:
{json.dumps([_facts(a) for a in actions], ensure_ascii=False, indent=1)}

Respond with ONLY JSON: {{"actions": [{{"id": "a1", "what": "...", "why": "..."}}]}}"""


def apply_explanations(actions, data):
    """Pure: copies what/why onto actions that still need them. Returns count."""
    by_id = {a["id"]: a for a in actions if needs_explanation(a)}
    applied = 0
    for item in (data or {}).get("actions") or []:
        if not isinstance(item, dict):
            continue
        target = by_id.get(item.get("id"))
        what, why = str(item.get("what") or "").strip(), str(item.get("why") or "").strip()
        if target is not None and what and why:
            target["explanation"] = {"what": what[:MAX_CHARS], "why": why[:MAX_CHARS]}
            applied += 1
    return applied


def _parse_items(text):
    """Pure: {"actions": [...]} from an agent reply. When the wrapper is broken
    (seen live: a dropped closing brace makes extract_json return just the
    first item), collect every complete {id, what, why} object instead."""
    try:
        data = agents_lib.extract_json(text)
        if isinstance(data, dict) and isinstance(data.get("actions"), list):
            return data
    except ValueError:
        pass
    decoder, items, i = json.JSONDecoder(), [], (text or "").find("{", 1)
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict) and {"id", "what", "why"} <= obj.keys():
                items.append(obj)
                i = text.find("{", end)
                continue
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return {"actions": items}


def explain_actions(actions, summary, context, run_agent_fn=None, backend=None):
    """Explains a post's pending actions in place. Returns agent calls made (0/1). Never raises."""
    todo = [a for a in actions if needs_explanation(a)]
    if not todo:
        return 0
    try:
        result = (run_agent_fn or agents_lib.run_agent)(build_prompt(summary, context or {}, todo),
                                                        backend=backend, schema=EXPLAIN_SCHEMA)
        if result.get("ok"):
            if not apply_explanations(actions, _parse_items(result["text"])):
                print(f"[explain] WARNING: no usable explanations in: {result['text'][:200]!r}", file=sys.stderr)
        else:
            print(f"[explain] WARNING: agent call failed: {result.get('error')}", file=sys.stderr)
    except Exception as e:
        print(f"[explain] WARNING: not explained: {e}", file=sys.stderr)
    return 1


def explain_pending(staging_root, extracted_root, media_root, run_agent_fn=None, backend=None):
    result = {"proposals": 0, "agent_calls": 0, "changed": []}
    for shortcode in staging_lib.list_pending(staging_root):
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        result["proposals"] += 1
        if not any(needs_explanation(a) for a in proposal["actions"]):
            continue
        context = enrich.load_context(shortcode, extracted_root, media_root)
        result["agent_calls"] += explain_actions(proposal["actions"], proposal.get("summary", ""), context,
                                                 run_agent_fn=run_agent_fn, backend=backend)
        if any(a.get("explanation") for a in proposal["actions"]):
            staging_lib.write_proposal(shortcode, staging_root, proposal)
            result["changed"].append(shortcode)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", action="store_true", required=True)
    ap.add_argument("--staging-root", default=str(REPO_ROOT / "staging"))
    ap.add_argument("--extracted-root", default=str(REPO_ROOT / "extracted"))
    ap.add_argument("--media-root", default=str(REPO_ROOT / "media"))
    ap.add_argument("--backend", default=None, choices=["claude", "codex"])
    args = ap.parse_args()
    print(json.dumps(explain_pending(args.staging_root, args.extracted_root, args.media_root,
                                     backend=args.backend), indent=2))


if __name__ == "__main__":
    main()
