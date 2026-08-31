#!/usr/bin/env python3
"""Phase 4a stage orchestrator (design spec Component 4). Reads
extracted/<shortcode>.json + media/<shortcode>/, runs tiers 1-2 of the
resolver, one agent call (triage+draft), escalates to tier 4 (web search)
then tier 3 (frame OCR) plus a redraft call only if the draft names an
unnamed tool, validates the result against the action registry, and writes
staging/<shortcode>/proposal.json.

Usage:
    python3 scripts/interpret.py <shortcode> [--media-root DIR]
        [--extracted-root DIR] [--staging-root DIR] [--logs-root DIR]
        [--backend claude|codex] [--origin telegram|backfill]
    python3 scripts/interpret.py --backfill-sweep [same root flags]

Single-shortcode mode prints proposal.json to stdout (matches fetch.py/
extract.py's convention) and exits nonzero on a failed agent call or an
invalid action list -- design spec's Error handling: "a failed call never
yields a fabricated proposal". worker.py treats that like any other stage
failure (job moves to jobs/failed/).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import agents_lib
import registry
import resolve_tools
import staging_lib

registry.load_all_handlers()

DEFAULT_MEDIA_ROOT = REPO_ROOT / "media"
DEFAULT_EXTRACTED_ROOT = REPO_ROOT / "extracted"
DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"
DEFAULT_LOGS_ROOT = REPO_ROOT / "logs"

ACTION_LIST_SCHEMA = {
    "type": "object",
    "required": ["summary", "unnamed_tool", "actions"],
    "properties": {
        "summary": {"type": "string"},
        "unnamed_tool": {
            "type": "object", "required": ["present", "description"],
            "properties": {"present": {"type": "boolean"}, "description": {"type": "string"}},
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object", "required": ["type", "confidence", "payload"],
                "properties": {"type": {"type": "string"}, "confidence": {"type": "number"},
                                 "payload": {"type": "object"}},
            },
        },
    },
}
SEARCH_SCHEMA = {
    "type": "object",
    "required": ["tool_name", "url", "confidence", "sources"],
    "properties": {
        "tool_name": {"type": ["string", "null"]}, "url": {"type": ["string", "null"]},
        "confidence": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}},
    },
}


def load_post_context(shortcode, extracted_root, media_root):
    """File IO only: gathers everything the triage prompt needs. Raises
    FileNotFoundError if extracted/<shortcode>.json is missing -- interpret.py
    is only ever run after extract.py, same precondition fetch.py/extract.py
    already enforce on each other."""
    extracted_path = Path(extracted_root) / f"{shortcode}.json"
    if not extracted_path.exists():
        raise FileNotFoundError(f"no extracted data for {shortcode} at {extracted_path}")
    extracted = json.loads(extracted_path.read_text())

    media_dir = Path(media_root) / shortcode
    description, comments, channel, duration_s = "", [], None, 0

    info_json = media_dir / f"{shortcode}.info.json"
    if info_json.exists():
        info = json.loads(info_json.read_text())
        description = info.get("description", "")
        comments = info.get("comments") or []
        channel = info.get("channel")
        duration_s = info.get("duration") or 0
    else:
        desc_path = media_dir / f"{shortcode}.description"
        if desc_path.exists():
            description = desc_path.read_text()
        sidecars = sorted(media_dir.glob(f"{shortcode}_*.json"))
        if sidecars:
            sidecar = json.loads(sidecars[0].read_text())
            description = description or sidecar.get("description", "")
            channel = sidecar.get("username")

    return {"extracted": extracted, "description": description, "comments": comments,
            "channel": channel, "duration_s": duration_s}


def build_triage_prompt(shortcode, context, pure_resolver, catalogue):
    disagreement_spans = (context["extracted"].get("reconciliation") or {}).get("disagreement_spans", [])
    return f"""You are classifying one Instagram post for a personal knowledge/tool pipeline.
Read the transcript/OCR text, caption and comments below. Identify any concrete,
actionable outcomes -- a tool the creator demos, a fact worth keeping, an event,
a procedure worth turning into a Claude Code skill or rule -- and propose one
action per outcome from the registry below. A post can produce zero, one, or
several actions.

If the post demos an installable tool (a package, binary, or repo the viewer
would want ON THEIR MACHINE), you MUST propose the concrete install action
(package_install / git_repo / binary_release / etc.) for it -- a `reference`
noting that the tool exists is not a substitute and must not replace it. Add a
`reference` alongside the install action only for extra context that doesn't
fit the install payload (e.g. a tip from the comments), never instead of it.
If the tool needs a shell init line to actually work day-to-day (e.g. `zoxide
init`), propose a SEPARATE `shell_snippet` action for that line in addition to
the install action -- do not fold it into a reference either.

