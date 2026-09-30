"""Pure-logic unit tests for Phase 4a (interpret stage + action registry).
Run with: python3 -m pytest scripts/test_interpret.py -v
No live agent/Telegram calls -- those live inside functions these tests
don't invoke (matches test_extract.py/test_ingest.py's convention)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import agents_lib
import subprocess
import registry
import reference
import note
import discard
import unsupported
import rule
import skill as skill_action
import package_install
import skill_install
import _skill_directory
import _skills_root
import shell_snippet
import git_repo
import calendar_event
import staging_lib
import install_artifact
import resolve_tools
import interpret


class _FakeHandler:
    """Stand-in satisfying the full handler contract, used to test the
    registry framework in isolation before any real handler exists."""
    TYPE = "fake_type"
    RISK = "config"
    SCHEMA = {"required": ["foo"], "types": {"foo": str}, "enum": {"foo": ("a", "b")}}
    __doc__ = "A fake handler for registry tests."

    @staticmethod
    def target_path(payload):
        return None

    @staticmethod
    def describe(payload):
        return "fake"

    @staticmethod
    def preview(payload):
        return "fake"

    @staticmethod
    def collides(payload):
        return None

    @staticmethod
    def install(payload, target, context=None):
        return {"ok": True}


def _fresh_registry():
    reg = {}
    registry.register(_FakeHandler, registry_dict=reg)
    return reg


def test_build_cmd_claude_default():
    cmd = agents_lib.build_cmd("claude", "prompt text")
    assert cmd == ["claude", "-p", "--output-format", "json", "--restricted"]


def test_build_cmd_claude_with_search_and_model():
    cmd = agents_lib.build_cmd("claude", "p", allow_search=True, model="sonnet")
    assert cmd == ["claude", "-p", "--output-format", "json", "--restricted",
                    "--allowed-tools", "WebSearch", "--model", "sonnet"]


def test_build_cmd_codex_default():
    cmd = agents_lib.build_cmd("codex", "prompt text")
    assert cmd == ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]


def test_build_cmd_codex_with_search_schema_output():
    cmd = agents_lib.build_cmd("codex", "p", allow_search=True,
                                 schema_path=Path("/tmp/s.json"), output_path=Path("/tmp/o.txt"))
    assert cmd == ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                    "--search", "--output-schema", "/tmp/s.json", "-o", "/tmp/o.txt"]


def test_build_cmd_unknown_backend_raises():
    try:
        agents_lib.build_cmd("gpt4", "p")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_extract_json_fenced_block():
    text = 'Here is the answer:\n```json\n{"a": 1, "b": [2, 3]}\n```\nHope that helps.'
    assert agents_lib.extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_bare_object():
    text = '{"a": 1}'
    assert agents_lib.extract_json(text) == {"a": 1}


def test_extract_json_trailing_prose():
    text = '{"a": 1, "nested": {"b": 2}}\n\nLet me know if you need anything else!'
    assert agents_lib.extract_json(text) == {"a": 1, "nested": {"b": 2}}


def test_extract_json_no_json_raises():
    try:
        agents_lib.extract_json("no json here at all")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_get_backend_defaults_to_claude_when_missing(tmp_path):
    assert agents_lib.get_backend(tmp_path / "nope.json") == "claude"


def test_set_backend_then_get_backend(tmp_path):
    config_path = tmp_path / "agent.json"
    agents_lib.set_backend("codex", config_path=config_path)
    assert agents_lib.get_backend(config_path) == "codex"


def test_set_backend_preserves_model(tmp_path):
    config_path = tmp_path / "agent.json"
    agents_lib.set_backend("claude", config_path=config_path, model="opus")
    agents_lib.set_backend("codex", config_path=config_path)
    assert agents_lib.get_model(config_path) == "opus"


def test_set_backend_unknown_raises(tmp_path):
    try:
        agents_lib.set_backend("gpt4", config_path=tmp_path / "agent.json")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_register_populates_registry_dict():
    reg = _fresh_registry()
    assert reg["fake_type"] is _FakeHandler


def test_register_rejects_missing_member():
    class Incomplete:
        TYPE = "incomplete"
        RISK = "inert"
        SCHEMA = {}
        # missing target_path/describe/preview/collides/install

    try:
        registry.register(Incomplete, registry_dict={})
        assert False, "expected AttributeError"
    except AttributeError:
        pass


def test_register_rejects_bad_risk():
    class BadRisk(_FakeHandler):
        TYPE = "bad_risk_type"
        RISK = "catastrophic"

    try:
        registry.register(BadRisk, registry_dict={})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_payload_missing_required_field():
    reg = _fresh_registry()
    ok, error = registry.validate_payload("fake_type", {}, registry_dict=reg)
    assert ok is False
    assert "foo" in error


def test_validate_payload_wrong_type():
    reg = _fresh_registry()
    ok, error = registry.validate_payload("fake_type", {"foo": 123}, registry_dict=reg)
    assert ok is False
    assert "str" in error


def test_validate_payload_bad_enum_value():
    reg = _fresh_registry()
    ok, error = registry.validate_payload("fake_type", {"foo": "z"}, registry_dict=reg)
    assert ok is False


def test_validate_payload_bad_pattern_value():
    class PatternHandler(_FakeHandler):
        TYPE = "pattern_type"
        SCHEMA = {"required": ["url"], "types": {"url": str}, "patterns": {"url": r"^https?://\S+$"}}

    reg = {}
    registry.register(PatternHandler, registry_dict=reg)
    ok, error = registry.validate_payload("pattern_type", {"url": "not a url"}, registry_dict=reg)
    assert ok is False
    ok, error = registry.validate_payload("pattern_type", {"url": "https://example.com"}, registry_dict=reg)
    assert ok is True


def test_git_repo_rejects_empty_url():
    reg = {}
    registry.register(git_repo, registry_dict=reg)
    ok, error = registry.validate_payload("git_repo", {"name": "x", "url": ""}, registry_dict=reg)
    assert ok is False


def test_git_repo_rejects_non_url_placeholder_text():
    reg = {}
    registry.register(git_repo, registry_dict=reg)
    ok, error = registry.validate_payload(
        "git_repo", {"name": "x", "url": "UNVERIFIED -- no repo link found"}, registry_dict=reg)
    assert ok is False


def test_git_repo_accepts_real_url():
    reg = {}
    registry.register(git_repo, registry_dict=reg)
    ok, error = registry.validate_payload(
        "git_repo", {"name": "x", "url": "https://github.com/x/y"}, registry_dict=reg)
    assert ok is True


def test_validate_payload_unknown_type():
    ok, error = registry.validate_payload("nonexistent_type", {}, registry_dict={})
    assert ok is False
    assert "unknown action type" in error


def test_validate_payload_valid():
    reg = _fresh_registry()
    ok, error = registry.validate_payload("fake_type", {"foo": "a"}, registry_dict=reg)
    assert ok is True
    assert error is None


def test_validate_action_confidence_out_of_range():
    reg = _fresh_registry()
    action = {"type": "fake_type", "confidence": 1.5, "payload": {"foo": "a"}}
    ok, error = registry.validate_action(action, registry_dict=reg)
    assert ok is False


def test_validate_action_missing_confidence():
    reg = _fresh_registry()
    action = {"type": "fake_type", "payload": {"foo": "a"}}
    ok, error = registry.validate_action(action, registry_dict=reg)
    assert ok is False


def test_validate_actions_all_valid():
    reg = _fresh_registry()
    actions = [{"type": "fake_type", "confidence": 0.9, "payload": {"foo": "a"}}]
    ok, errors = registry.validate_actions(actions, registry_dict=reg)
    assert ok is True
    assert errors == []


def test_validate_actions_collects_all_errors():
    reg = _fresh_registry()
    actions = [
        {"type": "fake_type", "confidence": 0.9, "payload": {}},
        {"type": "nonexistent", "confidence": 0.5, "payload": {}},
    ]
    ok, errors = registry.validate_actions(actions, registry_dict=reg)
    assert ok is False
    assert len(errors) == 2


def test_generate_prompt_catalogue_includes_type_and_risk():
    reg = _fresh_registry()
    catalogue = registry.generate_prompt_catalogue(registry_dict=reg)
    assert "fake_type" in catalogue
    assert "config" in catalogue
    assert "foo" in catalogue


def test_reference_target_path_uses_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "CAPTURED_KNOWLEDGE_ROOT", tmp_path)
    target = reference.target_path({"title": "Zoxide Tips!", "content": "x"})
    assert target == tmp_path / "facts" / "zoxide-tips.md"


def test_reference_collides_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "CAPTURED_KNOWLEDGE_ROOT", tmp_path)
    assert reference.collides({"title": "New Thing", "content": "x"}) is None


def test_reference_collides_true_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "CAPTURED_KNOWLEDGE_ROOT", tmp_path)
    target = reference.target_path({"title": "Existing", "content": "x"})
    target.parent.mkdir(parents=True)
    target.write_text("old")
    assert reference.collides({"title": "Existing", "content": "x"}) == target


def test_reference_install_writes_file_and_index(tmp_path):
    target = tmp_path / "facts" / "some-fact.md"
    result = reference.install({"title": "Some Fact", "content": "the content"}, target)
    assert result["ok"] is True
    assert target.read_text() == "the content"


def test_note_target_path_uses_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(note, "CAPTURED_KNOWLEDGE_ROOT", tmp_path)
    target = note.target_path({"title": "A Note", "content": "x"})
    assert target == tmp_path / "notes" / "a-note.md"


def test_note_install_writes_file(tmp_path):
    target = tmp_path / "notes" / "n.md"
    result = note.install({"title": "N", "content": "body"}, target)
    assert result["ok"] is True
    assert target.read_text() == "body"


def test_discard_target_path_is_none():
    assert discard.target_path({"reason": "x"}) is None


def test_discard_collides_always_none():
    assert discard.collides({"reason": "x"}) is None


def test_discard_install_appends_jsonl(monkeypatch, tmp_path):
    log_path = tmp_path / "discarded.jsonl"
    monkeypatch.setattr(discard, "DISCARDED_LOG", log_path)
    discard.install({"reason": "personal essay, not tech"}, None, {"shortcode": "ABC123", "transcript_excerpt": "..."})
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["shortcode"] == "ABC123"
    assert record["reason"] == "personal essay, not tech"


def test_unsupported_install_appends_jsonl(monkeypatch, tmp_path):
    log_path = tmp_path / "unsupported.jsonl"
    monkeypatch.setattr(unsupported, "UNSUPPORTED_LOG", log_path)
    payload = {"proposed_type": "browser_extension", "payload_sketch": {"name": "x"}, "rationale": "no handler"}
    unsupported.install(payload, None, {"shortcode": "XYZ789"})
    record = json.loads(log_path.read_text().splitlines()[0])
    assert record["proposed_type"] == "browser_extension"
    assert record["shortcode"] == "XYZ789"


def test_unsupported_collides_always_none():
    assert unsupported.collides({"proposed_type": "x", "payload_sketch": {}, "rationale": "y"}) is None


def test_rule_target_path_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path)
    target = rule.target_path({"topic": "Claude Code Token Costs", "content": "x"})
    assert target == tmp_path / "claude-code-token-costs.md"


def test_rule_collides_true_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path)
    target = rule.target_path({"topic": "existing", "content": "x"})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old")
    assert rule.collides({"topic": "existing", "content": "x"}) == target


def test_rule_install_writes_content(tmp_path):
    target = tmp_path / "topic.md"
    result = rule.install({"topic": "topic", "content": "the rule text"}, target)
    assert result["ok"] is True
    assert target.read_text() == "the rule text"


def test_skill_target_path_uses_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_action, "SKILLS_ROOT", tmp_path)
    target = skill_action.target_path({"name": "Token Saver", "description": "d", "content": "c"})
    assert target == tmp_path / "token-saver" / "SKILL.md"


def test_skill_install_writes_frontmatter(tmp_path):
    target = tmp_path / "token-saver" / "SKILL.md"
    payload = {"name": "Token Saver", "description": "Cuts Claude Code costs", "content": "body text"}
    result = skill_action.install(payload, target)
    assert result["ok"] is True
    written = target.read_text()
    assert "name: token-saver" in written
    assert "description: Cuts Claude Code costs" in written
    assert "body text" in written


def test_skill_targets_the_canonical_repo_not_an_agent_directory():
    """Skills are single-sourced in ~/agent-skills/skills and reach
    ~/.claude/skills only as symlinks `agent-skills sync` maintains, so
    writing into the agent directory would be unversioned and Claude-only."""
    assert skill_action.SKILLS_ROOT == Path.home() / "agent-skills" / "skills"
    target = skill_action.target_path({"name": "Token Saver", "description": "d", "content": "c"})
    assert target == Path.home() / "agent-skills" / "skills" / "token-saver" / "SKILL.md"


def test_skill_install_syncs_a_canonical_target(monkeypatch, tmp_path):
    """Writing the SKILL.md is half the install -- until sync links it, no
    agent can see it, so a sync failure must surface as a note."""
    calls = []
    monkeypatch.setattr(_skills_root, "sync", lambda: calls.append(1) or "sync broke")
    monkeypatch.setattr(_skills_root, "SKILLS_ROOT", tmp_path)
    payload = {"name": "Token Saver", "description": "d", "content": "c"}
    result = skill_action.install(payload, tmp_path / "token-saver" / "SKILL.md")
    assert result["ok"] is True and result["note"] == "sync broke"
    assert calls == [1]


def test_skill_install_does_not_sync_a_target_outside_the_repo(monkeypatch, tmp_path):
    def boom():
        raise AssertionError("should not sync a target outside the canonical repo")

    monkeypatch.setattr(_skills_root, "sync", boom)
    result = skill_action.install({"name": "x", "description": "d", "content": "c"},
                                    tmp_path / "x" / "SKILL.md")
    assert result["ok"] is True and result["note"] is None


def test_skill_collides_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_action, "SKILLS_ROOT", tmp_path)
    assert skill_action.collides({"name": "new-skill", "description": "d", "content": "c"}) is None


def test_package_install_preview_is_literal_command():
    payload = {"manager": "cargo", "package": "zoxide", "command": "cargo install zoxide"}
    assert package_install.preview(payload) == "cargo install zoxide"


def test_package_install_target_path_none():
    payload = {"manager": "cargo", "package": "zoxide", "command": "cargo install zoxide"}
    assert package_install.target_path(payload) is None


def test_package_install_collides_always_none():
    payload = {"manager": "pip", "package": "x", "command": "pip install x"}
    assert package_install.collides(payload) is None


def test_package_install_rejects_unlisted_manager():
    reg = {}
    registry.register(package_install, registry_dict=reg)
    ok, error = registry.validate_payload(
        "package_install", {"manager": "brew", "package": "x", "command": "brew install x"}, registry_dict=reg)
    assert ok is False


def test_package_install_install_runs_command_and_captures_exit_code():
    payload = {"manager": "pip", "package": "x", "command": "true"}
    result = package_install.install(payload)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["command"] == "true"


def test_package_install_install_records_failure():
    payload = {"manager": "pip", "package": "x", "command": "false"}
    result = package_install.install(payload)
    assert result["ok"] is False
    assert result["exit_code"] != 0


def test_shell_snippet_target_path_is_bashrc():
    assert shell_snippet.target_path({"marker": "x", "snippet": "y", "description": "d"}) == shell_snippet.BASHRC


def test_shell_snippet_preview_includes_markers_and_snippet():
    payload = {"marker": "zoxide-init", "snippet": 'eval "$(zoxide init bash)"', "description": "d"}
    preview = shell_snippet.preview(payload)
    assert "instagram-to-value: zoxide-init" in preview
    assert 'eval "$(zoxide init bash)"' in preview


def test_shell_snippet_collides_false_when_marker_absent(monkeypatch, tmp_path):
    fake_bashrc = tmp_path / ".bashrc"
    fake_bashrc.write_text("# nothing here\n")
    monkeypatch.setattr(shell_snippet, "BASHRC", fake_bashrc)
    assert shell_snippet.collides({"marker": "new-marker", "snippet": "x", "description": "d"}) is None


def test_shell_snippet_collides_true_when_marker_present(monkeypatch, tmp_path):
    fake_bashrc = tmp_path / ".bashrc"
    monkeypatch.setattr(shell_snippet, "BASHRC", fake_bashrc)
    payload = {"marker": "dup", "snippet": "x", "description": "d"}
    shell_snippet.install(payload, fake_bashrc)
    assert shell_snippet.collides(payload) == fake_bashrc


def test_shell_snippet_install_appends_block(tmp_path):
    target = tmp_path / ".bashrc"
    target.write_text("# existing content\n")
    payload = {"marker": "zoxide-init", "snippet": 'eval "$(zoxide init bash)"', "description": "d"}
    result = shell_snippet.install(payload, target)
    assert result["ok"] is True
    text = target.read_text()
    assert "# existing content" in text
    assert 'eval "$(zoxide init bash)"' in text


def test_git_repo_target_path(monkeypatch, tmp_path):
    monkeypatch.setattr(git_repo, "TOOLS_ROOT", tmp_path)
    assert git_repo.target_path({"name": "mem-palace", "url": "https://x"}) == tmp_path / "mem-palace"


def test_git_repo_preview_is_literal_clone_command(monkeypatch, tmp_path):
    monkeypatch.setattr(git_repo, "TOOLS_ROOT", tmp_path)
    preview = git_repo.preview({"name": "mem-palace", "url": "https://github.com/x/mem-palace"})
    assert preview == f"git clone https://github.com/x/mem-palace {tmp_path / 'mem-palace'}"


def test_git_repo_collides_true_when_dir_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(git_repo, "TOOLS_ROOT", tmp_path)
    (tmp_path / "existing").mkdir()
    assert git_repo.collides({"name": "existing", "url": "https://x"}) == tmp_path / "existing"


def test_calendar_event_target_path_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar_event, "CALENDAR_ROOT", tmp_path)
    target = calendar_event.target_path({"title": "Apple Event!", "date": "2026-09-09"})
    assert target == tmp_path / "apple-event.ics"


def test_calendar_event_ics_contains_date_and_title():
    ics = calendar_event.preview({"title": "Apple Event", "date": "2026-09-09", "time": "10:00"})
    assert "SUMMARY:Apple Event" in ics
    assert "DTSTART:20260909T100000" in ics


def test_calendar_event_install_writes_ics(tmp_path):
    target = tmp_path / "apple-event.ics"
    result = calendar_event.install({"title": "Apple Event", "date": "2026-09-09"}, target)
    assert result["ok"] is True
    assert "BEGIN:VCALENDAR" in target.read_text()


def test_calendar_event_collides_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(calendar_event, "CALENDAR_ROOT", tmp_path)
    assert calendar_event.collides({"title": "New Event", "date": "2026-01-01"}) is None


def test_write_and_read_proposal_roundtrip(tmp_path):
    staging_lib.write_proposal("ABC", tmp_path, {"shortcode": "ABC", "actions": []})
    assert staging_lib.read_proposal("ABC", tmp_path) == {"shortcode": "ABC", "actions": []}


def test_read_proposal_missing_returns_none(tmp_path):
    assert staging_lib.read_proposal("NOPE", tmp_path) is None


def test_has_staging_true_after_write(tmp_path):
    staging_lib.write_proposal("ABC", tmp_path, {"shortcode": "ABC", "actions": []})
    assert staging_lib.has_staging("ABC", tmp_path) is True
    assert staging_lib.has_staging("NOPE", tmp_path) is False


def test_has_staging_false_when_only_agent_error_present(tmp_path):
    """A prior failed attempt (agent-error.txt, no proposal.json) must stay
    retryable -- found live during the Task 13 real backfill sweep, where a
    mid-sweep API spend-limit error would otherwise have permanently blocked
    a re-run from ever retrying those shortcodes."""
    d = staging_lib.staging_dir("ABC", tmp_path)
    d.mkdir(parents=True)
    (d / "agent-error.txt").write_text("{}")
    assert staging_lib.has_staging("ABC", tmp_path) is False


def test_list_pending_filters_by_status(tmp_path):
    staging_lib.write_proposal("A", tmp_path, {"shortcode": "A", "status": "pending", "actions": []})
    staging_lib.write_proposal("B", tmp_path, {"shortcode": "B", "status": "decided", "actions": []})
    assert staging_lib.list_pending(tmp_path) == ["A"]


def test_update_action_status_marks_decided_when_all_done(tmp_path):
    staging_lib.write_proposal("A", tmp_path, {
        "shortcode": "A", "status": "pending",
        "actions": [{"id": "a1", "status": "pending", "result": None}]})
    proposal = staging_lib.update_action_status("A", tmp_path, "a1", "installed", {"ok": True})
    assert proposal["actions"][0]["status"] == "installed"
    assert proposal["status"] == "decided"


def test_update_action_status_stays_pending_with_more_actions(tmp_path):
    staging_lib.write_proposal("A", tmp_path, {
        "shortcode": "A", "status": "pending",
        "actions": [{"id": "a1", "status": "pending", "result": None},
                    {"id": "a2", "status": "pending", "result": None}]})
    proposal = staging_lib.update_action_status("A", tmp_path, "a1", "installed", {"ok": True})
    assert proposal["status"] == "pending"


def test_set_message_id(tmp_path):
    staging_lib.write_proposal("A", tmp_path, {"shortcode": "A", "actions": [], "message_id": None})
    staging_lib.set_message_id("A", tmp_path, 4242)
    assert staging_lib.read_proposal("A", tmp_path)["message_id"] == 4242


def test_record_reject_reason_marks_pending_actions_skipped(tmp_path):
    staging_root = tmp_path / "staging"
    logs_root = tmp_path / "logs"
    staging_lib.write_proposal("A", staging_root, {
        "shortcode": "A", "summary": "s", "status": "pending",
        "actions": [{"id": "a1", "status": "pending", "result": None}]})
    staging_lib.record_reject_reason("A", staging_root, "not useful", logs_root=logs_root)
    proposal = staging_lib.read_proposal("A", staging_root)
    assert proposal["actions"][0]["status"] == "skipped"
    assert proposal["status"] == "decided"
    lines = (logs_root / "discarded.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["reason"] == "not useful"


def test_format_proposal_message_includes_summary_and_actions():
    proposal = {
        "shortcode": "ABC", "summary": "zoxide, a smarter cd",
        "resolver": {"status": "resolved", "tool_name": "zoxide"},
        "actions": [{"id": "a1", "type": "package_install", "risk": "exec",
                       "payload": {"package": "zoxide"}}],
    }
    proposal["actions"][0].update(status="pending", explanation={"what": "A smarter cd.", "why": "Demoed."})
    text = staging_lib.format_proposal_message(proposal, {"package_install": lambda p: f"install {p['package']}"})
    # per post, but each action rendered as the same card /pending uses
    assert text.splitlines() == ["ABC — zoxide, a smarter cd", "", "🔴 [a1] install zoxide",
                                 "What: A smarter cd.", "Why: Demoed."]


def test_is_auto_discard_eligible_true_for_backfill_high_confidence():
    assert staging_lib.is_auto_discard_eligible("backfill", "discard", 0.9) is True


def test_is_auto_discard_eligible_false_for_telegram_origin():
    assert staging_lib.is_auto_discard_eligible("telegram", "discard", 0.99) is False


def test_is_auto_discard_eligible_false_below_threshold():
    assert staging_lib.is_auto_discard_eligible("backfill", "discard", 0.5) is False


def test_is_auto_discard_eligible_false_for_non_discard_type():
    assert staging_lib.is_auto_discard_eligible("backfill", "reference", 0.99) is False


def test_format_digest_counts_categories_and_auto_discards():
    proposals = [
        {"shortcode": "A", "actions": [{"type": "package_install", "status": "pending"},
                                          {"type": "reference", "status": "pending"}]},
        {"shortcode": "B", "actions": [{"type": "discard", "status": "auto_discarded"}]},
        {"shortcode": "C", "actions": [{"type": "unsupported", "status": "pending"}]},
    ]
    digest = staging_lib.format_digest(proposals)
    assert "3 proposals" in digest
    assert "1 tools" in digest
    assert "1 references" in digest
    assert "1 auto-discarded" in digest
    assert "1 unsupported types" in digest


def test_aggregate_unsupported_counts_by_proposed_type(tmp_path):
    log_path = tmp_path / "unsupported.jsonl"
    log_path.write_text(
        json.dumps({"proposed_type": "browser_extension"}) + "\n" +
        json.dumps({"proposed_type": "browser_extension"}) + "\n" +
        json.dumps({"proposed_type": "rss_subscribe"}) + "\n"
    )
    counts = staging_lib.aggregate_unsupported(log_path)
    assert counts == {"browser_extension": 2, "rss_subscribe": 1}


def test_aggregate_unsupported_missing_file_returns_empty(tmp_path):
    assert staging_lib.aggregate_unsupported(tmp_path / "nope.jsonl") == {}


def test_format_unsupported_summary_sorted_desc():
    summary = staging_lib.format_unsupported_summary({"rss_subscribe": 1, "browser_extension": 2})
    assert summary == "⚠️ 3 posts wanted actions I can't do yet: browser_extension ×2, rss_subscribe ×1."


def test_format_unsupported_summary_empty_counts():
    assert staging_lib.format_unsupported_summary({}) == ""


def test_validate_path_accepts_valid_target(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    target = allowed_root / "sub" / "file.md"  # doesn't need to exist
    resolved = install_artifact.validate_path(target, allowed_roots=[allowed_root], allowed_files=[])
    assert resolved == target.resolve()


def test_validate_path_rejects_traversal(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    (tmp_path / "outside").mkdir()
    target = allowed_root / ".." / "outside" / "secret.txt"
    try:
        install_artifact.validate_path(target, allowed_roots=[allowed_root], allowed_files=[])
        assert False, "expected PathNotAllowedError"
    except install_artifact.PathNotAllowedError:
        pass


def test_validate_path_rejects_symlink_escape(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    escape_link = allowed_root / "escape"
    escape_link.symlink_to(outside)
    target = escape_link / "secret.txt"
    try:
        install_artifact.validate_path(target, allowed_roots=[allowed_root], allowed_files=[])
        assert False, "expected PathNotAllowedError"
    except install_artifact.PathNotAllowedError:
        pass


def test_validate_path_accepts_allowed_file_exact_match(tmp_path):
    fake_bashrc = tmp_path / ".bashrc"
    resolved = install_artifact.validate_path(fake_bashrc, allowed_roots=[], allowed_files=[fake_bashrc])
    assert resolved == fake_bashrc.resolve()


def test_validate_path_rejects_outside_root(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_target = tmp_path / "outside" / "file.md"
    try:
        install_artifact.validate_path(outside_target, allowed_roots=[allowed_root], allowed_files=[])
        assert False, "expected PathNotAllowedError"
    except install_artifact.PathNotAllowedError:
        pass


def test_install_action_unknown_type_fails(tmp_path):
    staging_root = tmp_path / "staging"
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{"id": "a1", "type": "nope", "status": "pending"}]})
    action = {"id": "a1", "type": "nope", "payload": {}}
    result = install_artifact.install_action(action, "ABC", staging_root)
    assert result["ok"] is False


def test_install_action_cancel_marks_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path / "rules")
    staging_root = tmp_path / "staging"
    action = {"id": "a1", "type": "rule", "payload": {"topic": "x", "content": "y"}}
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{**action, "status": "pending", "result": None}]})
    result = install_artifact.install_action(action, "ABC", staging_root, resolution="cancel")
    assert result["ok"] is True
    proposal = staging_lib.read_proposal("ABC", staging_root)
    assert proposal["actions"][0]["status"] == "skipped"


def test_install_action_installs_when_no_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path / "rules")
    monkeypatch.setattr(install_artifact, "ALLOWED_ROOTS", [tmp_path / "rules"])
    staging_root = tmp_path / "staging"
    action = {"id": "a1", "type": "rule", "payload": {"topic": "new-topic", "content": "content here"}}
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{**action, "status": "pending", "result": None}]})
    result = install_artifact.install_action(action, "ABC", staging_root)
    assert result["ok"] is True
    assert (tmp_path / "rules" / "new-topic.md").read_text() == "content here"
    proposal = staging_lib.read_proposal("ABC", staging_root)
    assert proposal["actions"][0]["status"] == "installed"


def test_install_action_reports_collision_without_deciding(tmp_path, monkeypatch):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path / "rules")
    monkeypatch.setattr(install_artifact, "ALLOWED_ROOTS", [tmp_path / "rules"])
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "dup.md").write_text("old content")
    staging_root = tmp_path / "staging"
    action = {"id": "a1", "type": "rule", "payload": {"topic": "dup", "content": "new content"}}
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{**action, "status": "pending", "result": None}]})
    result = install_artifact.install_action(action, "ABC", staging_root)
    assert result.get("collision") is True
    proposal = staging_lib.read_proposal("ABC", staging_root)
    assert proposal["actions"][0]["status"] == "pending"  # not yet decided


def test_install_action_replace_backs_up_original(tmp_path, monkeypatch):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path / "rules")
    monkeypatch.setattr(install_artifact, "ALLOWED_ROOTS", [tmp_path / "rules"])
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "dup.md").write_text("old content")
    staging_root = tmp_path / "staging"
    action = {"id": "a1", "type": "rule", "payload": {"topic": "dup", "content": "new content"}}
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{**action, "status": "pending", "result": None}]})
    result = install_artifact.install_action(action, "ABC", staging_root, resolution="replace")
    assert result["ok"] is True
    assert (tmp_path / "rules" / "dup.md").read_text() == "new content"
    assert (staging_root / "ABC" / "replaced-dup.md").read_text() == "old content"


def test_install_action_keep_both_suffixes_target(tmp_path, monkeypatch):
    monkeypatch.setattr(rule, "RULES_ROOT", tmp_path / "rules")
    monkeypatch.setattr(install_artifact, "ALLOWED_ROOTS", [tmp_path / "rules"])
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "dup.md").write_text("old content")
    staging_root = tmp_path / "staging"
    action = {"id": "a1", "type": "rule", "payload": {"topic": "dup", "content": "new content"}}
    staging_lib.write_proposal("ABC", staging_root, {
        "shortcode": "ABC", "actions": [{**action, "status": "pending", "result": None}]})
    result = install_artifact.install_action(action, "ABC", staging_root, resolution="keep_both")
    assert result["ok"] is True
    assert (tmp_path / "rules" / "dup.md").read_text() == "old content"
    assert (tmp_path / "rules" / "dup-2.md").read_text() == "new content"


def test_extract_urls_dedupes_and_strips_punctuation():
    text = "check https://zoxide.dev/. also https://zoxide.dev/ and (https://other.com/x)!"
    assert resolve_tools.extract_urls(text) == ["https://zoxide.dev/", "https://other.com/x"]


def test_extract_urls_empty_text():
    assert resolve_tools.extract_urls("") == []
    assert resolve_tools.extract_urls(None) == []


def test_is_self_referential():
    assert resolve_tools.is_self_referential("https://www.instagram.com/p/ABC/") is True
    assert resolve_tools.is_self_referential("https://github.com/x/y") is False


def test_tier1_creator_replies_filters_by_author():
    comments = [
        {"author": "networkchuck", "text": "Get it at https://zoxide.dev/"},
        {"author": "randomfan", "text": "also try https://not-the-creator.com/"},
    ]
    assert resolve_tools.tier1_creator_replies(comments, "networkchuck") == ["https://zoxide.dev/"]


def test_tier1_creator_replies_excludes_self_referential():
    comments = [{"author": "networkchuck", "text": "see https://instagram.com/p/other/"}]
    assert resolve_tools.tier1_creator_replies(comments, "networkchuck") == []


def test_tier1_creator_replies_no_channel_returns_empty():
    comments = [{"author": "networkchuck", "text": "https://x.com"}]
    assert resolve_tools.tier1_creator_replies(comments, None) == []


def test_tier2_caption_and_comments_combines_both():
    description = "check out https://zoxide.dev/"
    comments = [{"author": "randomfan", "text": "also https://other.com"}]
    urls = resolve_tools.tier2_caption_and_comments(description, comments)
    assert urls == ["https://zoxide.dev/", "https://other.com"]


def test_resolve_pure_tiers_resolved_from_tier1():
    comments = [{"author": "chuck", "text": "https://zoxide.dev/"}]
    result = resolve_tools.resolve_pure_tiers("caption", comments, "chuck")
    assert result == {"status": "resolved", "tier": 1, "candidates": ["https://zoxide.dev/"]}


def test_resolve_pure_tiers_uncertain_multiple_candidates():
    comments = [{"author": "chuck", "text": "https://a.com https://b.com"}]
    result = resolve_tools.resolve_pure_tiers("caption", comments, "chuck")
    assert result["status"] == "uncertain"
    assert result["tier"] == 1


def test_resolve_pure_tiers_falls_back_to_tier2():
    comments = [{"author": "chuck", "text": "no links here"}, {"author": "fan", "text": "https://x.com"}]
    result = resolve_tools.resolve_pure_tiers("caption", comments, "chuck")
    assert result == {"status": "resolved", "tier": 2, "candidates": ["https://x.com"]}


def test_resolve_pure_tiers_not_found():
    result = resolve_tools.resolve_pure_tiers("no links here", [{"author": "fan", "text": "none either"}], "chuck")
    assert result == {"status": "not_found", "tier": None, "candidates": []}


def test_sample_frame_timestamps_short_clip():
    assert resolve_tools.sample_frame_timestamps(0) == [0.0]


def test_sample_frame_timestamps_respects_max_frames():
    timestamps = resolve_tools.sample_frame_timestamps(400, interval_s=5, max_frames=8)
    assert len(timestamps) == 8
    assert timestamps[0] == 0.0


def test_sample_frame_timestamps_short_duration_fewer_frames():
    timestamps = resolve_tools.sample_frame_timestamps(12, interval_s=5, max_frames=8)
    assert len(timestamps) == 3  # 12 // 5 + 1


def test_resolve_via_web_search_high_confidence_resolved():
    text = '{"tool_name": "zoxide", "url": "https://zoxide.dev/", "confidence": "high", "sources": ["a"]}'
    result = resolve_tools.resolve_via_web_search(text, agents_lib.extract_json)
    assert result == {"status": "resolved", "tool_name": "zoxide", "url": "https://zoxide.dev/", "sources": ["a"]}


def test_resolve_via_web_search_low_confidence_uncertain():
    text = '{"tool_name": "maybe-x", "url": null, "confidence": "low", "sources": []}'
    result = resolve_tools.resolve_via_web_search(text, agents_lib.extract_json)
    assert result["status"] == "uncertain"


def test_resolve_via_web_search_no_tool_name_unresolved():
    text = '{"tool_name": null, "url": null, "confidence": "low", "sources": []}'
    result = resolve_tools.resolve_via_web_search(text, agents_lib.extract_json)
    assert result["status"] == "unresolved"


def test_resolve_via_web_search_unparseable_unresolved():
    result = resolve_tools.resolve_via_web_search("not json at all", agents_lib.extract_json)
    assert result["status"] == "unresolved"


def test_load_post_context_missing_extracted_raises(tmp_path):
    try:
        interpret.load_post_context("NOPE", tmp_path, tmp_path)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_load_post_context_reads_video_post(tmp_path):
    extracted_root = tmp_path / "extracted"
    media_root = tmp_path / "media"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "transcript", "media_type": "audio"}))
    media_dir = media_root / "ABC"
    media_dir.mkdir(parents=True)
    (media_dir / "ABC.info.json").write_text(json.dumps({
        "description": "caption", "comments": [{"author": "x", "text": "y"}],
        "channel": "networkchuck", "duration": 120}))
    context = interpret.load_post_context("ABC", extracted_root, media_root)
    assert context["description"] == "caption"
    assert context["channel"] == "networkchuck"
    assert context["duration_s"] == 120


def test_load_post_context_reads_image_post(tmp_path):
    extracted_root = tmp_path / "extracted"
    media_root = tmp_path / "media"
    extracted_root.mkdir()
    (extracted_root / "IMG.json").write_text(json.dumps({"text": "ocr text", "media_type": "image"}))
    media_dir = media_root / "IMG"
    media_dir.mkdir(parents=True)
    (media_dir / "IMG_01.jpg.json").write_text(json.dumps({"description": "caption", "username": "ynetgram"}))
    context = interpret.load_post_context("IMG", extracted_root, media_root)
    assert context["description"] == "caption"
    assert context["channel"] == "ynetgram"


def test_interpret_post_happy_path_single_agent_call(tmp_path, monkeypatch):
    extracted_root, media_root, staging_root = tmp_path / "e", tmp_path / "m", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "install zoxide", "media_type": "audio"}))
    (media_root / "ABC").mkdir(parents=True)
    (media_root / "ABC" / "ABC.info.json").write_text(json.dumps({
        "description": "zoxide", "comments": [], "channel": "chuck", "duration": 60}))

    fake_response = json.dumps({
        "summary": "zoxide, a smarter cd", "unnamed_tool": {"present": False, "description": ""},
        "actions": [{"type": "package_install", "confidence": 0.9,
                       "payload": {"manager": "cargo", "package": "zoxide", "command": "cargo install zoxide"}}],
    })
    monkeypatch.setattr(agents_lib, "run_agent",
                          lambda *a, **kw: {"text": fake_response, "backend": "claude", "model": None,
                                             "duration_s": 1.0, "ok": True, "error": None})

    proposal = interpret.interpret_post("ABC", media_root, extracted_root, staging_root, tmp_path / "logs")
    # triage + the one per-post explanation call for the pending action
    assert proposal["agent_calls"] == 2
    assert proposal["actions"][0]["type"] == "package_install"
    assert proposal["actions"][0]["risk"] == "exec"
    assert staging_lib.read_proposal("ABC", staging_root) == proposal


def test_interpret_post_persists_evidence_for_install_actions(tmp_path, monkeypatch):
    extracted_root, media_root, staging_root = tmp_path / "e", tmp_path / "m", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "two UI skills", "media_type": "video"}))
    (media_root / "ABC").mkdir(parents=True)
    (media_root / "ABC" / "ABC.info.json").write_text(json.dumps({
        "description": "", "channel": "c", "duration": 60,
        "comments": [{"author": "dev", "text": "npx skills add dev/tool-kit"}]}))
    response = json.dumps({"summary": "s", "unnamed_tool": {"present": False, "description": ""},
                           "actions": [{"type": "skill_install", "confidence": 0.8,
                                        "payload": {"name": "tool-kit", "source": "dev/tool-kit"}}]})
    monkeypatch.setattr(agents_lib, "run_agent",
                        lambda *a, **kw: {"text": response, "backend": "claude", "model": None,
                                          "duration_s": 1.0, "ok": True, "error": None})
    fetchers = {"directory": lambda name, source: {"status": "ok", "listed": False, "source": source,
                                                   "installs": None, "dominant": False},
                "github": lambda repo: {"status": "ok", "stars": 3, "contributors": 1, "licence": "MIT",
                                        "archived": False, "pushed_at": None},
                "readme": lambda repo: {"status": "ok", "head": "tool kit"}}
    proposal = interpret.interpret_post("ABC", media_root, extracted_root, staging_root, tmp_path / "logs",
                                        enrich_fetchers=fetchers)
    action = staging_lib.read_proposal("ABC", staging_root)["actions"][0]
    assert action["evidence"]["provenance"]["comment_author"] == "dev"
    assert action["flags"] == ["not_in_video", "self_promoted_in_comment", "low_adoption", "solo_author",
                               "not_in_directory"]
    assert proposal["actions"][0] == action


def test_interpret_post_agent_failure_raises_and_writes_error(tmp_path, monkeypatch):
    extracted_root, media_root, staging_root = tmp_path / "e", tmp_path / "m", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "x", "media_type": "audio"}))
    (media_root / "ABC").mkdir(parents=True)
    monkeypatch.setattr(agents_lib, "run_agent",
                          lambda *a, **kw: {"text": "", "backend": "claude", "model": None,
                                             "duration_s": 1.0, "ok": False, "error": "boom"})
    try:
        interpret.interpret_post("ABC", media_root, extracted_root, staging_root, tmp_path / "logs")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert (staging_root / "ABC" / "agent-error.txt").exists()


def test_interpret_post_invalid_action_list_raises(tmp_path, monkeypatch):
    extracted_root, media_root, staging_root = tmp_path / "e", tmp_path / "m", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "x", "media_type": "audio"}))
    (media_root / "ABC").mkdir(parents=True)
    bad_response = json.dumps({"summary": "s", "unnamed_tool": {"present": False, "description": ""},
                                 "actions": [{"type": "nonexistent_type", "confidence": 0.5, "payload": {}}]})
    monkeypatch.setattr(agents_lib, "run_agent",
                          lambda *a, **kw: {"text": bad_response, "backend": "claude", "model": None,
                                             "duration_s": 1.0, "ok": True, "error": None})
    try:
        interpret.interpret_post("ABC", media_root, extracted_root, staging_root, tmp_path / "logs")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_interpret_post_auto_discard_eligible_on_backfill(tmp_path, monkeypatch):
    extracted_root, media_root, staging_root, logs_root = tmp_path / "e", tmp_path / "m", tmp_path / "s", tmp_path / "l"
    extracted_root.mkdir()
    (extracted_root / "ABC.json").write_text(json.dumps({"text": "personal essay", "media_type": "audio"}))
    (media_root / "ABC").mkdir(parents=True)
    response = json.dumps({"summary": "personal essay, not tech", "unnamed_tool": {"present": False, "description": ""},
                             "actions": [{"type": "discard", "confidence": 0.95, "payload": {"reason": "not tech"}}]})
    monkeypatch.setattr(agents_lib, "run_agent",
                          lambda *a, **kw: {"text": response, "backend": "claude", "model": None,
                                             "duration_s": 1.0, "ok": True, "error": None})
    monkeypatch.setattr(discard, "DISCARDED_LOG", logs_root / "discarded.jsonl")
    proposal = interpret.interpret_post("ABC", media_root, extracted_root, staging_root, logs_root, origin="backfill")
    assert proposal["actions"][0]["status"] == "auto_discarded"
    assert proposal["status"] == "decided"
    assert (logs_root / "discarded.jsonl").exists()


def test_backfill_sweep_skips_already_staged(tmp_path):
    extracted_root, staging_root = tmp_path / "e", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "DONE.json").write_text("{}")
    (extracted_root / "NEW.json").write_text("{}")
    staging_lib.write_proposal("DONE", staging_root, {"shortcode": "DONE", "actions": []})

    calls = []
    def fake_interpret_post(shortcode, *a, **kw):
        calls.append(shortcode)
        return {"shortcode": shortcode, "actions": []}

    results = interpret.backfill_sweep(tmp_path / "m", extracted_root, staging_root, tmp_path / "l",
                                          interpret_post_fn=fake_interpret_post)
    assert calls == ["NEW"]
    assert [p["shortcode"] for p in results["interpreted"]] == ["NEW"]


def test_backfill_sweep_is_rerunnable(tmp_path):
    extracted_root, staging_root = tmp_path / "e", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "A.json").write_text("{}")

    def fake_interpret_post(shortcode, *a, **kw):
        staging_lib.write_proposal(shortcode, staging_root, {"shortcode": shortcode, "actions": []})
        return {"shortcode": shortcode, "actions": []}

    first = interpret.backfill_sweep(tmp_path / "m", extracted_root, staging_root, tmp_path / "l",
                                        interpret_post_fn=fake_interpret_post)
    second = interpret.backfill_sweep(tmp_path / "m", extracted_root, staging_root, tmp_path / "l",
                                         interpret_post_fn=fake_interpret_post)
    assert len(first["interpreted"]) == 1
    assert len(second["interpreted"]) == 0


def test_backfill_sweep_sends_one_digest_not_per_post(tmp_path):
    extracted_root, staging_root = tmp_path / "e", tmp_path / "s"
    extracted_root.mkdir()
    (extracted_root / "A.json").write_text("{}")
    (extracted_root / "B.json").write_text("{}")

    def fake_interpret_post(shortcode, *a, **kw):
        return {"shortcode": shortcode, "actions": [{"type": "reference", "status": "pending"}]}

    sent = []
    interpret.backfill_sweep(tmp_path / "m", extracted_root, staging_root, tmp_path / "l",
                                notify_fn=sent.append, interpret_post_fn=fake_interpret_post)
    assert len(sent) == 1
    assert "2 proposals" in sent[0]


# --- skill_install (Phase 4b, Session 4 backlog item #1) -----------------
# The pilot found this gap in all three states at once: 4 explicit
# `unsupported: skill_install`, 3 force-fits into package_install, and 7
# force-fits into git_repo (SESSION-4-phase4b-handlers.md "Ranked backlog").

def test_skill_install_is_registered_with_exec_risk():
    assert "skill_install" in registry.HANDLER_MODULE_NAMES
    assert skill_install.TYPE == "skill_install"
    # `npx skills add` downloads and executes a third-party CLI -- exec, not config.
    assert skill_install.RISK == "exec"


def test_skill_install_accepts_bare_owner_repo():
    reg = {}
    registry.register(skill_install, registry_dict=reg)
    ok, err = registry.validate_payload(
        "skill_install", {"name": "impeccable", "source": "pbakaus/impeccable"}, registry_dict=reg)
    assert ok, err


def test_skill_install_rejects_url_and_bare_name_sources():
    """Backlog proof #3: the force-fit payloads were internally inconsistent --
    `package: "skills"` (the CLI) in one post, `package: "pbakaus/impeccable"`
    (the skill) in another. The pattern pins one canonical form so any dedup
    keyed on `source` matches."""
    reg = {}
    registry.register(skill_install, registry_dict=reg)
    for bad in ["https://github.com/pbakaus/impeccable", "impeccable", "", "owner/repo extra"]:
        ok, err = registry.validate_payload(
            "skill_install", {"name": "x", "source": bad}, registry_dict=reg)
        assert not ok, f"source {bad!r} should be rejected"


def test_skill_install_target_path_is_the_canonical_repo():
    """Backlog proof #2: `npx skills add` installs relative to cwd, and
    package_install.install() passes no cwd=, so running it from scripts/
    created scripts/.claude/skills/ inside this repo and still returned ok.
    The destination is now pinned to the one Git-versioned skill repo."""
    target = skill_install.target_path({"name": "Impeccable", "source": "pbakaus/impeccable"})
    assert target.is_absolute()
    assert target == Path.home() / "agent-skills" / "skills" / "impeccable"


def test_skill_install_preview_shows_command_and_destination():
    payload = {"name": "impeccable", "source": "pbakaus/impeccable"}
    preview = skill_install.preview(payload)
    assert "npx --yes skills add pbakaus/impeccable" in preview  # --yes: worker runs non-interactive
    assert "--copy" in preview  # a symlink into npx's cache can't be committed
    assert str(skill_install.SKILLS_ROOT) in preview
    assert "agent-skills sync" in preview


def test_skill_install_collides_when_already_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    payload = {"name": "impeccable", "source": "pbakaus/impeccable"}
    assert skill_install.collides(payload) is None
    (tmp_path / "skills" / "impeccable").mkdir(parents=True)
    assert skill_install.collides(payload) == tmp_path / "skills" / "impeccable"


def _fake_add(*names, returncode=0):
    """Stand-in for `npx skills add --copy --agent claude-code`, which copies
    each skill in the repo to <cwd>/.claude/skills/<name>/."""
    def fake_run(cmd, **kwargs):
        staged = Path(kwargs["cwd"]) / ".claude" / "skills"
        for name in names:
            (staged / name).mkdir(parents=True)
            (staged / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        return subprocess.CompletedProcess(cmd, returncode, stdout="0 alerts", stderr="")
    return fake_run


def test_skill_install_adopts_into_the_canonical_repo(monkeypatch, tmp_path):
    """The whole point of the handler: never inherit the worker's cwd, and
    never leave a third-party skill in an agent-local directory."""
    seen = {}
    fake_add = _fake_add("impeccable", "polish")

    def recording_run(cmd, **kwargs):
        seen["cmd"], seen["cwd"] = cmd, kwargs.get("cwd")
        return fake_add(cmd, **kwargs)

    monkeypatch.setattr(skill_install.subprocess, "run", recording_run)
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    monkeypatch.setattr(_skills_root, "sync", lambda: None)
    result = skill_install.install({"name": "impeccable", "source": "pbakaus/impeccable"}, target=None)

    assert result["ok"] is True
    assert Path(seen["cwd"]).is_absolute() and seen["cwd"] != str(Path.cwd())
    assert seen["cmd"] == ["npx", "--yes", "skills", "add", "pbakaus/impeccable",
                            "--copy", "--skill", "*", "--agent", "claude-code", "--yes"]
    # Both skills the repo ships are adopted; the approved name is the path.
    assert (tmp_path / "skills" / "impeccable" / "SKILL.md").is_file()
    assert (tmp_path / "skills" / "polish" / "SKILL.md").is_file()
    assert result["path"] == str(tmp_path / "skills" / "impeccable")
    assert "impeccable, polish" in result["note"]
    # The staging directory is a temp dir and does not outlive the install.
    assert not Path(seen["cwd"]).exists()


def test_skill_install_never_overwrites_a_canonical_skill(monkeypatch, tmp_path):
    """Replacing a skill is the approval flow's decision (Replace/Keep both),
    not a side effect of installing a repo that happens to ship that name."""
    monkeypatch.setattr(skill_install.subprocess, "run", _fake_add("impeccable", "polish"))
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    monkeypatch.setattr(_skills_root, "sync", lambda: None)
    (tmp_path / "skills" / "polish").mkdir(parents=True)
    (tmp_path / "skills" / "polish" / "SKILL.md").write_text("mine")

    result = skill_install.install({"name": "impeccable", "source": "pbakaus/impeccable"}, target=None)
    assert result["ok"] is True
    assert (tmp_path / "skills" / "polish" / "SKILL.md").read_text() == "mine"
    assert "already canonical, left untouched: polish" in result["note"]


def test_skill_install_fails_when_exit_zero_leaves_nothing(monkeypatch, tmp_path):
    """PLAN.md open item #10: a proposal marked installed with path:null and
    no artifact anywhere on disk. Exit 0 is not evidence of an install."""
    monkeypatch.setattr(skill_install.subprocess, "run", _fake_add())
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    result = skill_install.install({"name": "ghost", "source": "nobody/ghost"}, target=None)
    assert result["ok"] is False
    assert result["path"] is None
    assert "no SKILL.md" in result["error"]


def test_skill_install_records_failure(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    result = skill_install.install({"name": "nope", "source": "nobody/nope"}, target=None)
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "not found" in result["output"]


def test_skill_install_appears_in_prompt_catalogue():
    """Adding a handler must teach the classifier it exists -- no prompt is
    hand-edited (registry.generate_prompt_catalogue docstring)."""
    reg = {}
    registry.register(skill_install, registry_dict=reg)
    catalogue = registry.generate_prompt_catalogue(registry_dict=reg)
    assert "`skill_install`" in catalogue
    assert "source" in catalogue


# --- installing a skill by name alone (skills.sh directory) ----------------
# Four `unsupported: skill_install` rationales said the same thing: "the skill
# is named, but the post provides no source URL ... and the registry has no
# action that resolves skill names through a skill directory." skills.sh has
# such a directory, but it is thick with forks -- a bare exact-name search for
# "impeccable" matches 73 distinct owners. Install counts separate the original
# from the copies by three orders of magnitude, so resolution turns on
# DOMINANCE, not on being the only match. Fixtures below are trimmed real
# responses from https://skills.sh/api/search.

_IMPECCABLE = {"searchType": "fuzzy", "skills": [
    {"skillId": "impeccable", "name": "impeccable", "source": "pbakaus/impeccable", "installs": 266695},
    {"skillId": "polish", "name": "polish", "source": "pbakaus/impeccable", "installs": 87227},
    {"skillId": "impeccable", "name": "impeccable", "source": "bergside/awesome-design-skills", "installs": 1015},
    {"skillId": "impeccable", "name": "impeccable", "source": "boraoztunc/skills", "installs": 197},
]}
_TASTE = {"searchType": "fuzzy", "skills": [
    {"skillId": "design-taste-frontend", "name": "design-taste-frontend",
     "source": "leonxlnx/taste-skill", "installs": 455836},
    {"skillId": "taste", "name": "taste", "source": "affaan-m/ecc", "installs": 3022},
    {"skillId": "taste-skill", "name": "taste-skill", "source": "nexu-io/open-design", "installs": 815},
]}
_CLAUDE_VIDEO = {"searchType": "fuzzy", "skills": [
    {"skillId": "claude-video", "name": "claude-video", "source": "agricidaniel/claude-video", "installs": 13},
    {"skillId": "claude-video", "name": "claude-video", "source": "opheliabm/claude-videoedit", "installs": 6},
    {"skillId": "claude-video", "name": "claude-video", "source": "zeryamkill/claude-video", "installs": 2},
]}
_CRUCIBLE = {"searchType": "fuzzy", "skills": [
    {"skillId": "crucible", "name": "crucible", "source": "geekkingcloud/skills", "installs": 9},
    {"skillId": "crucible", "name": "crucible", "source": "ryanelian/crucible-agent-skill", "installs": 8},
]}


def _fixed_search(response):
    calls = []

    def search(query):
        calls.append(query)
        return response

    search.calls = calls
    return search


def test_directory_query_strips_skill_and_version_noise():
    """Searching the raw caption name loses the skill: skills.sh answers
    "Taste skill 2.0" with searchType=semantic and unrelated hits, while
    "taste" fuzzy-matches leonxlnx/taste-skill (455k installs)."""
    assert _skill_directory.normalize("Taste skill 2.0") == "taste"
    assert _skill_directory.normalize("last30days-skill") == "last30days"
    assert _skill_directory.normalize("Impeccable") == "impeccable"
    assert _skill_directory.normalize("Claude Video") == "claude-video"


def test_directory_resolves_a_dominant_exact_match():
    search = _fixed_search(_IMPECCABLE)
    result = _skill_directory.resolve("Impeccable", search_fn=search)
    assert search.calls == ["impeccable"]
    assert result["status"] == "resolved"
    assert result["source"] == "pbakaus/impeccable"
    assert result["skill_id"] == "impeccable"
    assert result["installs"] == 266695


def test_directory_resolves_when_the_repo_not_the_skill_carries_the_name():
    """The post said "Taste skill 2.0"; the directory calls the skill
    design-taste-frontend and only the repo is named taste-skill."""
    result = _skill_directory.resolve("Taste skill 2.0", search_fn=_fixed_search(_TASTE))
    assert result["status"] == "resolved"
    assert result["source"] == "leonxlnx/taste-skill"


def test_directory_refuses_a_name_no_candidate_dominates():
    """Three owners publish a claude-video skill with 13, 6 and 2 installs.
    Nothing here is the thing the post demoed -- guessing would install a
    stranger's code under an exec-risk action."""
    result = _skill_directory.resolve("Claude Video", search_fn=_fixed_search(_CLAUDE_VIDEO))
    assert result["status"] == "uncertain"
    assert result["source"] is None
    assert "agricidaniel/claude-video" in " ".join(result["candidates"])


