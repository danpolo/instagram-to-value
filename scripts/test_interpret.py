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