The "prefer the weakest artifact" principle applies only among the knowledge/
agent-config types themselves: default to `reference` over `rule`/`skill`
UNLESS the post is a genuine repeatable procedure or always-on constraint (in
which case propose the `skill`/`rule` directly, don't downgrade it to a
reference). It never means skipping a real install/calendar/knowledge action
in favor of a weaker one that captures less of the value. Concretely: a post
of concrete practices for how an AI coding agent (like you) should be used or
configured -- e.g. "always do X to cut token cost", "run Y before Z" -- is a
`skill`/`rule` candidate, not merely a `reference`, even when it lists several
tactics; a `reference` is for a fact/pointer with no actionable "always do
this" content of its own.

If a tool is named directly in the content (even without a link), use that
name -- do not treat it as "unnamed". Only set unnamed_tool.present=true if the
creator demos something and deliberately withholds its name (e.g. "comment X
and I'll DM you the link").

The transcript comes from ASR and may mis-hear proper nouns -- these spans were
flagged as disagreeing between two independent transcription engines, treat
tokens inside them with extra skepticism: {json.dumps(disagreement_spans[:10], ensure_ascii=False)}

Caption:
{context['description']}

Transcript/OCR text:
{context['extracted'].get('text', '')}

Top comments:
{json.dumps([c.get('text', '') for c in context['comments'][:15]], ensure_ascii=False)}

Tier 1-2 URL search already ran (pure Python, over comments/caption only) and found:
{json.dumps(pure_resolver, ensure_ascii=False)}

Available action types:
{catalogue}

Respond with ONLY a JSON object, no prose before or after, matching this shape:
{{"summary": "one sentence", "unnamed_tool": {{"present": false, "description": ""}},
  "actions": [{{"type": "package_install", "confidence": 0.9, "payload": {{...}}}}]}}
Shortcode: {shortcode}
"""


def build_search_prompt(shortcode, context, unnamed_tool_description):
    return f"""Web search task. A creator's Instagram post describes a tool without
naming it: "{unnamed_tool_description}". Context from the post:

Caption: {context['description']}
Transcript excerpt: {context['extracted'].get('text', '')[:1500]}

Search the web and identify the specific tool/product being described. Respond
with ONLY a JSON object: {{"tool_name": "...", "url": "...", "confidence":
"high"|"medium"|"low", "sources": ["..."]}}. If you cannot identify it with at
least medium confidence, set tool_name and url to null."""


def build_redraft_prompt(triage_prompt, new_evidence):
    return triage_prompt + f"""

NEW EVIDENCE resolved since your first answer, not available before -- use it
to correct/complete the action list if it changes anything:
{json.dumps(new_evidence, ensure_ascii=False)}

Respond again with ONLY the same JSON shape as before, incorporating this evidence."""


def call_agent_for_json(prompt, schema, backend, allow_search=False, max_retries=1):
    """One agent call + up to max_retries retries with a "JSON only" nudge on
    an unparseable/failed response. Returns (data, raw_result); data is None
    on final failure -- design spec's error handling: one retry, then fail,
    never a fabricated proposal."""
    attempt_prompt = prompt
    last_result = None
    for _ in range(max_retries + 1):
        result = agents_lib.run_agent(attempt_prompt, backend=backend, allow_search=allow_search, schema=schema)
        last_result = result
        if not result["ok"]:
            attempt_prompt = prompt + "\n\nYour previous attempt failed to run. Return ONLY the JSON object."
            continue
        try:
            return agents_lib.extract_json(result["text"]), result
        except ValueError:
            attempt_prompt = prompt + "\n\nYour previous response was not valid JSON. Return ONLY the JSON object, nothing else."
    return None, last_result


def call_agent_for_actions(prompt, backend, allow_search=False, max_retries=1):
    """Like call_agent_for_json, but additionally validates the returned
    action list against the registry and retries with a validation-error
    nudge on a schema violation -- design spec's error handling: "Schema
    violation in the returned action list -> same path as an unparseable
    response." (Found live during Task 12 verification: the agent proposed
    package_install with manager="brew", which isn't installed on this
    machine -- the SCHEMA enum correctly rejected it, but nothing retried the
    call before this fix, so a single enum slip failed the whole post.)

    Deliberately does NOT pass ACTION_LIST_SCHEMA as codex's --output-schema
    (also found live, Task 12's backend-parity check): an action's `payload`
    is a genuinely different shape per action type, so it can't be a static
    JSON Schema object -- and OpenAI's structured-output enforcement (which
    codex's --output-schema uses) requires every object node to declare
    "additionalProperties": false, rejecting an open-ended payload outright
    ("'additionalProperties' is required to be supplied and to be false").
    Python-side registry.validate_actions() is the real gate for both
    backends here, same as the design spec's "Claude can only be asked"
    already accepted for claude -- codex just doesn't get the extra belt on
    this particular call. (SEARCH_SCHEMA has no such field and can still use
    --output-schema; see build_search_prompt's caller.)
    Returns (draft, agent_result); draft is None on final failure."""
    attempt_prompt = prompt
    last_result = None
    for _ in range(max_retries + 1):
        result = agents_lib.run_agent(attempt_prompt, backend=backend, allow_search=allow_search)
        last_result = result
        if not result["ok"]:
            attempt_prompt = prompt + "\n\nYour previous attempt failed to run. Return ONLY the JSON object."
            continue
        try:
            draft = agents_lib.extract_json(result["text"])
        except ValueError:
            attempt_prompt = prompt + "\n\nYour previous response was not valid JSON. Return ONLY the JSON object, nothing else."
            continue
        ok, errors = registry.validate_actions(draft.get("actions", []))
        if ok:
            return draft, result
        attempt_prompt = (prompt + f"\n\nYour previous action list was invalid: {'; '.join(errors)}. "
                           "Only use the exact action types, and the exact payload fields/enum values, from "
                           "the 'Available action types' list above (e.g. package_install's manager must be "
                           "one of npm/uv/pip/cargo/apt/docker -- brew/pipx/go are NOT installed on this "
                           "machine). Return ONLY the corrected JSON object.")
    return None, last_result


def _write_agent_error(shortcode, staging_root, agent_result):
    d = staging_lib.staging_dir(shortcode, staging_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-error.txt").write_text(json.dumps(agent_result, indent=2, ensure_ascii=False))


def interpret_post(shortcode, media_root, extracted_root, staging_root, logs_root, backend=None, origin="telegram"):
    """The whole Component 4 pipeline for one post. Returns the written
    proposal dict, or raises RuntimeError on an unrecoverable agent/
    validation failure (caller -- worker.py -- treats that like any other
    stage failure)."""
    backend = backend or agents_lib.get_backend()
    context = load_post_context(shortcode, extracted_root, media_root)
    pure_resolver = resolve_tools.resolve_pure_tiers(context["description"], context["comments"], context["channel"])
    catalogue = registry.generate_prompt_catalogue()

    triage_prompt = build_triage_prompt(shortcode, context, pure_resolver, catalogue)
    draft, agent_result = call_agent_for_actions(triage_prompt, backend)
    agent_calls = 1
    if draft is None:
        _write_agent_error(shortcode, staging_root, agent_result)
        raise RuntimeError(f"interpret: agent call #1 failed for {shortcode}: {agent_result.get('error')}")

    resolver = {"status": pure_resolver["status"], "tool_name": None, "url": None,
                "tier": pure_resolver["tier"], "evidence": pure_resolver["candidates"]}
    unnamed_tool = draft.get("unnamed_tool") or {"present": False, "description": ""}

    if unnamed_tool.get("present") and pure_resolver["status"] == "not_found":
        search_prompt = build_search_prompt(shortcode, context, unnamed_tool.get("description", ""))
        search_data, _ = call_agent_for_json(search_prompt, SEARCH_SCHEMA, backend, allow_search=True)
        agent_calls += 1
        if search_data is not None:
            web_result = resolve_tools.resolve_via_web_search(json.dumps(search_data), agents_lib.extract_json)
            if web_result["status"] in ("resolved", "uncertain"):
                resolver.update({"status": web_result["status"], "tool_name": web_result["tool_name"],
                                  "url": web_result["url"], "tier": 4, "evidence": web_result["sources"]})
                redraft, _ = call_agent_for_actions(build_redraft_prompt(triage_prompt, web_result), backend)
                agent_calls += 1
                if redraft is not None:
                    draft = redraft
            elif context["extracted"].get("media_type") == "video":
                frame_result = resolve_tools.resolve_via_frame_ocr(shortcode, media_root, context["duration_s"])
                if frame_result["texts"]:
                    redraft, _ = call_agent_for_actions(
                        build_redraft_prompt(triage_prompt, {"on_screen_text": frame_result["texts"]}), backend)
                    agent_calls += 1
                    if redraft is not None:
                        draft = redraft

    raw_actions = draft.get("actions", [])
    ok, errors = registry.validate_actions(raw_actions)
    if not ok:
        _write_agent_error(shortcode, staging_root, {"error": "; ".join(errors), "raw": json.dumps(draft)})
        raise RuntimeError(f"interpret: invalid action list for {shortcode}: {errors}")

    excerpt = context["extracted"].get("text", "")[:300]
    actions = []
    for i, raw in enumerate(raw_actions, start=1):
        handler = registry.get_handler(raw["type"])
        action_id = f"a{i}"
        status = "pending"
        if staging_lib.is_auto_discard_eligible(origin, raw["type"], raw["confidence"]):
            handler.install(raw["payload"], None, {"shortcode": shortcode, "transcript_excerpt": excerpt})
            status = "auto_discarded"
        elif raw["type"] == "unsupported":
            handler.install(raw["payload"], None, {"shortcode": shortcode})
        actions.append({"id": action_id, "type": raw["type"], "risk": handler.RISK,
                         "confidence": raw["confidence"], "payload": raw["payload"],
                         "status": status, "decided_at": None, "result": None})

    proposal = {
        "shortcode": shortcode, "origin": origin, "backend": backend,
        "model": agent_result.get("model"), "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": draft.get("summary", ""), "resolver": resolver,
        "status": "pending" if any(a["status"] == "pending" for a in actions) else "decided",
        "message_id": None, "actions": actions, "agent_calls": agent_calls,
    }
    staging_lib.write_proposal(shortcode, staging_root, proposal)
    return proposal


def backfill_sweep(media_root, extracted_root, staging_root, logs_root, backend=None,
                     notify_fn=None, interpret_post_fn=interpret_post):
    """Runs interpret_post over every extracted/<shortcode>.json missing
    staging/<shortcode>/ -- catches up the 49-job drain, which wrote
    extracted/ before interpret.py existed. Safe to re-run (skips anything
    already staged). Sends ONE digest message at the end, not one per post
    (design spec: "staged silently, then one digest per drain")."""
    extracted_root = Path(extracted_root)
    results = {"interpreted": [], "failed": []}
    for path in sorted(extracted_root.glob("*.json")):
        shortcode = path.stem
        if staging_lib.has_staging(shortcode, staging_root):
            continue
        try:
            proposal = interpret_post_fn(shortcode, media_root, extracted_root, staging_root, logs_root,
                                           backend=backend, origin="backfill")
            results["interpreted"].append(proposal)
        except (RuntimeError, FileNotFoundError) as e:
            print(f"[interpret] WARNING: backfill sweep skipped {shortcode}: {e}", file=sys.stderr)
            results["failed"].append(shortcode)

    if results["interpreted"] and notify_fn is not None:
        digest = staging_lib.format_digest(results["interpreted"])
        unsupported_counts = staging_lib.aggregate_unsupported(Path(logs_root) / "unsupported_actions.jsonl")
        unsupported_summary = staging_lib.format_unsupported_summary(unsupported_counts)
        message = digest + (f"\n{unsupported_summary}" if unsupported_summary else "")
        try:
            notify_fn(message)
        except Exception as e:
            print(f"[interpret] WARNING: digest notify failed: {e}", file=sys.stderr)

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("shortcode", nargs="?")
    ap.add_argument("--media-root", default=str(DEFAULT_MEDIA_ROOT))
    ap.add_argument("--extracted-root", default=str(DEFAULT_EXTRACTED_ROOT))
    ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    ap.add_argument("--logs-root", default=str(DEFAULT_LOGS_ROOT))
    ap.add_argument("--backend", default=None, choices=["claude", "codex"])
    ap.add_argument("--origin", default="telegram", choices=["telegram", "backfill"])
    ap.add_argument("--backfill-sweep", action="store_true")
    args = ap.parse_args()

    if args.backfill_sweep:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from telegram_notify import send as notify_send
        results = backfill_sweep(args.media_root, args.extracted_root, args.staging_root, args.logs_root,
                                   backend=args.backend, notify_fn=notify_send)
        print(json.dumps({"interpreted": [p["shortcode"] for p in results["interpreted"]],
                            "failed": results["failed"]}, indent=2))
        return

    if not args.shortcode:
        raise SystemExit("[interpret] FATAL: shortcode required unless --backfill-sweep")

    try:
        proposal = interpret_post(args.shortcode, args.media_root, args.extracted_root, args.staging_root,
                                    args.logs_root, backend=args.backend, origin=args.origin)
    except (RuntimeError, FileNotFoundError) as e:
        raise SystemExit(f"[interpret] FATAL: {e}")
    print(json.dumps(proposal, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
