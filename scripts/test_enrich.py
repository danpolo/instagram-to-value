"""Pure-logic tests for Phase A provenance enrichment (scripts/enrich.py).
Run with: python3 -m pytest scripts/test_enrich.py -v
Every fetcher is injected -- nothing here touches GitHub or skills.sh."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import enrich
import staging_lib

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)

# The DbJvV3BpnO6 post, trimmed: ASR mis-hears "impeccable" as "Imperfectible".
TRANSCRIPT = ("there's really only two UI skills you should be using. The first one is "
              "Imperfectible. The second skill, and it also is open source, is the Taste skill.")
CAPTION = "Comment “agent” to get my free Claude code guides"
PROMO = ("That's the rabbit hole that led me to build page-foundry: an open-source skill "
         "(npx skills add taylorbanks/page-foundry) that runs Impeccable as its design engine")
COMMENTS = [{"author": "fuckyeahtaylor", "text": PROMO},
            {"author": "shawnieboy303", "text": "Don't fall for this guy's BS"}]


def skill(name, source=None, aid="a1"):
    payload = {"name": name}
    if source:
        payload["source"] = source
    return {"id": aid, "type": "skill_install", "risk": "exec", "confidence": 0.9,
            "payload": payload, "status": "pending", "decided_at": None, "result": None}


# --- candidate names / repo derivation -------------------------------------------------

def test_repo_of_skill_install_uses_source():
    assert enrich.repo_of(skill("page-foundry", "taylorbanks/page-foundry")) == "taylorbanks/page-foundry"


def test_repo_of_git_repo_parses_github_url():
    action = {"type": "git_repo", "payload": {"name": "t", "url": "https://github.com/Leonxlnx/taste-skill.git"}}
    assert enrich.repo_of(action) == "Leonxlnx/taste-skill"


def test_repo_of_git_repo_non_github_is_none():
    assert enrich.repo_of({"type": "git_repo", "payload": {"name": "x", "url": "https://gitlab.com/a/b"}}) is None


def test_repo_of_package_install_reads_skills_add_command():
    action = {"type": "package_install",
              "payload": {"manager": "npm", "package": "skills", "command": "npx skills add nutlope/hallmark"}}
    assert enrich.repo_of(action) == "nutlope/hallmark"


def test_repo_of_plain_npm_package_is_none():
    action = {"type": "package_install",
              "payload": {"manager": "npm", "package": "@openai/codex", "command": "npm install -g @openai/codex"}}
    assert enrich.repo_of(action) is None


def test_candidate_names_include_repo_name_normalized():
    names = enrich.candidate_names({"type": "git_repo",
                                    "payload": {"name": "taste-skill", "url": "https://github.com/Leonxlnx/taste-skill"}})
    assert "taste" in names


def test_candidate_names_skip_cli_name_for_skills_add():
    action = {"type": "package_install",
              "payload": {"manager": "npm", "package": "skills", "command": "npx skills add nutlope/hallmark"}}
    names = enrich.candidate_names(action)
    assert "hallmark" in names and "skills" not in names


# --- provenance classifier (A1) --------------------------------------------------------

def test_provenance_exact_word_in_transcript_is_video():
    p = enrich.classify_provenance(["taste"], None, TRANSCRIPT, CAPTION, COMMENTS)
    assert p["source"] == "video"
    assert p["comment_author"] is None


def test_provenance_fuzzy_asr_mishearing_is_video_and_records_heard_word():
    p = enrich.classify_provenance(["impeccable"], "pbakaus/impeccable", TRANSCRIPT, CAPTION, COMMENTS)
    assert p["source"] == "video"
    assert p["matched"] == "Imperfectible"


def test_provenance_fuzzy_rejects_unrelated_long_word():
    p = enrich.classify_provenance(["termshark"], None, "a registered trademark of someone", "", [])
    assert p["source"] == "agent_inferred"


def test_provenance_multi_word_name_does_not_fuzzy_match_one_word():
    names = enrich.candidate_names({"type": "package_install", "payload": {
        "manager": "npm", "package": "@deepseek-ai/dsh", "command": "npm install -g @deepseek-ai/dsh"}})
    p = enrich.classify_provenance(names, None, "DeepSeek just shipped a terminal agent", "", [])
    assert p["source"] == "agent_inferred"


def test_provenance_joined_multi_word_name_matches_exactly():
    p = enrich.classify_provenance(["page-foundry"], None, "I built PageFoundry", "", [])
    assert p["source"] == "video" and p["matched"] == "PageFoundry"


def test_provenance_short_names_need_exact_word():
    p = enrich.classify_provenance(["taste"], None, "run the test suite", "", [])
    assert p["source"] == "agent_inferred"


def test_provenance_comment_only_records_author_and_self_link():
    p = enrich.classify_provenance(["page-foundry"], "taylorbanks/page-foundry", TRANSCRIPT, CAPTION, COMMENTS)
    assert p["source"] == "comment"
    assert p["comment_author"] == "fuckyeahtaylor"
    assert p["comment_links_repo"] is True
    assert "page-foundry" in p["comment_text"]


def test_provenance_comment_without_repo_link():
    comments = [{"author": "fan", "text": "you should try page-foundry too"}]
    p = enrich.classify_provenance(["page-foundry"], "taylorbanks/page-foundry", "", "", comments)
    assert p["source"] == "comment"
    assert p["comment_links_repo"] is False


def test_provenance_comment_github_url_counts_as_link():
    comments = [{"author": "x", "text": "see https://github.com/TaylorBanks/page-foundry"}]
    p = enrich.classify_provenance(["page-foundry"], "taylorbanks/page-foundry", "", "", comments)
    assert p["comment_links_repo"] is True


def test_provenance_caption_beats_comment():
    p = enrich.classify_provenance(["hallmark"], None, "", "link: nutlope/hallmark", COMMENTS + [
        {"author": "x", "text": "hallmark rocks"}])
    assert p["source"] == "caption"


def test_provenance_nowhere_is_agent_inferred():
    p = enrich.classify_provenance(["lightrag"], "HKUDS/LightRAG", TRANSCRIPT, CAPTION, COMMENTS)
    assert p["source"] == "agent_inferred"


def test_provenance_tolerates_missing_comment_fields():
    p = enrich.classify_provenance(["x-tool"], None, None, None, [{}, {"text": None}])
    assert p["source"] == "agent_inferred"


# --- fetchers (A2): injectable, never raise --------------------------------------------

def _raise(*_a, **_k):
    raise OSError("network down")


def test_fetch_directory_explicit_source_not_listed():
    search = lambda q: {"skills": [{"skillId": "microsoft-foundry", "name": "microsoft-foundry",
                                    "installs": 589546, "source": "microsoft/azure-skills"}]}
    d = enrich.fetch_directory("page-foundry", "taylorbanks/page-foundry", search_fn=search)
    assert d == {"status": "ok", "listed": False, "source": "taylorbanks/page-foundry",
                 "installs": None, "dominant": False}


def test_fetch_directory_name_only_resolves_dominant_source():
    search = lambda q: {"skills": [
        {"skillId": "impeccable", "name": "impeccable", "installs": 271152, "source": "pbakaus/impeccable"},
        {"skillId": "impeccable", "name": "impeccable", "installs": 900, "source": "fork/impeccable"}]}
    d = enrich.fetch_directory("impeccable", None, search_fn=search)
    assert d["status"] == "ok" and d["listed"] is True and d["dominant"] is True
    assert d["source"] == "pbakaus/impeccable" and d["installs"] == 271152


def test_fetch_directory_explicit_source_listed_reports_installs():
    search = lambda q: {"skills": [
        {"skillId": "taste", "name": "taste", "installs": 466153, "source": "leonxlnx/taste-skill"}]}
    d = enrich.fetch_directory("taste", "Leonxlnx/taste-skill", search_fn=search)
    assert d["listed"] is True and d["installs"] == 466153


def test_fetch_directory_outage_is_unavailable_not_raise():
    d = enrich.fetch_directory("impeccable", None, search_fn=_raise)
    assert d["status"] == "unavailable" and "network down" in d["error"]


def test_fetch_github_ok_shape():
    responses = {
        "https://api.github.com/repos/taylorbanks/page-foundry": {
            "full_name": "taylorbanks/page-foundry", "stargazers_count": 2, "forks_count": 0,
            "created_at": "2026-07-07T10:00:00Z", "pushed_at": "2026-08-14T10:00:00Z",
            "license": {"spdx_id": "MIT"}, "archived": False, "description": "Pretty doesn't pay."},
        "https://api.github.com/repos/taylorbanks/page-foundry/contributors?per_page=100": [
            {"login": "taylorbanks", "contributions": 206}],
    }
    g = enrich.fetch_github("taylorbanks/page-foundry", get_json=lambda url: responses[url])
    assert g == {"status": "ok", "full_name": "taylorbanks/page-foundry", "stars": 2, "forks": 0,
                 "created_at": "2026-07-07", "pushed_at": "2026-08-14", "licence": "MIT",
                 "archived": False, "contributors": 1, "description": "Pretty doesn't pay."}


def test_fetch_github_unclassified_licence_is_not_missing():
    responses = {"https://api.github.com/repos/a/b": {"full_name": "a/b", "license": {"spdx_id": "NOASSERTION"}},
                 "https://api.github.com/repos/a/b/contributors?per_page=100": []}
    g = enrich.fetch_github("a/b", get_json=lambda url: responses[url])
    assert g["licence"] == "other" and g["contributors"] == 0
    assert "no_licence" not in enrich.derive_flags("git_repo", {"source": "video"}, None, g, NOW)


def test_fetch_github_404_is_marked_not_found():
    class NotFound(Exception):
        code = 404
    def get(url):
        raise NotFound("HTTP Error 404")
    g = enrich.fetch_github("a/gone", get_json=get)
    assert g["status"] == "unavailable" and g["not_found"] is True
    action = skill("gone", "a/gone")
    action["evidence"] = {"github": g, "directory": None, "readme": None}
    assert enrich.needs_enrichment(action) is False


def test_fetch_github_contributors_failure_keeps_repo_data():
    def get(url):
        if "contributors" in url:
            raise OSError("403 rate limited")
        return {"full_name": "a/b", "stargazers_count": 5}
    g = enrich.fetch_github("a/b", get_json=get)
    assert g["status"] == "ok" and g["stars"] == 5 and g["contributors"] is None


def test_fetch_github_failure_is_unavailable():
    g = enrich.fetch_github("a/b", get_json=_raise)
    assert g["status"] == "unavailable" and "network down" in g["error"]


def test_fetch_readme_head_skips_markup_and_badges():
    text = ("<div align=\"center\">\n\n# page-foundry\n\n### Pretty doesn't pay. Positioning does.\n\n"
            "[![npm](https://img.shields.io/x)](https://npmjs.com)\n\n```bash\nnpx skills add x/y\n```\n\n"
            "A skill that researches the buyer before it designs the page.\n")
    r = enrich.fetch_readme("taylorbanks/page-foundry", get_text=lambda url: text)
    assert r["status"] == "ok"
    assert r["head"] == ("page-foundry — Pretty doesn't pay. Positioning does. — "
                         "A skill that researches the buyer before it designs the page.")


def test_fetch_readme_failure_is_unavailable():
    assert enrich.fetch_readme("a/b", get_text=_raise)["status"] == "unavailable"


# --- flags (A3) ------------------------------------------------------------------------

PAGE_FOUNDRY_GITHUB = {"status": "ok", "stars": 2, "forks": 0, "created_at": "2026-07-07",
                       "pushed_at": "2026-08-14", "licence": "MIT", "archived": False, "contributors": 1}
COMMENT_PROV = {"source": "comment", "comment_links_repo": True, "comment_author": "fuckyeahtaylor"}


def test_flags_page_foundry_shape():
    flags = enrich.derive_flags("skill_install", COMMENT_PROV, {"status": "ok", "listed": False},
                                PAGE_FOUNDRY_GITHUB, NOW)
    assert flags == ["not_in_video", "self_promoted_in_comment", "low_adoption", "solo_author",
                     "not_in_directory"]


def test_flags_popular_video_repo_is_clean():
    github = {"status": "ok", "stars": 67185, "pushed_at": "2026-09-10", "licence": "Apache-2.0",
              "archived": False, "contributors": 40}
    assert enrich.derive_flags("skill_install", {"source": "video"}, {"status": "ok", "listed": True},
                               github, NOW) == []


def test_flags_stale_archived_no_licence():
    github = {"status": "ok", "stars": 500, "pushed_at": "2025-01-01", "licence": None,
              "archived": True, "contributors": 3}
    assert enrich.derive_flags("git_repo", {"source": "caption"}, None, github, NOW) == [
        "stale", "no_licence", "archived"]


def test_flags_unavailable_fetches_claim_nothing():
    flags = enrich.derive_flags("skill_install", {"source": "video"}, {"status": "unavailable"},
                                {"status": "unavailable"}, NOW)
    assert flags == []


def test_flags_comment_without_link_is_not_self_promoted():
    flags = enrich.derive_flags("git_repo", {"source": "comment", "comment_links_repo": False}, None, None, NOW)
    assert flags == ["not_in_video"]


def test_not_in_directory_only_for_skill_installs():
    assert enrich.derive_flags("git_repo", {"source": "video"}, {"status": "ok", "listed": False}, None, NOW) == []


def test_thresholds_live_in_one_dict():
    assert set(enrich.THRESHOLDS) >= {"low_adoption_stars", "stale_days"}


# --- enrich_actions (A4 core) ----------------------------------------------------------

def stub_fetchers(calls=None):
    calls = calls if calls is not None else []

    def directory(name, source):
        calls.append(("directory", name, source))
        if name == "page-foundry":
            return {"status": "ok", "listed": False, "source": source, "installs": None, "dominant": False}
        return {"status": "ok", "listed": True, "source": "pbakaus/impeccable", "installs": 271152, "dominant": True}

    def github(repo):
        calls.append(("github", repo))
        if repo == "taylorbanks/page-foundry":
            return dict(PAGE_FOUNDRY_GITHUB)
        return {"status": "ok", "stars": 67185, "forks": 4118, "created_at": "2025-11-16",
                "pushed_at": "2026-09-10", "licence": "Apache-2.0", "archived": False, "contributors": 30}

    def readme(repo):
        calls.append(("readme", repo))
        return {"status": "ok", "head": f"{repo} readme"}

    return {"directory": directory, "github": github, "readme": readme}, calls


CONTEXT = {"transcript": TRANSCRIPT, "caption": CAPTION, "comments": COMMENTS}


def test_enrich_actions_page_foundry_and_impeccable():
    actions = [skill("impeccable"), skill("page-foundry", "taylorbanks/page-foundry", aid="a3")]
    fetchers, _ = stub_fetchers()
    enrich.enrich_actions(actions, CONTEXT, fetchers=fetchers, now=NOW)
    assert actions[0]["flags"] == []
    assert actions[0]["evidence"]["repo"] == "pbakaus/impeccable"
    assert actions[0]["evidence"]["provenance"]["source"] == "video"
    assert actions[1]["flags"] == ["not_in_video", "self_promoted_in_comment", "low_adoption",
                                   "solo_author", "not_in_directory"]
    assert actions[1]["evidence"]["checked_at"] == "2026-09-15"


def test_enrich_actions_ignores_uncovered_types():
    action = {"id": "a1", "type": "reference", "payload": {"title": "x"}}
    fetchers, calls = stub_fetchers()
    enrich.enrich_actions([action], CONTEXT, fetchers=fetchers, now=NOW)
    assert "evidence" not in action and calls == []


def test_enrich_actions_skips_complete_evidence():
    actions = [skill("impeccable")]
    fetchers, calls = stub_fetchers()
    enrich.enrich_actions(actions, CONTEXT, fetchers=fetchers, now=NOW)
    before, n = json.dumps(actions), len(calls)
    enrich.enrich_actions(actions, CONTEXT, fetchers=fetchers, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert json.dumps(actions) == before and len(calls) == n


def test_enrich_actions_retries_unavailable_evidence():
    actions = [skill("impeccable")]
    fetchers, calls = stub_fetchers()
    broken = dict(fetchers, github=lambda repo: {"status": "unavailable", "error": "rate limited"})
    enrich.enrich_actions(actions, CONTEXT, fetchers=broken, now=NOW)
    assert actions[0]["evidence"]["github"]["status"] == "unavailable"
    enrich.enrich_actions(actions, CONTEXT, fetchers=fetchers, now=NOW)
    assert actions[0]["evidence"]["github"]["status"] == "ok"


def test_enrich_actions_no_repo_skips_github_and_readme():
    action = {"id": "a1", "type": "package_install", "status": "pending", "payload": {
        "manager": "npm", "package": "@openai/codex", "command": "npm install -g @openai/codex"}}
    fetchers, calls = stub_fetchers()
    enrich.enrich_actions([action], {"transcript": "install codex", "caption": "", "comments": []},
                          fetchers=fetchers, now=NOW)
    assert action["evidence"]["github"] is None and calls == []
    assert action["evidence"]["provenance"]["source"] == "video"


def test_enrich_actions_never_raises_on_raising_fetcher():
    actions = [skill("impeccable")]
    enrich.enrich_actions(actions, CONTEXT, fetchers={"directory": _raise, "github": _raise, "readme": _raise},
                          now=NOW)
    ev = actions[0]["evidence"]
    assert ev["directory"]["status"] == "unavailable"
    assert actions[0]["flags"] == []


def test_enrich_actions_leaves_decided_actions_alone():
    action = skill("impeccable")
    action["status"] = "installed"
    fetchers, calls = stub_fetchers()
    enrich.enrich_actions([action], CONTEXT, fetchers=fetchers, now=NOW)
    assert "evidence" not in action and calls == []


# --- rendering (A5) --------------------------------------------------------------------

def enriched_page_foundry():
    actions = [skill("page-foundry", "taylorbanks/page-foundry", aid="a3")]
    enrich.enrich_actions(actions, CONTEXT, fetchers=stub_fetchers()[0], now=NOW)
    return actions[0]


def test_card_formats_big_star_counts():
    action = skill("impeccable")
    enrich.enrich_actions([action], CONTEXT, fetchers=stub_fetchers()[0], now=NOW)
    card = staging_lib.format_action_card({"summary": "s"}, action, lambda p: p["name"])
    assert "67.2k★" in card and "⚠️" not in card


def test_evidence_block_quotes_comment_and_author():
    block = staging_lib.format_evidence_block(enriched_page_foundry())
    assert "@fuckyeahtaylor" in block
    assert "npx skills add taylorbanks/page-foundry" in block
    assert "https://github.com/taylorbanks/page-foundry" in block
    assert "taylorbanks/page-foundry readme" in block
    assert "not_in_video" in block


def test_evidence_block_video_match_names_heard_word():
    action = skill("impeccable")
    enrich.enrich_actions([action], CONTEXT, fetchers=stub_fetchers()[0], now=NOW)
    assert "Imperfectible" in staging_lib.format_evidence_block(action)


def test_evidence_block_empty_without_evidence():
    assert staging_lib.format_evidence_block(skill("x")) == ""


# --- --pending backfill (A6) -----------------------------------------------------------

def _write_post(tmp_path, shortcode, actions, status="pending"):
    staging_root, extracted_root, media_root = tmp_path / "staging", tmp_path / "extracted", tmp_path / "media"
    staging_lib.write_proposal(shortcode, staging_root, {"shortcode": shortcode, "status": status,
                                                         "created_at": "2026-09-08", "actions": actions})
    extracted_root.mkdir(exist_ok=True)
    (extracted_root / f"{shortcode}.json").write_text(json.dumps({"text": TRANSCRIPT}))
    (media_root / shortcode).mkdir(parents=True, exist_ok=True)
    (media_root / shortcode / f"{shortcode}.info.json").write_text(
        json.dumps({"description": CAPTION, "comments": COMMENTS}))
    return staging_root, extracted_root, media_root


def test_enrich_pending_is_byte_idempotent(tmp_path):
    roots = _write_post(tmp_path, "DbJvV3BpnO6", [skill("impeccable"),
                                                 skill("page-foundry", "taylorbanks/page-foundry", aid="a3")])
    fetchers, calls = stub_fetchers()
    first = enrich.enrich_pending(*roots, fetchers=fetchers, now=NOW)
    path = staging_lib.proposal_path("DbJvV3BpnO6", roots[0])
    snapshot, n = path.read_bytes(), len(calls)
    second = enrich.enrich_pending(*roots, fetchers=fetchers, now=datetime(2026, 9, 16, tzinfo=timezone.utc))
    assert path.read_bytes() == snapshot and len(calls) == n
    assert first["changed"] == ["DbJvV3BpnO6"] and second["changed"] == []
    assert first["with_evidence"] == ["DbJvV3BpnO6"]


def test_enrich_pending_skips_decided_proposals(tmp_path):
    roots = _write_post(tmp_path, "DONE", [skill("impeccable")], status="decided")
    fetchers, calls = stub_fetchers()
    result = enrich.enrich_pending(*roots, fetchers=fetchers, now=NOW)
    assert calls == [] and result["proposals"] == 0


def test_enrich_pending_missing_extracted_still_uses_caption(tmp_path):
    roots = _write_post(tmp_path, "NOEXT", [skill("page-foundry", "taylorbanks/page-foundry")])
    (roots[1] / "NOEXT.json").unlink()
    enrich.enrich_pending(*roots, fetchers=stub_fetchers()[0], now=NOW)
    action = staging_lib.read_proposal("NOEXT", roots[0])["actions"][0]
    assert action["evidence"]["provenance"]["source"] == "comment"
