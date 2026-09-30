"""Tests for the flat per-action /pending view: the cached agent-written
explanation (scripts/explain.py) and the action card (staging_lib).
Run with: python3 -m pytest scripts/test_explain.py -v
The agent is always injected -- no live calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import explain
import staging_lib


def action(aid="a1", type_="skill_install", status="pending", payload=None, **extra):
    a = {"id": aid, "type": type_, "risk": "exec", "confidence": 0.9, "status": status,
         "payload": payload or {"name": "page-foundry", "source": "taylorbanks/page-foundry"},
         "decided_at": None, "result": None}
    a.update(extra)
    return a


PAGE_FOUNDRY_EVIDENCE = {
    "provenance": {"source": "comment", "comment_author": "fuckyeahtaylor", "comment_links_repo": True,
                   "comment_text": "npx skills add taylorbanks/page-foundry", "matched": "page-foundry"},
    "repo": "taylorbanks/page-foundry",
    "github": {"status": "ok", "stars": 2, "contributors": 1, "licence": "MIT",
               "created_at": "2026-07-07", "pushed_at": "2026-08-14",
               "description": "Pretty doesn't pay. Positioning does."},
    "directory": {"status": "ok", "listed": True, "installs": 6},
    "readme": {"status": "ok", "head": "page-foundry — Pretty doesn't pay."},
    "checked_at": "2026-09-14",
}
PAGE_FOUNDRY_FLAGS = ["not_in_video", "self_promoted_in_comment", "low_adoption", "solo_author"]


def stage(root, shortcode, actions, summary="post summary", created="2026-01-01"):
    staging_lib.write_proposal(shortcode, root, {"shortcode": shortcode, "summary": summary, "status": "pending",
                                                 "created_at": created, "actions": actions})


# --- flat list -------------------------------------------------------------------------

def test_pending_action_items_flattens_across_posts_keeping_origin(tmp_path):
    stage(tmp_path, "OLD", [action("a1"), action("a2", status="installed"), action("a3")], created="2026-01-01")
    stage(tmp_path, "NEW", [action("a1")], created="2026-02-01")
    items = staging_lib.pending_action_items(tmp_path)
    assert [(sc, a["id"]) for sc, _, a in items] == [("OLD", "a1"), ("OLD", "a3"), ("NEW", "a1")]
    assert items[0][1]["shortcode"] == "OLD"


# --- warnings --------------------------------------------------------------------------

def test_plain_warning_page_foundry():
    assert staging_lib.plain_warning(PAGE_FOUNDRY_FLAGS, PAGE_FOUNDRY_EVIDENCE) == (
        "⚠️ Only recommended in a comment that links its own repo; very few stars; one-person project")


def test_plain_warning_comment_without_self_link():
    evidence = {"provenance": {"source": "comment", "comment_links_repo": False}}
    assert staging_lib.plain_warning(["not_in_video"], evidence) == (
        "⚠️ Only mentioned in a comment, not the post itself")


def test_plain_warning_agent_inferred():
    evidence = {"provenance": {"source": "agent_inferred"}}
    assert staging_lib.plain_warning(["not_in_video", "stale"], evidence) == (
        "⚠️ Not named in the post (the agent inferred it); no updates in over a year")


def test_plain_warning_none():
    assert staging_lib.plain_warning([], {}) == ""


# --- card ------------------------------------------------------------------------------

def describe(payload):
    return f"Install skill `{payload['name']}`"


def test_action_card_with_explanation_evidence_and_warning():
    a = action(evidence=PAGE_FOUNDRY_EVIDENCE, flags=PAGE_FOUNDRY_FLAGS,
               explanation={"what": "A Claude Code skill that works out who a landing page is for.",
                            "why": "Suggested in a comment by the repo's own author, not in the video."})
    card = staging_lib.format_action_card({"shortcode": "DbJvV3BpnO6", "summary": "s"}, a, describe)
    assert card.splitlines() == [
        "🔴 Install skill `page-foundry` · 2★",
        "What: A Claude Code skill that works out who a landing page is for.",
        "Why: Suggested in a comment by the repo's own author, not in the video.",
        "⚠️ Only recommended in a comment that links its own repo; very few stars; one-person project",
    ]


def test_action_card_leaves_out_dates_licence_contributors_and_shortcode():
    a = action(evidence=PAGE_FOUNDRY_EVIDENCE, flags=PAGE_FOUNDRY_FLAGS)
    card = staging_lib.format_action_card({"shortcode": "DbJvV3BpnO6", "summary": "s"}, a, describe)
    for noise in ("2026-07-07", "2026-08-14", "MIT", "contributor", "DbJvV3BpnO6", "skills.sh"):
        assert noise not in card, noise


def test_action_card_falls_back_to_repo_description_and_post_summary():
    a = action(evidence=PAGE_FOUNDRY_EVIDENCE)
    card = staging_lib.format_action_card({"shortcode": "X", "summary": "Two UI skills."}, a, describe)
    assert "What: Pretty doesn't pay. Positioning does." in card
    assert "Why: Two UI skills." in card


def test_action_card_without_any_evidence():
    a = action(type_="reference", payload={"title": "T"}, risk="inert")
    a["risk"] = "inert"
    card = staging_lib.format_action_card({"shortcode": "X", "summary": "A fact."}, a, lambda p: "Save T")
    assert card == "🟢 Save T\nWhy: A fact."


# --- explain (cached agent explanation) ------------------------------------------------

def test_needs_explanation_only_pending_unexplained():
    assert explain.needs_explanation(action()) is True
    assert explain.needs_explanation(action(status="installed")) is False
    assert explain.needs_explanation(action(explanation={"what": "x", "why": "y"})) is False


def test_build_prompt_carries_facts_not_noise():
    a = action(evidence=PAGE_FOUNDRY_EVIDENCE, flags=PAGE_FOUNDRY_FLAGS)
    prompt = explain.build_prompt("Install UI skills", {"transcript": "two UI skills", "caption": "cap",
                                                        "comments": []}, [a])
    for fact in ("a1", "page-foundry", "Pretty doesn't pay", "two UI skills", "comment", "fuckyeahtaylor",
                 "Install UI skills"):
        assert fact in prompt, fact


def test_apply_explanations_matches_ids_and_ignores_junk():
    actions = [action("a1"), action("a2"), action("a3", status="skipped")]
    data = {"actions": [{"id": "a1", "what": " W1 ", "why": "Y1"},
                        {"id": "a2", "what": "", "why": "Y2"},
                        {"id": "a3", "what": "W3", "why": "Y3"},
                        {"id": "zz", "what": "W", "why": "Y"}, "junk"]}
    assert explain.apply_explanations(actions, data) == 1
    assert actions[0]["explanation"] == {"what": "W1", "why": "Y1"}
    assert "explanation" not in actions[1] and "explanation" not in actions[2]


def _agent(payload, calls):
    def run(prompt, backend=None, schema=None, **kw):
        calls.append(prompt)
        return {"ok": True, "text": json.dumps(payload), "model": None, "error": None}
    return run


def test_explain_actions_one_call_per_post():
    actions = [action("a1"), action("a2")]
    calls = []
    run = _agent({"actions": [{"id": "a1", "what": "W1", "why": "Y1"}, {"id": "a2", "what": "W2", "why": "Y2"}]},
                 calls)
    n = explain.explain_actions(actions, "s", {"transcript": "", "caption": "", "comments": []}, run_agent_fn=run)
    assert n == 1 and len(calls) == 1
    assert actions[1]["explanation"]["why"] == "Y2"


def test_explain_actions_survives_missing_closing_brace():
    # Seen live 2026-09-15 on 4 of 28 posts: the model dropped the outer '}',
    # so extract_json returned only the first action item and nothing applied.
    actions = [action("a1"), action("a2")]
    text = '{"actions": [{"id": "a1", "what": "W1", "why": "Y1"}, {"id": "a2", "what": "W2", "why": "Y2"}]'
    run = lambda *a, **kw: {"ok": True, "text": text, "error": None}
    explain.explain_actions(actions, "s", {}, run_agent_fn=run)
    assert actions[0]["explanation"] == {"what": "W1", "why": "Y1"}
    assert actions[1]["explanation"] == {"what": "W2", "why": "Y2"}


def test_explain_actions_no_call_when_nothing_needs_it():
    calls = []
    n = explain.explain_actions([action(status="installed")], "s", {}, run_agent_fn=_agent({}, calls))
    assert n == 0 and calls == []


def test_explain_actions_never_raises():
    def boom(*a, **kw):
        raise RuntimeError("cli missing")
    actions = [action()]
    assert explain.explain_actions(actions, "s", {}, run_agent_fn=boom) == 1
    assert "explanation" not in actions[0]


def test_explain_actions_failed_call_leaves_actions_untouched():
    actions = [action()]
    run = lambda *a, **kw: {"ok": False, "text": "", "error": "quota"}
    explain.explain_actions(actions, "s", {}, run_agent_fn=run)
    assert "explanation" not in actions[0]


def test_explain_pending_idempotent(tmp_path):
    staging, extracted, media = tmp_path / "s", tmp_path / "e", tmp_path / "m"
    stage(staging, "AAA", [action("a1")])
    calls = []
    run = _agent({"actions": [{"id": "a1", "what": "W", "why": "Y"}]}, calls)
    first = explain.explain_pending(staging, extracted, media, run_agent_fn=run)
    path = staging_lib.proposal_path("AAA", staging)
    snapshot = path.read_bytes()
    second = explain.explain_pending(staging, extracted, media, run_agent_fn=run)
    assert first == {"proposals": 1, "agent_calls": 1, "changed": ["AAA"]}
    assert second == {"proposals": 1, "agent_calls": 0, "changed": []}
    assert path.read_bytes() == snapshot and len(calls) == 1


# --- duplicate merging -----------------------------------------------------------------

def git(aid, url, status="pending"):
    return action(aid, type_="git_repo", status=status, payload={"name": "x", "url": url},
                  evidence={"repo": url.split("github.com/")[1].removesuffix(".git")})


def test_action_key_same_repo_across_types_and_case():
    a = git("a1", "https://github.com/Leonxlnx/taste-skill.git")
    b = action("a2", payload={"name": "taste"}, evidence={"repo": "leonxlnx/taste-skill"})
    assert staging_lib.action_key(a) == staging_lib.action_key(b) == "repo:leonxlnx/taste-skill"


def test_action_key_package_and_non_install():
    pkg = action(type_="package_install", payload={"manager": "npm", "package": "@openai/codex", "command": "x"})
    assert staging_lib.action_key(pkg) == "pkg:npm:@openai/codex"
    assert staging_lib.action_key(action(type_="reference", payload={"title": "T"})) is None


def test_pending_action_groups_merges_and_prefers_skill_install(tmp_path):
    stage(tmp_path, "P1", [git("a1", "https://github.com/Leonxlnx/taste-skill"),
                           action("a2", type_="reference", payload={"title": "T"})], created="2026-01-01")
    stage(tmp_path, "P2", [action("a1", payload={"name": "taste"}, evidence={"repo": "leonxlnx/taste-skill"}),
                           action("a2", type_="reference", payload={"title": "T"})], created="2026-02-01")
    groups = staging_lib.pending_action_groups(tmp_path)
    assert len(groups) == 3                      # taste merged; the two references stay separate
    taste = groups[0]
    assert [(sc, a["id"]) for sc, _, a in taste["items"]] == [("P2", "a1"), ("P1", "a1")]
    assert taste["items"][0][2]["type"] == "skill_install"
    assert taste["installed_elsewhere"] is False


def test_pending_action_groups_detects_installed_elsewhere(tmp_path):
    staging_lib.write_proposal("DONE", tmp_path, {"shortcode": "DONE", "status": "decided", "actions": [
        git("a1", "https://github.com/pbakaus/impeccable", status="installed")]})
    stage(tmp_path, "P1", [action("a1", payload={"name": "impeccable"}, evidence={"repo": "pbakaus/impeccable"})])
    assert staging_lib.pending_action_groups(tmp_path)[0]["installed_elsewhere"] is True


def test_card_shows_post_count_and_installed_warning():
    a = action(evidence={"github": {"status": "ok", "stars": 68000}}, explanation={"what": "W", "why": "Y"})
    card = staging_lib.format_action_card({"summary": "s"}, a, describe, posts=3, installed_elsewhere=True)
    assert card.splitlines()[0] == "🔴 Install skill `page-foundry` · 68k★ · suggested in 3 posts"
    assert card.splitlines()[-1] == "⚠️ Already installed from another post"


def test_card_label_for_per_post_message():
    card = staging_lib.format_action_card({"summary": "s"}, action(), describe, label="a1")
    assert card.splitlines()[0] == "🔴 [a1] Install skill `page-foundry`"


def test_action_key_reads_repo_from_unenriched_url_and_skills_add():
    clone = action(type_="git_repo", status="installed", payload={"name": "i", "url": "https://github.com/pbakaus/impeccable.git"})
    npx = action(type_="package_install", status="installed",
                 payload={"manager": "npm", "package": "skills", "command": "npx skills add pbakaus/impeccable"})
    skill = action(payload={"name": "impeccable"}, evidence={"repo": "pbakaus/impeccable"})
    assert staging_lib.action_key(clone) == staging_lib.action_key(npx) == staging_lib.action_key(skill)