def test_directory_refuses_a_tie():
    result = _skill_directory.resolve("Crucible", search_fn=_fixed_search(_CRUCIBLE))
    assert result["status"] == "uncertain"
    assert result["source"] is None


def test_directory_ignores_sources_that_are_not_owner_repo():
    """skills.sh also lists aggregator hosts (smithery.ai, wai-stacks.vercel.app)
    as a `source`. `npx skills add` cannot take those, and skill_install's
    schema rejects them, so they must never win the ranking."""
    response = {"searchType": "fuzzy", "skills": [
        {"skillId": "widget", "name": "widget", "source": "smithery.ai", "installs": 900000},
        {"skillId": "widget", "name": "widget", "source": "realowner/widget", "installs": 40000},
    ]}
    result = _skill_directory.resolve("widget", search_fn=_fixed_search(response))
    assert result["status"] == "resolved"
    assert result["source"] == "realowner/widget"


def test_directory_reports_not_found_when_the_name_is_absent():
    result = _skill_directory.resolve("zzzznotarealskill",
                                       search_fn=_fixed_search({"searchType": "fuzzy", "skills": []}))
    assert result["status"] == "not_found"
    assert result["candidates"] == []


def test_directory_survives_a_dead_search():
    """A directory outage must degrade to "I could not resolve it", never to an
    exception inside an approval tap."""
    def boom(query):
        raise OSError("connection refused")

    result = _skill_directory.resolve("impeccable", search_fn=boom)
    assert result["status"] == "not_found"
    assert "connection refused" in (result["error"] or "")


