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
