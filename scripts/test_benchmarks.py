"""Tests for the benchmark gate (Phase B): the registry state machine
(benchmarks_lib), quota routing (quota_router), the draft/run/judge
orchestration (benchmark), the agy/effort/workdir agent backends, and the
Remove/Keep verdict in telegram_bot.
Run with: python3 -m pytest scripts/test_benchmarks.py -v
Every agent, quota fetch and Telegram send is injected -- no live calls."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import agents_lib
import benchmark
import benchmarks_lib
import quota_router
import staging_lib

NOW = "2026-09-15T12:00:00+00:00"


def git_action(aid="a1", name="impeccable", url="https://github.com/pbakaus/impeccable", status="installed"):
    return {"id": aid, "type": "git_repo", "risk": "exec", "confidence": 0.9, "status": status,
            "payload": {"name": name, "url": url}, "decided_at": "2026-09-12T10:00:00+00:00",
            "result": {"ok": True, "path": f"/home/dan/tools/{name}"}}


def ok_result(path):
    return {"ok": True, "path": str(path), "command": None, "exit_code": 0, "output": None, "error": None}


# --- benchmarks_lib: state machine ------------------------------------------------------

def test_transition_follows_the_approved_state_machine():
    record = {"state": "not_benchmarked", "history": []}
    for state in ("drafted", "running", "failing", "verdict_sent", "kept_despite_failure"):
        record = benchmarks_lib.transition(record, state, NOW)
    assert record["state"] == "kept_despite_failure"
    assert [h["state"] for h in record["history"]] == ["drafted", "running", "failing", "verdict_sent",
                                                        "kept_despite_failure"]


def test_transition_rejects_skipping_a_step():
    with pytest.raises(benchmarks_lib.InvalidTransition):
        benchmarks_lib.transition({"state": "not_benchmarked"}, "failing", NOW)
    with pytest.raises(benchmarks_lib.InvalidTransition):
        benchmarks_lib.transition({"state": "passing"}, "uninstalled", NOW)


def test_transition_is_pure_and_records_extra_fields():
    record = {"state": "running", "history": []}
    new = benchmarks_lib.transition(record, "passing", NOW, last_run="runs/x")
    assert record["state"] == "running" and new["last_run"] == "runs/x"


def test_a_failed_run_can_fall_back_to_drafted_for_a_retry():
    assert benchmarks_lib.transition({"state": "running"}, "drafted", NOW)["state"] == "drafted"


# --- benchmarks_lib: registration --------------------------------------------------------

def test_register_install_creates_one_not_benchmarked_record():
    registry = benchmarks_lib.empty()
    record = benchmarks_lib.register_install(registry, "SC1", git_action(), ok_result("/t/impeccable"), NOW)
    assert record["state"] == "not_benchmarked"
    assert record["name"] == "impeccable" and record["key"] == "repo:pbakaus/impeccable"
    assert record["shortcode"] == "SC1" and record["installed_at"] == NOW
    assert record["installs"] == [{"shortcode": "SC1", "action_id": "a1", "type": "git_repo",
                                   "path": "/t/impeccable", "command": None}]
    assert list(registry["records"]) == [record["id"]]


def test_the_same_repo_from_another_post_adds_an_install_not_a_record():
    registry = benchmarks_lib.empty()
    benchmarks_lib.register_install(registry, "SC1", git_action(), ok_result("/t/impeccable"), NOW)
    npx = {"id": "a1", "type": "package_install", "status": "installed",
           "payload": {"manager": "npm", "package": "pbakaus/impeccable",
                       "command": "npx skills add pbakaus/impeccable"}}
    record = benchmarks_lib.register_install(registry, "SC2", npx, {"ok": True, "path": None}, NOW)
    assert len(registry["records"]) == 1
    assert [i["shortcode"] for i in record["installs"]] == ["SC1", "SC2"]
    assert record["state"] == "not_benchmarked"


def test_rules_and_references_are_not_benchmarked():
    registry = benchmarks_lib.empty()
    rule = {"id": "a1", "type": "rule", "status": "installed", "payload": {"topic": "x", "content": "y"}}
    assert benchmarks_lib.register_install(registry, "SC1", rule, ok_result("/r.md"), NOW) is None
    assert registry["records"] == {}


def test_record_ids_fit_telegram_callback_data():
    rid = benchmarks_lib.record_id("repo:" + "a" * 200)
    assert len(f"bench_keep:{rid}".encode()) <= 64


def test_backfill_registers_installed_artefacts_only_once(tmp_path):
    staging = tmp_path / "staging"
    staging_lib.write_proposal("SC1", staging, {"shortcode": "SC1", "status": "decided", "actions": [
        git_action("a1"), git_action("a2", name="notes", status="skipped"),
        {"id": "a3", "type": "rule", "status": "installed", "payload": {}, "result": {"ok": True}}]})
    staging_lib.write_proposal("SC2", staging, {"shortcode": "SC2", "status": "decided", "actions": [
        git_action("a1", name="impeccable-2"), git_action("a2", name="CLI-Anything",
                                                          url="https://github.com/HKUDS/CLI-Anything")]})
    registry = benchmarks_lib.empty()
    assert benchmarks_lib.backfill(registry, staging) == 2
    assert benchmarks_lib.backfill(registry, staging) == 0
    names = sorted(r["name"] for r in registry["records"].values())
    assert names == ["CLI-Anything", "impeccable"]
    impeccable = next(r for r in registry["records"].values() if r["name"] == "impeccable")
    assert len(impeccable["installs"]) == 2
    assert impeccable["installed_at"] == "2026-09-12T10:00:00+00:00"


def test_update_persists_under_a_lock(tmp_path):
    path = tmp_path / "state" / "benchmarks.json"
    benchmarks_lib.update(path, lambda reg: benchmarks_lib.register_install(
        reg, "SC1", git_action(), ok_result("/t/impeccable"), NOW))
    assert len(benchmarks_lib.load(path)["records"]) == 1


def test_path_for_sits_beside_staging(tmp_path):
    assert benchmarks_lib.path_for(tmp_path / "staging") == (tmp_path / "state" / "benchmarks.json").resolve()


def test_due_skips_recent_errors_and_settled_records():
    records = {
        "a": {"id": "a", "state": "not_benchmarked", "attempted_at": None},
        "b": {"id": "b", "state": "not_benchmarked", "attempted_at": "2026-09-15T11:00:00+00:00"},
        "c": {"id": "c", "state": "drafted", "attempted_at": "2026-09-15T01:00:00+00:00"},
        "d": {"id": "d", "state": "passing", "attempted_at": None},
        "e": {"id": "e", "state": "verdict_sent", "attempted_at": None},
    }
    due = benchmarks_lib.due({"records": records}, NOW, retry_after_s=6 * 3600)
    assert [r["id"] for r in due] == ["a", "c"]


def test_attention_lists_unbenchmarked_and_failing():
    records = {k: {"id": k, "name": k, "state": s} for k, s in [
        ("n", "not_benchmarked"), ("d", "drafted"), ("p", "passing"), ("f", "verdict_sent"),
        ("k", "kept_despite_failure"), ("u", "uninstalled")]}
    view = benchmarks_lib.attention({"records": records})
    assert [r["id"] for r in view["not_benchmarked"]] == ["n", "d"]
    assert [r["id"] for r in view["failing"]] == ["f"]


# --- install_artifact hook ----------------------------------------------------------------

def test_a_successful_install_lands_in_the_registry(tmp_path, monkeypatch):
    import install_artifact
    import registry as handlers
    staging = tmp_path / "staging"
    action = git_action(status="pending")
    staging_lib.write_proposal("SC1", staging, {"shortcode": "SC1", "status": "pending", "actions": [action]})
    handlers.load_all_handlers()
    handler = handlers.get_handler("git_repo")
    target = tmp_path / "tools" / "impeccable"
    monkeypatch.setattr(handler, "target_path", lambda p: target)
    monkeypatch.setattr(handler, "collides", lambda p: None)
    monkeypatch.setattr(handler, "install", lambda p, t, c=None: ok_result(t))
    monkeypatch.setattr(install_artifact, "validate_path", lambda t: Path(t))
    install_artifact.install_action(action, "SC1", staging)
    records = benchmarks_lib.load(benchmarks_lib.path_for(staging))["records"]
    assert [(r["name"], r["state"]) for r in records.values()] == [("impeccable", "not_benchmarked")]


def test_a_failed_or_skipped_install_does_not(tmp_path, monkeypatch):
    import install_artifact
    import registry as handlers
    staging = tmp_path / "staging"
    action = git_action(status="pending")
    staging_lib.write_proposal("SC1", staging, {"shortcode": "SC1", "status": "pending",
                                                "actions": [action, git_action("a2", status="pending")]})
    handlers.load_all_handlers()
    handler = handlers.get_handler("git_repo")
    monkeypatch.setattr(handler, "target_path", lambda p: tmp_path / "x")
    monkeypatch.setattr(handler, "collides", lambda p: None)
    monkeypatch.setattr(handler, "install", lambda p, t, c=None: {"ok": False, "path": None, "error": "exit 1"})
    monkeypatch.setattr(install_artifact, "validate_path", lambda t: Path(t))
    install_artifact.install_action(action, "SC1", staging)
    install_artifact.install_action(git_action("a2", status="pending"), "SC1", staging, resolution="cancel")
    assert benchmarks_lib.load(benchmarks_lib.path_for(staging))["records"] == {}


# --- quota_router --------------------------------------------------------------------------

USAGE = {
    "claude": {"weekly": {"pct": 89, "pace_delta": 10.4}, "five_hour": {"pct": 9}},
    "codex": {"weekly": {"pct": 50, "pace_delta": -5.0}, "five_hour": {"pct": 1}},
    "antigravity": {"model_pools": {
        "gemini": {"weekly": {"pct": 15, "pace_delta": 50.1},
                   "five_hour": {"pct": 44, "resets_at": "2026-09-15T11:50:26Z"}},
        "3p": {"weekly": {"pct": 0, "pace_delta": 12.1}, "five_hour": {"pct": 0}}}},
}


def test_pick_quota_takes_the_one_most_ahead_of_weekly_pace():
    assert quota_router.pick_quota(USAGE, NOW) == "agy-gemini"


def test_pick_quota_skips_a_quota_whose_five_hour_window_is_nearly_spent():
    usage = json.loads(json.dumps(USAGE))
    usage["antigravity"]["model_pools"]["gemini"]["five_hour"] = {"pct": 95, "resets_at": "2026-09-15T14:00:00Z"}
    assert quota_router.pick_quota(usage, NOW) == "agy-3p"


def test_pick_quota_ignores_an_already_reset_five_hour_window():
    usage = json.loads(json.dumps(USAGE))
    usage["antigravity"]["model_pools"]["gemini"]["five_hour"] = {"pct": 99, "resets_at": "2026-09-15T11:00:00Z"}
    assert quota_router.pick_quota(usage, NOW) == "agy-gemini"


def test_pick_quota_skips_exhausted_and_unknown_quotas():
    usage = {"claude": {"weekly": {"pct": 100, "pace_delta": 30}}, "codex": {"weekly": {"pct": 10}}}
    assert quota_router.pick_quota(usage, NOW) is None
    assert quota_router.pick_quota(None, NOW) is None


def test_profiles_match_dans_model_choices():
    p = quota_router.PROFILES
    assert p["claude"]["work"] == {"backend": "claude", "model": "sonnet", "effort": "medium"}
    assert p["claude"]["judge"] == {"backend": "claude", "model": "opus", "effort": "medium"}
    assert p["codex"]["work"] == {"backend": "codex", "model": "gpt-5.6-terra", "effort": "medium"}
    assert p["codex"]["judge"] == {"backend": "codex", "model": "gpt-5.6-sol", "effort": "medium"}
    assert p["agy-gemini"]["work"]["model"] == "gemini-3.8-flash-low"
    assert p["agy-gemini"]["judge"]["model"] == "gemini-3.8-flash-high"
    assert p["agy-3p"]["work"]["model"] == "claude-sonnet-4-6"
    assert p["agy-3p"]["judge"]["model"] == "claude-opus-4-6-thinking"


def test_fetch_usage_never_raises(tmp_path):
    assert quota_router.fetch_usage(url="http://127.0.0.1:9/usage", token_path=tmp_path / "missing") is None


# --- agents_lib backends --------------------------------------------------------------------

def test_build_cmd_agy_passes_model_workdir_and_prompt():
    cmd = agents_lib.build_cmd("agy", "do it", model="gemini-3.8-flash-low", workdir="/tmp/w", timeout=600)
    assert cmd[0] == "agy" and cmd[-2:] == ["-p", "do it"]
    assert ["--model", "gemini-3.8-flash-low"] == cmd[cmd.index("--model"):cmd.index("--model") + 2]
    assert "--add-dir" in cmd and "/tmp/w" in cmd and "accept-edits" in cmd
    assert "600s" in cmd


def test_build_cmd_effort_and_workdir_for_claude_and_codex():
    claude = agents_lib.build_cmd("claude", "p", model="sonnet", effort="medium", workdir="/tmp/w")
    assert ["--effort", "medium"] == claude[claude.index("--effort"):claude.index("--effort") + 2]
    assert "--restricted" in claude and "acceptEdits" in claude
    codex = agents_lib.build_cmd("codex", "p", model="gpt-5.6-terra", effort="medium", workdir="/tmp/w")
    assert "model_reasoning_effort=medium" in codex
    assert codex[codex.index("--sandbox") + 1] == "workspace-write"
    assert codex[codex.index("-C") + 1] == "/tmp/w"
    assert agents_lib.build_cmd("codex", "p")[agents_lib.build_cmd("codex", "p").index("--sandbox") + 1] == "read-only"


def test_agy_envelope_is_unwrapped():
    out = json.dumps({"status": "SUCCESS", "response": "hello\n"})
    assert agents_lib.parse_output("agy", out) == ("hello\n", None)
    assert agents_lib.parse_output("agy", json.dumps({"status": "ERROR", "response": ""}))[1]


# --- benchmark: pure pieces -----------------------------------------------------------------

def test_find_skill_doc_prefers_skill_md_then_claude_skill_then_readme(tmp_path):
    repo = tmp_path / "impeccable"
    (repo / ".claude" / "skills" / "impeccable").mkdir(parents=True)
    (repo / ".claude" / "skills" / "impeccable" / "SKILL.md").write_text("skill")
    (repo / "README.md").write_text("readme")
    assert benchmark.find_skill_doc(repo).name == "SKILL.md"
    (repo / "SKILL.md").write_text("root")
    assert benchmark.find_skill_doc(repo) == repo / "SKILL.md"
    assert benchmark.find_skill_doc(repo / "SKILL.md") == repo / "SKILL.md"
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "README.md").write_text("r")
    assert benchmark.find_skill_doc(bare) == bare / "README.md"
    assert benchmark.find_skill_doc(tmp_path / "nope") is None
    assert benchmark.find_skill_doc(None) is None


TASKS = {"tasks": [
    {"id": "t1", "title": "Audit a page", "prompt": "Write audit.md for this HTML ...",
     "pass_criteria": ["lists at least 3 issues", "names a fix for each"]},
    {"id": "t2", "title": "Polish copy", "prompt": "Rewrite hero.txt ...", "pass_criteria": ["under 20 words"]},
]}


def test_parse_tasks_accepts_2_to_4_and_recovers_a_dropped_brace():
    assert [t["id"] for t in benchmark.parse_tasks(json.dumps(TASKS))] == ["t1", "t2"]
    broken = json.dumps(TASKS)[:-2]  # the closing "]}" dropped, as Claude did live
    assert [t["id"] for t in benchmark.parse_tasks(broken)] == ["t1", "t2"]
    with pytest.raises(ValueError):
        benchmark.parse_tasks(json.dumps({"tasks": TASKS["tasks"][:1]}))
    many = {"tasks": [dict(TASKS["tasks"][0], id=f"t{i}") for i in range(6)]}
    assert len(benchmark.parse_tasks(json.dumps(many))) == 4


def test_parse_verdicts_needs_every_task():
    text = json.dumps({"tasks": [{"id": "t1", "passed": True, "reason": "ok"},
                                 {"id": "t2", "passed": False, "reason": "42 words"}]})
    verdicts = benchmark.parse_verdicts(text, ["t1", "t2"])
    assert verdicts["t2"] == {"passed": False, "reason": "42 words"}
    with pytest.raises(ValueError):
        benchmark.parse_verdicts(json.dumps({"tasks": [{"id": "t1", "passed": True, "reason": "ok"}]}),
                                 ["t1", "t2"])


def test_failure_message_is_plain_and_has_remove_keep():
    record = {"id": "abc123", "name": "impeccable"}
    text = benchmark.format_failure_message(record, TASKS["tasks"][1], "42 words, limit was 20")
    assert "impeccable" in text and "Polish copy" in text and "42 words" in text
    assert "still installed" in text.lower()
    assert "abc123" not in text and "SC1" not in text
    assert benchmark.failure_buttons("abc123") == [[("🗑 Remove", "bench_rm:abc123"),
                                                    ("👍 Keep", "bench_keep:abc123")]]


# --- benchmark: orchestration ---------------------------------------------------------------

class FakeAgent:
    """Answers draft, run and judge prompts by role; records every call."""

    def __init__(self, verdict=True, fail_role=None):
        self.calls, self.verdict, self.fail_role = [], verdict, fail_role

    def __call__(self, prompt, **kw):
        role = "draft" if "BENCHMARK DRAFT" in prompt else "judge" if "BENCHMARK JUDGE" in prompt else "run"
        self.calls.append({"role": role, **kw})
        if role == self.fail_role:
            return {"ok": False, "text": "", "error": "quota exceeded"}
        if role == "draft":
            return {"ok": True, "text": json.dumps(TASKS), "error": None}
        if role == "run":
            Path(kw["workdir"], "answer.md").write_text("did the thing")
            return {"ok": True, "text": "done", "error": None}
        return {"ok": True, "error": None, "text": json.dumps({"tasks": [
            {"id": "t1", "passed": True, "reason": "fine"},
            {"id": "t2", "passed": self.verdict, "reason": "fine" if self.verdict else "42 words, limit was 20"}]})}


def setup_record(tmp_path):
    skill = tmp_path / "tools" / "impeccable"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# impeccable\nMakes frontends impeccable.")
    staging = tmp_path / "staging"
    path = benchmarks_lib.path_for(staging)
    record = benchmarks_lib.update(path, lambda reg: benchmarks_lib.register_install(
        reg, "SC1", git_action(), ok_result(skill), NOW))
    return path, tmp_path / "benchmarks", record


def run(tmp_path, agent, sent, usage=USAGE):
    path, bench_root, record = setup_record(tmp_path)
    summary = benchmark.benchmark_one(record["id"], registry_path=path, bench_root=bench_root,
                                      usage_fn=lambda: usage, run_agent_fn=agent,
                                      notify_fn=lambda text, buttons: sent.append((text, buttons)) or 777)
    return path, bench_root, record, summary


def test_a_passing_benchmark_updates_the_record_silently(tmp_path):
    agent, sent = FakeAgent(verdict=True), []
    path, bench_root, record, summary = run(tmp_path, agent, sent)
    assert summary["verdict"] == "pass" and sent == []
    assert summary["agent_calls"] == 4 and summary["quota"] == "agy-gemini"
    stored = benchmarks_lib.load(path)["records"][record["id"]]
    assert stored["state"] == "passing" and stored["last_run"] == summary["run_dir"]
    assert (bench_root / "impeccable" / "bench.md").read_text().count("Pass criteria") == 2
    run_json = json.loads((Path(summary["run_dir"]) / "run.json").read_text())
    assert run_json["verdicts"]["t2"]["passed"] is True
    assert (Path(summary["run_dir"]) / "t1" / "answer.md").exists()
    roles = {c["role"]: c for c in agent.calls}
    assert roles["draft"]["model"] == "gemini-3.8-flash-low" and roles["judge"]["model"] == "gemini-3.8-flash-high"
    assert roles["run"]["backend"] == "agy" and roles["run"]["workdir"]


def test_a_failing_benchmark_sends_exactly_one_message_with_remove_keep(tmp_path):
    agent, sent = FakeAgent(verdict=False), []
    path, _, record, summary = run(tmp_path, agent, sent)
    assert summary["verdict"] == "fail" and len(sent) == 1
    text, buttons = sent[0]
    assert "Polish copy" in text and "42 words" in text
    assert buttons == benchmark.failure_buttons(record["id"])
    stored = benchmarks_lib.load(path)["records"][record["id"]]
    assert stored["state"] == "verdict_sent" and stored["message_id"] == 777
    assert [h["state"] for h in stored["history"]] == ["drafted", "running", "failing", "verdict_sent"]


def test_an_agent_error_is_not_a_verdict_and_retries_later(tmp_path):
    sent = []
    path, _, record, summary = run(tmp_path, FakeAgent(fail_role="judge"), sent)
    assert summary["verdict"] == "error" and sent == []
    stored = benchmarks_lib.load(path)["records"][record["id"]]
    assert stored["state"] == "drafted" and "quota exceeded" in stored["last_error"]
    assert stored["attempted_at"]


def test_a_draft_error_leaves_the_record_not_benchmarked(tmp_path):
    path, _, record, summary = run(tmp_path, FakeAgent(fail_role="draft"), [])
    stored = benchmarks_lib.load(path)["records"][record["id"]]
    assert summary["verdict"] == "error" and stored["state"] == "not_benchmarked" and stored["last_error"]


def test_no_quota_headroom_means_no_agent_calls(tmp_path):
    agent = FakeAgent()
    path, _, record, summary = run(tmp_path, agent, [], usage=None)
    assert summary["verdict"] == "deferred" and agent.calls == []
    assert benchmarks_lib.load(path)["records"][record["id"]]["state"] == "not_benchmarked"


def test_stub_verdict_forces_a_failure_without_a_judge_call(tmp_path):
    agent, sent = FakeAgent(verdict=True), []
    path, bench_root, record = setup_record(tmp_path)
    summary = benchmark.benchmark_one(record["id"], registry_path=path, bench_root=bench_root,
                                      usage_fn=lambda: USAGE, run_agent_fn=agent, stub_verdict="fail",
                                      notify_fn=lambda text, buttons: sent.append((text, buttons)) or 1)
    assert summary["verdict"] == "fail" and len(sent) == 1
    assert "judge" not in {c["role"] for c in agent.calls}


def test_run_next_benchmarks_only_the_first_due_record(tmp_path):
    agent, sent = FakeAgent(), []
    path, bench_root, record = setup_record(tmp_path)
    kw = dict(registry_path=path, bench_root=bench_root, usage_fn=lambda: USAGE, run_agent_fn=agent,
              notify_fn=lambda t, b: 1)
    assert benchmark.run_next(**kw)["verdict"] == "pass"
    assert benchmark.run_next(**kw) is None


# --- removal ---------------------------------------------------------------------------------

def test_remove_installs_deletes_only_allowlisted_paths(tmp_path):
    inside = tmp_path / "tools" / "impeccable"
    inside.mkdir(parents=True)
    skill_md = tmp_path / "skills" / "polish" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("x")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    record = {"installs": [
        {"type": "git_repo", "path": str(inside)}, {"type": "skill", "path": str(skill_md)},
        {"type": "git_repo", "path": str(outside)},
        {"type": "package_install", "path": None, "command": "npx skills add pbakaus/impeccable"}]}
    notes = benchmark.remove_installs(record, allowed_roots=[tmp_path / "tools", tmp_path / "skills"],
                                      sync_fn=lambda: None)
    assert not inside.exists() and not skill_md.parent.exists() and outside.exists()
    joined = "\n".join(notes)
    assert "npx skills add pbakaus/impeccable" in joined and "elsewhere" in joined


# --- telegram_bot: Remove / Keep and /benchmarks ----------------------------------------------

def _bot_record(tmp_path, state="verdict_sent"):
    staging = tmp_path / "staging"
    path = benchmarks_lib.path_for(staging)
    tool = tmp_path / "tools" / "impeccable"
    tool.mkdir(parents=True)

    def mk(reg):
        record = benchmarks_lib.register_install(reg, "SC1", git_action(), ok_result(tool), NOW)
        record.update(state=state)
        return record
    return staging, path, tool, benchmarks_lib.update(path, mk)


def _tap(staging, data):
    import asyncio
    import telegram_bot
    from test_ingest import _FakeContext, _FakeQuery
    sent = []
    query = _FakeQuery(sent, data)
    asyncio.run(telegram_bot.handle_callback(type("U", (), {"callback_query": query})(), _FakeContext(staging)))
    return sent, query


def test_keep_marks_kept_despite_failure_and_deletes_nothing(tmp_path):
    staging, path, tool, record = _bot_record(tmp_path)
    sent, query = _tap(staging, f"bench_keep:{record['id']}")
    assert benchmarks_lib.load(path)["records"][record["id"]]["state"] == "kept_despite_failure"
    assert tool.exists() and query.markup_cleared
    assert sent and "Kept" in sent[0]["text"]


def test_remove_deletes_the_install_and_marks_uninstalled(tmp_path, monkeypatch):
    import telegram_bot
    staging, path, tool, record = _bot_record(tmp_path)
    monkeypatch.setattr(telegram_bot.benchmark, "remove_installs",
                        lambda rec: [f"removed {i['path']}" for i in rec["installs"]])
    sent, _ = _tap(staging, f"bench_rm:{record['id']}")
    assert benchmarks_lib.load(path)["records"][record["id"]]["state"] == "uninstalled"
    assert "Removed" in sent[0]["text"]


def test_a_second_tap_changes_nothing(tmp_path):
    staging, path, tool, record = _bot_record(tmp_path, state="kept_despite_failure")
    sent, _ = _tap(staging, f"bench_rm:{record['id']}")
    assert benchmarks_lib.load(path)["records"][record["id"]]["state"] == "kept_despite_failure"
    assert tool.exists() and "already" in sent[0]["text"]


def test_cmd_benchmarks_lists_unbenchmarked_and_failing(tmp_path):
    import asyncio
    import telegram_bot
    from test_ingest import _FakeContext, _FakeUpdate
    staging, path, _, record = _bot_record(tmp_path, state="not_benchmarked")
    sent = []
    asyncio.run(telegram_bot.cmd_benchmarks(_FakeUpdate(sent), _FakeContext(staging)))
    assert "impeccable" in sent[0]["text"] and "Not benchmarked" in sent[0]["text"]


# --- worker hook ------------------------------------------------------------------------------

def test_worker_benchmarks_only_when_the_job_queue_is_empty(tmp_path):
    import worker
    calls = []
    (tmp_path / "queued").mkdir()
    worker.maybe_benchmark(tmp_path, run_next_fn=lambda: calls.append(1), min_interval_s=0)
    assert calls == [1]
    (tmp_path / "queued" / "X.json").write_text("{}")
    worker.maybe_benchmark(tmp_path, run_next_fn=lambda: calls.append(2), min_interval_s=0)
    assert calls == [1]


def test_worker_survives_a_raising_benchmark(tmp_path):
    import worker

    def boom():
        raise RuntimeError("x")
    worker.maybe_benchmark(tmp_path, run_next_fn=boom, min_interval_s=0)