def test_skill_install_accepts_a_name_with_no_source():
    """The whole point: a post that names a skill and gives no URL is now
    installable, so the classifier stops emitting `unsupported` for it."""
    reg = {}
    registry.register(skill_install, registry_dict=reg)
    ok, err = registry.validate_payload("skill_install", {"name": "Impeccable"}, registry_dict=reg)
    assert ok, err


def test_skill_install_catalogue_advertises_the_name_only_payload():
    reg = {}
    registry.register(skill_install, registry_dict=reg)
    line = registry.generate_prompt_catalogue(registry_dict=reg)
    # `name` alone is enough; `source?` stays advertised so a post that DOES
    # link a repo still gets an exact identifier instead of a lookup.
    assert "payload: {name, source?}" in line
    assert "skills.sh" in line, "the classifier must be told the directory exists"


def test_skill_install_resolves_a_bare_name_before_running_the_cli(monkeypatch, tmp_path):
    seen = {}
    fake_add = _fake_add("impeccable")

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return fake_add(cmd, **kwargs)

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    monkeypatch.setattr(_skills_root, "sync", lambda: None)
    monkeypatch.setattr(skill_install._skill_directory, "search", lambda query: _IMPECCABLE)
    result = skill_install.install({"name": "Impeccable"}, target=None)
    assert result["ok"] is True
    assert seen["cmd"][:5] == ["npx", "--yes", "skills", "add", "pbakaus/impeccable"]
    # the approval message must say what the name was resolved to: an exec-risk
    # install that reports only "installed" hides which stranger's repo ran.
    assert "pbakaus/impeccable" in result["note"]
    assert "266695" in result["note"].replace(",", "")


