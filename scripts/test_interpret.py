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
import registry
import reference
import note
import discard
import unsupported
import rule
import skill as skill_action
import package_install
import shell_snippet
import git_repo
import calendar_event


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