def test_skill_install_refuses_to_guess_an_unresolved_name(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise AssertionError("must not run the installer for an unresolved name")

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)
    monkeypatch.setattr(skill_install._skill_directory, "search", lambda query: _CLAUDE_VIDEO)
    result = skill_install.install({"name": "Claude Video"}, target=None)
    assert result["ok"] is False
    assert "agricidaniel/claude-video" in result["error"]


def test_skill_install_prefers_an_explicit_source_over_the_directory(monkeypatch, tmp_path):
    def no_search(query):
        raise AssertionError("an explicit source must not be second-guessed")

    monkeypatch.setattr(skill_install._skill_directory, "search", no_search)
    monkeypatch.setattr(skill_install.subprocess, "run", _fake_add("impeccable"))
    monkeypatch.setattr(skill_install, "SKILLS_ROOT", tmp_path / "skills")
    monkeypatch.setattr(_skills_root, "sync", lambda: None)
    result = skill_install.install({"name": "Impeccable", "source": "pbakaus/impeccable"}, target=None)
    assert result["ok"] is True


def test_skill_install_describe_says_where_a_bare_name_will_come_from():
    assert "skills.sh" in skill_install.describe({"name": "Impeccable"})
    assert "pbakaus/impeccable" in skill_install.describe(
        {"name": "Impeccable", "source": "pbakaus/impeccable"})


def test_format_proposal_message_always_shows_the_shortcode():
    """Found live 2026-09-11: the shortcode was only a *fallback* for a missing
    summary, so every real proposal rendered without its id. Dan could read
    `/pending` on his phone but had no way to tell which message was the
    `DbJvV3BpnO6` referred to in chat, the docs and the logs. It must be at a
    fixed position — first thing on the first line — so a column of proposals
    can be scanned without reading each summary to the end."""
    proposal = {
        "shortcode": "DbJvV3BpnO6", "summary": "Install the Impeccable and Taste skills",
        "resolver": {"status": "not_found"},
        "actions": [{"id": "a1", "type": "skill_install", "risk": "exec",
                       "payload": {"name": "impeccable"}}],
    }
    text = staging_lib.format_proposal_message(proposal, {"skill_install": lambda p: f"install {p['name']}"})
    assert text.splitlines()[0].startswith("DbJvV3BpnO6")
    assert "Install the Impeccable and Taste skills" in text
