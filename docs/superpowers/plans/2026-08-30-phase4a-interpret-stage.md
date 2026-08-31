# Phase 4a — Interpret Stage + Action Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A post that has been fetched and extracted produces a staged proposal — an ordered list of concrete, individually-approvable actions — summarised to Telegram (immediately for a hand-sent URL, digested for a backfill drain), where approving installs the actions and rejecting logs why.

**Architecture:** A 4th filesystem-coupled stage, `interpret.py`, invoked exactly like `fetch.py`/`extract.py`. Fat Python, thin agent: pure Python owns URL extraction (resolver tiers 1-2), path-allowlist enforcement, schema validation and staging state; a pluggable headless agent backend (`claude -p` default, `codex exec` second) is invoked only for classification and (rarely) web search. An extensible action registry (`scripts/actions/`) replaces PLAN.md §5's six-type taxonomy — each handler is an independent module sharing one contract, so `registry.py` can both validate the agent's output and generate the agent's own prompt catalogue from the same source.

**Tech Stack:** `python-telegram-bot` v22.7 (already installed) for the inline-button approval flow; stdlib `subprocess`/`json`/`argparse` throughout, matching `fetch.py`/`extract.py` conventions; headless `claude -p` (2.1.251) and `codex exec` (0.151.0), both verified installed; `pytest` for pure-logic unit tests, no mocking of the agent CLIs or Telegram.

**Spec:** `docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md` (442 lines, commit `15f8d9f`) — the whole design and source of truth. It supersedes `PLAN.md` §5 and records five deliberate deviations from §4/§2 (see spec's "Design deviations from PLAN.md"). Also: `PLAN.md` §7 Phase 4, §4, §6, §8 risks #1/#4; `docs/superpowers/handoffs/SESSION-2-phase4a-interpret-stage.md` (this session's mandate).

## Global Constraints

- Fail loud on a genuine failure; a failed agent call or invalid action list never yields a fabricated proposal (spec's "Error handling") — one retry with a "JSON only" nudge, then the job/sweep entry fails and the raw output is written to `staging/<shortcode>/agent-error.txt`.
- Exec-tier actions run on approval like any other action (Dan's decision, 2026-08-30) — the only mitigation is `preview()` rendering the **literal command, verbatim**, before the approval tap. This is a tested requirement on every exec-tier handler, not a convention.
- Every install-time path is validated against the allowlist in `install_artifact.py` before any write — traversal and symlink-escape both fail the action rather than being silently relocated.
- `origin` (`"telegram"` | `"backfill"`) drives exactly two things, stated identically everywhere they appear: notification style (immediate summary vs. one digest per drain) and auto-discard permission (backfill only, never a hand-sent URL).
- Reuse, don't duplicate: shortcode/media-root conventions from `fetch.py`/`extract.py`, the `note()`-wrapped-notify pattern and `--once` CLI shape from `worker.py`, the `write_job`/`move_job` state-machine shape from `jobs_lib.py`.
- `staging/`, `logs/`, `config/` are new top-level dirs. `staging/` is already gitignored; `logs/` needs adding (Task 3); `config/agent.json` is tracked (small, non-secret runtime setting, same class as `.claude/settings.json`).
- Coordination: the 49-job backfill drain (fetch+extract+discover only — interpret.py doesn't exist yet) runs as a background task throughout this session. Never run `worker.py` against the live queue directly; all interpret.py verification calls it standalone against posts already in `extracted/`. `scripts/telegram_bot.py` is not currently running (no PID found this session) — Task 11's live verification starts it fresh.

---

### Task 1: `scripts/agents_lib.py` — pluggable agent backend

**Files:**
- Create: `scripts/agents_lib.py`
- Create: `config/agent.json`
- Test: `scripts/test_interpret.py` (new file, started here)

**Interfaces:**
- Produces: `build_cmd(backend, prompt, *, schema_path=None, allow_search=False, model=None, output_path=None) -> list[str]` (pure), `run_agent(prompt, backend=None, model=None, allow_search=False, schema=None, timeout=300) -> dict` (impure; `{text, backend, model, duration_s, ok, error}`), `extract_json(text) -> dict` (raises `ValueError` if nothing parses), `get_backend(config_path=...) -> str`, `get_model(config_path=...) -> str|None`, `set_backend(name, config_path=..., model=None) -> None` (raises `ValueError` on an unknown name).

- [ ] **Step 1: Write `config/agent.json`**

```json
{
  "backend": "claude",
  "model": null
}
```

- [ ] **Step 2: Write `scripts/agents_lib.py`**

```python
#!/usr/bin/env python3
"""Pluggable agent backend for Phase 4's interpret stage (design spec
Component 1). Both backends are headless CLIs already installed and verified
on this machine (claude 2.1.251, codex-cli 0.151.0). The shared contract is
"return JSON matching this schema, validated Python-side for both" -- codex's
--output-schema is a second belt, not a replacement for that validation
(design spec: "Codex can be forced to a JSON shape; Claude can only be
asked").

Usage (as a library):
    from agents_lib import run_agent, extract_json, get_backend
    result = run_agent("prompt text", backend=get_backend())
    if result["ok"]:
        data = extract_json(result["text"])

CLI (manual smoke test only):
    python3 scripts/agents_lib.py "say hello" [--backend claude|codex] [--search]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "agent.json"
DEFAULT_BACKEND = "claude"
VALID_BACKENDS = ("claude", "codex")


def get_backend(config_path=DEFAULT_CONFIG_PATH):
    """Read the current backend name from config/agent.json. A missing or
    malformed config file must never crash the pipeline -- defaults to
    'claude' silently."""
    config_path = Path(config_path)
    if not config_path.exists():
        return DEFAULT_BACKEND
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return DEFAULT_BACKEND
    backend = data.get("backend", DEFAULT_BACKEND)
    return backend if backend in VALID_BACKENDS else DEFAULT_BACKEND


def get_model(config_path=DEFAULT_CONFIG_PATH):
    config_path = Path(config_path)
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("model")


def set_backend(name, config_path=DEFAULT_CONFIG_PATH, model=None):
    """Write config/agent.json. Raises ValueError on an unknown backend name
    -- telegram_bot.py's /agent command relies on this to reject a typo
    instead of silently writing garbage that get_backend() would then also
    silently ignore."""
    if name not in VALID_BACKENDS:
        raise ValueError(f"unknown backend {name!r}, must be one of {VALID_BACKENDS}")
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_model = get_model(config_path) if model is None else model
    config_path.write_text(json.dumps({"backend": name, "model": existing_model}, indent=2))


def build_cmd(backend, prompt, *, schema_path=None, allow_search=False, model=None, output_path=None):
    """Pure: the CLI invocation for one backend. No subprocess runs here --
    unit-testable without executing anything. `prompt` isn't embedded in the
    argv (both CLIs read it from stdin) -- avoids ARG_MAX and shell-quoting
    the multi-KB prompts this stage sends."""
    if backend == "claude":
        cmd = ["claude", "-p", "--output-format", "json", "--restricted"]
        if allow_search:
            cmd += ["--allowed-tools", "WebSearch"]
        if model:
            cmd += ["--model", model]
        return cmd
    if backend == "codex":
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        if allow_search:
            cmd += ["--search"]
        if schema_path:
            cmd += ["--output-schema", str(schema_path)]
        if output_path:
            cmd += ["-o", str(output_path)]
        if model:
            cmd += ["--model", model]
        return cmd
    raise ValueError(f"unknown backend {backend!r}, must be one of {VALID_BACKENDS}")


def extract_json(text):
    """Tolerant JSON extraction: a fenced ```json block wins if present, else
    the first balanced {...} object found by bracket counting (handles
    trailing prose a model appends after otherwise-valid JSON). Raises
    ValueError if nothing parses -- callers use that to trigger the
    one-retry-then-fail path (design spec's Error handling)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"no valid JSON object found in text: {text[:200]!r}")


def run_agent(prompt, backend=None, model=None, allow_search=False, schema=None, timeout=300):
    """The only impure function. Shells out to the chosen backend's headless
    CLI, prompt via stdin. Never raises on a subprocess failure -- returns
    {text, backend, model, duration_s, ok, error} and lets the caller decide
    what a failure means (design spec: "a failed call never yields a
    fabricated proposal"). When schema is given and backend=="codex", writes
    it to a temp file and passes --output-schema + -o so the final message is
    read back from a file rather than parsed out of exec's own stdout."""
    backend = backend or get_backend()
    model = model if model is not None else get_model()
    start = time.monotonic()
    tmp_dir = None
    try:
        schema_path = output_path = None
        if backend == "codex" and schema is not None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="interpret-codex-"))
            schema_path = tmp_dir / "schema.json"
            schema_path.write_text(json.dumps(schema))
            output_path = tmp_dir / "output.txt"

        cmd = build_cmd(backend, prompt, schema_path=schema_path, allow_search=allow_search,
                         model=model, output_path=output_path)
        try:
            result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"text": "", "backend": backend, "model": model,
                    "duration_s": time.monotonic() - start, "ok": False,
                    "error": f"timeout after {timeout}s"}

        duration = time.monotonic() - start
        if result.returncode != 0:
            return {"text": result.stdout, "backend": backend, "model": model,
                    "duration_s": duration, "ok": False,
                    "error": result.stderr[-2000:] or f"exit {result.returncode}"}

        if backend == "codex" and output_path is not None and output_path.exists():
            text = output_path.read_text()
        elif backend == "claude":
            try:
                envelope = json.loads(result.stdout)
                text = envelope.get("result", result.stdout)
            except json.JSONDecodeError:
                text = result.stdout  # some claude -p output isn't the JSON envelope; use raw stdout
        else:
            text = result.stdout

        return {"text": text, "backend": backend, "model": model,
                "duration_s": duration, "ok": True, "error": None}
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--backend", choices=VALID_BACKENDS, default=None)
    ap.add_argument("--search", action="store_true")
    args = ap.parse_args()
    result = run_agent(args.prompt, backend=args.backend, allow_search=args.search)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `scripts/test_interpret.py` (first slice)**

```python
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
```

- [ ] **Step 4: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/dan/projects/instagram-to-value
git add scripts/agents_lib.py scripts/test_interpret.py config/agent.json
git commit -m "feat: add agents_lib.py -- pluggable claude/codex backend for Phase 4a

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `scripts/actions/registry.py` — the action registry framework

**Files:**
- Create: `scripts/actions/registry.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Produces: `VALID_RISKS = ("inert", "config", "exec")`, `REGISTRY: dict[str, module]`, `register(handler, registry_dict=None)` (raises `AttributeError`/`ValueError` on a malformed handler), `get_handler(action_type, registry_dict=None)`, `validate_payload(action_type, payload, registry_dict=None) -> (bool, str|None)`, `validate_action(action, registry_dict=None) -> (bool, str|None)`, `validate_actions(actions, registry_dict=None) -> (bool, list[str])`, `generate_prompt_catalogue(registry_dict=None) -> str`, `load_all_handlers()`.

Every registry function takes an optional `registry_dict` (defaulting to the real module-level `REGISTRY`) purely so tests can exercise the framework against a throwaway dict instead of polluting the real one across test files that later `import` real handler modules (Tasks 3-6) which call `register()` at import time.

The handler contract every module in `scripts/actions/` must satisfy (an 8th member, `target_path`, is added beyond the design spec's six-member table — folded in here because centralizing path-allowlist enforcement in `install_artifact.py` (Task 7) needs a pure way to ask a handler "what path would this touch" *before* calling `install()`; the spec's own `collides()` only reveals a path when one already exists, which isn't enough for pre-install validation of a fresh target):

| Member | Purpose |
|---|---|
| `TYPE` | registry key, e.g. `"package_install"` |
| `RISK` | `"inert"` \| `"config"` \| `"exec"` |
| `SCHEMA` | `{"required": [...], "types": {field: type}, "enum": {field: (...)}}` |
| `target_path(payload) -> Path \| None` | the path this action would touch, or `None` if it doesn't write to an allowlisted path (e.g. `package_install` runs a package manager, `discard`/`unsupported` write only to `logs/`) |
| `describe(payload) -> str` | one-line Telegram summary |
| `preview(payload) -> str` | full render for "📄 Show full" -- the **literal command** for exec-tier, file content otherwise |
| `collides(payload) -> Path \| None` | the existing target path, or `None` |
| `install(payload, target, context=None) -> dict` | the effect; `target` is the path `install_artifact.py` already validated (or `None`); `context` carries `{"shortcode": ..., "transcript_excerpt": ...}` for handlers that log rather than write a file. Returns `{"ok", "path", "command", "exit_code", "output", "error"}`. |

- [ ] **Step 1: Write `scripts/actions/registry.py`**

```python
#!/usr/bin/env python3
"""Action registry framework (design spec Component 2). One module per
action type lives alongside this file; each declares the contract documented
in the Phase 4a plan's Task 2 (TYPE, RISK, SCHEMA, target_path, describe,
preview, collides, install) and calls register(sys.modules[__name__]) at
import time. load_all_handlers() imports every known handler module so
REGISTRY is fully populated -- interpret.py and install_artifact.py both call
it once at startup. generate_prompt_catalogue() builds the agent's action-type
menu straight from REGISTRY, so adding a handler automatically teaches the
classifier it exists; no prompt is ever hand-edited to list a new type."""
import importlib

VALID_RISKS = ("inert", "config", "exec")
REGISTRY = {}

# Extended in Phase 4b (Session 4) as new handlers are added -- design spec's
# "pilot loop" turns logs/unsupported_actions.jsonl into the priority order.
HANDLER_MODULE_NAMES = [
    "package_install", "shell_snippet", "git_repo", "calendar_event",
    "rule", "skill", "reference", "note", "discard", "unsupported",
]

REQUIRED_MEMBERS = ("TYPE", "RISK", "SCHEMA", "target_path", "describe", "preview", "collides", "install")


def register(handler, registry_dict=None):
    """Called by each handler module at import time:
    register(sys.modules[__name__]) at the bottom of the file. Validates the
    module declares the full contract before adding it -- a handler missing a
    required member fails loudly at import time, not at first use."""
    target_dict = REGISTRY if registry_dict is None else registry_dict
    for attr in REQUIRED_MEMBERS:
        if not hasattr(handler, attr):
            name = getattr(handler, "__name__", repr(handler))
            raise AttributeError(f"action handler {name} missing required member {attr!r}")
    if handler.RISK not in VALID_RISKS:
        name = getattr(handler, "__name__", repr(handler))
        raise ValueError(f"action handler {name} has invalid RISK {handler.RISK!r}, must be one of {VALID_RISKS}")
    target_dict[handler.TYPE] = handler


def load_all_handlers():
    """Import every handler module in HANDLER_MODULE_NAMES so each one's
    register() call at import time populates the real REGISTRY. Assumes
    scripts/actions/ is already on sys.path (every caller inserts it, matching
    this repo's flat-sibling-import convention -- see interpret.py)."""
    for name in HANDLER_MODULE_NAMES:
        importlib.import_module(name)


def get_handler(action_type, registry_dict=None):
    return (REGISTRY if registry_dict is None else registry_dict).get(action_type)


def _check_field_type(value, expected_type):
    if expected_type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected_type)


def validate_payload(action_type, payload, registry_dict=None):
    """Pure: (ok, error). Checks the handler's SCHEMA against payload.
    Unknown fields are allowed (forward-compatible); a missing required
    field, a wrong type, or a value outside a declared enum are not."""
    handler = get_handler(action_type, registry_dict)
    if handler is None:
        return False, f"unknown action type {action_type!r}"
    schema = handler.SCHEMA
    for field in schema.get("required", []):
        if field not in payload:
            return False, f"{action_type}: missing required payload field {field!r}"
    for field, expected_type in schema.get("types", {}).items():
        if field in payload and not _check_field_type(payload[field], expected_type):
            return False, f"{action_type}: payload field {field!r} must be {expected_type.__name__}"
    for field, allowed_values in schema.get("enum", {}).items():
        if field in payload and payload[field] not in allowed_values:
            return False, f"{action_type}: payload field {field!r} must be one of {allowed_values}"
    return True, None


def validate_action(action, registry_dict=None):
    """Pure: (ok, error) for one action dict {type, confidence, payload}."""
    if "type" not in action:
        return False, "action missing 'type'"
    action_type = action["type"]
    if "confidence" not in action:
        return False, f"{action_type}: action missing 'confidence'"
    confidence = action["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not (0.0 <= confidence <= 1.0):
        return False, f"{action_type}: confidence must be a number in [0, 1]"
    if "payload" not in action or not isinstance(action["payload"], dict):
        return False, f"{action_type}: action missing dict 'payload'"
    return validate_payload(action_type, action["payload"], registry_dict)


def validate_actions(actions, registry_dict=None):
    """Pure: (ok, errors) for a whole action list -- design spec's 'Schema
    violation in the returned action list -> same path as an unparseable
    response. Nothing partially-valid is staged.'"""
    errors = []
    for action in actions:
        ok, error = validate_action(action, registry_dict)
        if not ok:
            errors.append(error)
    return (len(errors) == 0), errors


def generate_prompt_catalogue(registry_dict=None):
    """Builds the agent prompt's action-type menu from the registry -- one
    line per type: risk tier, required payload fields, and the handler
    module's one-line docstring."""
    target_dict = REGISTRY if registry_dict is None else registry_dict
    lines = []
    for action_type in sorted(target_dict):
        handler = target_dict[action_type]
        required = handler.SCHEMA.get("required", [])
        doc = (handler.__doc__ or "").strip().splitlines()[0] if handler.__doc__ else ""
        lines.append(f"- `{action_type}` (risk: {handler.RISK}, payload: {{{', '.join(required)}}}) — {doc}")
    return "\n".join(lines)
```

- [ ] **Step 2: Extend `scripts/test_interpret.py`**

Add near the top (after the `agents_lib` import):

```python
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
```

Add:

```python
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
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 26 passed (13 from Task 1 + 13 new).

- [ ] **Step 4: Commit**

```bash
git add scripts/actions/registry.py scripts/test_interpret.py
git commit -m "feat: add scripts/actions/registry.py -- action registry framework

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Knowledge & meta handlers — `reference`, `note`, `discard`, `unsupported`

**Files:**
- Create: `scripts/actions/_knowledge_common.py`, `scripts/actions/reference.py`, `scripts/actions/note.py`, `scripts/actions/discard.py`, `scripts/actions/unsupported.py`
- Create: `~/.claude/skills/captured-knowledge/SKILL.md`
- Modify: `.gitignore` (add `logs/`)
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Consumes: `registry.register` (Task 2)
- Produces: four registered handlers. `reference`/`note` write markdown into `~/.claude/skills/captured-knowledge/{facts,notes}/<slug>.md` and append one line under that skill's `## Index`. `discard`/`unsupported` never touch an allowlisted path (`target_path` returns `None`); they append JSONL records to `logs/discarded.jsonl` / `logs/unsupported_actions.jsonl` under the repo root.

- [ ] **Step 1: Add `logs/` to `.gitignore`**

Add this block right after the existing `# --- pipeline state/output ...` section (before `# --- scratch`):

```
logs/
```

- [ ] **Step 2: Write `~/.claude/skills/captured-knowledge/SKILL.md`**

```markdown
---
name: captured-knowledge
description: Global, ever-growing store of tools, facts, notes and bookmarks captured from the instagram-to-value content pipeline -- real (not hypothetical) creator tool recommendations, dated facts, and reference material extracted from Instagram posts/reels the user approved for keeping. Use whenever a task could benefit from a previously-captured tool recommendation, fact, or note: the user mentions a tool by name and you're unsure whether it's already been evaluated, asks "have I seen anything about X" or "didn't I save something about this", wants a reference for a topic that might already be captured, or you're about to suggest researching something that may already be in this index. Check the Index below before assuming nothing has been captured.
---

# captured-knowledge

Global on-demand knowledge store, filed by `scripts/actions/reference.py` and
`scripts/actions/note.py` in the `instagram-to-value` project's Phase 4
interpret stage. See
`docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md`'s
"Where knowledge actions install" for why this exists instead of the
per-project auto-memory directory: this loads in every project on the machine
at the fixed cost of the one `description` line above; the body and files
below load only when this skill actually triggers.

## Layout

- `facts/<slug>.md` — a dated fact or pointer (the `reference` action type)
- `notes/<slug>.md` — a raw note (the `note` action type)
- `bookmarks.md` — one-line bookmarks (the `bookmark` action type — not built yet, Phase 4b)
- `tools/<slug>.md` — tool identification records (not built yet, Phase 4b)

## Index

(populated by `scripts/actions/reference.py` and `note.py` as they install —
one line per item, newest last)
```

- [ ] **Step 3: Write `scripts/actions/_knowledge_common.py`**

```python
#!/usr/bin/env python3
"""Shared helpers for the knowledge-family handlers (reference, note) that
install into ~/.claude/skills/captured-knowledge/ -- see that skill's
SKILL.md for the on-disk layout this maintains."""
import re
from pathlib import Path

CAPTURED_KNOWLEDGE_ROOT = Path.home() / ".claude" / "skills" / "captured-knowledge"
INDEX_MARKER = "## Index"


def slug(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "item"


def append_index_line(skill_md_path, line):
    """Append one bullet under '## Index' in SKILL.md, creating the file with
    a minimal frontmatter + Index section if it's somehow missing (defensive
    only -- Task 3 Step 2 always creates the real one first). Mirrors the
    pattern this Claude installation's own auto-memory MEMORY.md already
    uses: one line per fact, content lives in its own file."""
    skill_md_path = Path(skill_md_path)
    if skill_md_path.exists():
        text = skill_md_path.read_text()
    else:
        skill_md_path.parent.mkdir(parents=True, exist_ok=True)
        text = f"---\nname: captured-knowledge\ndescription: Captured knowledge.\n---\n\n{INDEX_MARKER}\n"
    if INDEX_MARKER not in text:
        text = text.rstrip() + f"\n\n{INDEX_MARKER}\n"
    text = text.rstrip("\n") + f"\n- {line}\n"
    skill_md_path.write_text(text)
```

- [ ] **Step 4: Write `scripts/actions/reference.py`**

```python
#!/usr/bin/env python3
"""reference -- a fact or pointer, filed in the global captured-knowledge
skill rather than the per-project auto-memory directory (design spec's
"Where knowledge actions install": the auto-memory path is per-$HOME-session,
not global). RISK inert: a markdown file nothing executes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register
from _knowledge_common import CAPTURED_KNOWLEDGE_ROOT, append_index_line, slug

TYPE = "reference"
RISK = "inert"
SCHEMA = {
    "required": ["title", "content"],
    "types": {"title": str, "content": str},
}


def target_path(payload):
    return CAPTURED_KNOWLEDGE_ROOT / "facts" / f"{slug(payload['title'])}.md"


def describe(payload):
    return f"Reference: {payload['title']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    append_index_line(CAPTURED_KNOWLEDGE_ROOT / "SKILL.md", f"{payload['title']} — facts/{target.name}")
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 5: Write `scripts/actions/note.py`**

```python
#!/usr/bin/env python3
"""note -- a raw note filed in the global captured-knowledge skill (see
reference.py for the destination rationale). RISK inert."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register
from _knowledge_common import CAPTURED_KNOWLEDGE_ROOT, append_index_line, slug

TYPE = "note"
RISK = "inert"
SCHEMA = {
    "required": ["title", "content"],
    "types": {"title": str, "content": str},
}


def target_path(payload):
    return CAPTURED_KNOWLEDGE_ROOT / "notes" / f"{slug(payload['title'])}.md"


def describe(payload):
    return f"Note: {payload['title']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    append_index_line(CAPTURED_KNOWLEDGE_ROOT / "SKILL.md", f"{payload['title']} — notes/{target.name}")
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 6: Write `scripts/actions/discard.py`**

```python
#!/usr/bin/env python3
"""discard -- the post/action offers nothing worth keeping. Never writes to
an allowlisted path; appends one line to logs/discarded.jsonl so the reason
tunes the classifier later (PLAN.md sec 5's discard row, sec 8 risk 4). RISK
inert. Distinct from telegram_bot.py's "❌ Discard" button, which rejects a
whole *proposal* -- this is one *action* the agent itself proposed discarding
(e.g. "this carousel is a personal essay, nothing to capture")."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "discard"
RISK = "inert"
SCHEMA = {
    "required": ["reason"],
    "types": {"reason": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DISCARDED_LOG = REPO_ROOT / "logs" / "discarded.jsonl"


def target_path(payload):
    return None  # internal bookkeeping only, not an agent-drafted install path


def describe(payload):
    return f"Discard: {payload['reason']}"


def preview(payload):
    return payload["reason"]


def collides(payload):
    return None


def install(payload, target=None, context=None):
    DISCARDED_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "shortcode": (context or {}).get("shortcode"),
        "reason": payload["reason"],
        "excerpt": (context or {}).get("transcript_excerpt"),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(DISCARDED_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(DISCARDED_LOG), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 7: Write `scripts/actions/unsupported.py`**

```python
#!/usr/bin/env python3
"""unsupported -- the agent named a real desired action but no handler exists
yet. Never installs; logs to logs/unsupported_actions.jsonl, forming the
pilot's ranked backlog (design spec's "The pilot loop"). RISK inert."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "unsupported"
RISK = "inert"
SCHEMA = {
    "required": ["proposed_type", "payload_sketch", "rationale"],
    "types": {"proposed_type": str, "payload_sketch": dict, "rationale": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UNSUPPORTED_LOG = REPO_ROOT / "logs" / "unsupported_actions.jsonl"


def target_path(payload):
    return None


def describe(payload):
    return f"Unsupported: wants {payload['proposed_type']} — {payload['rationale']}"


def preview(payload):
    return json.dumps(payload, indent=2, ensure_ascii=False)


def collides(payload):
    return None


def install(payload, target=None, context=None):
    UNSUPPORTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "shortcode": (context or {}).get("shortcode"),
        "proposed_type": payload["proposed_type"],
        "payload_sketch": payload["payload_sketch"],
        "rationale": payload["rationale"],
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(UNSUPPORTED_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(UNSUPPORTED_LOG), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 8: Extend `scripts/test_interpret.py`**

Add imports: `import reference, note, discard, unsupported`. Add:

```python
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
```

- [ ] **Step 9: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 39 passed (26 from Tasks 1-2 + 13 new).

- [ ] **Step 10: Commit**

```bash
git add scripts/actions/_knowledge_common.py scripts/actions/reference.py scripts/actions/note.py \
        scripts/actions/discard.py scripts/actions/unsupported.py scripts/test_interpret.py .gitignore
git commit -m "feat: add reference/note/discard/unsupported handlers + captured-knowledge skill

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

Note: `~/.claude/skills/captured-knowledge/SKILL.md` lives outside the repo (`$HOME`, not `instagram-to-value/`) and is not part of this commit.

---

### Task 4: Agent-config handlers — `rule`, `skill`

**Files:**
- Create: `scripts/actions/rule.py`, `scripts/actions/skill.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Both RISK `config` (design spec: "Never automatic ... one Instagram reel should never be able to write a global rule unattended"). `rule` targets `~/.claude/rules/<topic>.md`; `skill` targets `~/.claude/skills/<name>/SKILL.md` with required frontmatter.

- [ ] **Step 1: Write `scripts/actions/rule.py`**

```python
#!/usr/bin/env python3
"""rule -- an always-on constraint, written to ~/.claude/rules/<topic>.md.
RISK config: never automatic, per the design spec's staging-gate rationale."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "rule"
RISK = "config"
SCHEMA = {
    "required": ["topic", "content"],
    "types": {"topic": str, "content": str},
}
RULES_ROOT = Path.home() / ".claude" / "rules"


def _slug(topic):
    return re.sub(r"[^a-z0-9-]+", "-", topic.lower()).strip("-") or "rule"


def target_path(payload):
    return RULES_ROOT / f"{_slug(payload['topic'])}.md"


def describe(payload):
    return f"New global rule: {payload['topic']}"


def preview(payload):
    return payload["content"]


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload["content"])
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 2: Write `scripts/actions/skill.py`**

```python
#!/usr/bin/env python3
"""skill -- a repeatable procedure, written to
~/.claude/skills/<name>/SKILL.md with required frontmatter (name,
description). RISK config: changes future session behaviour, never
auto-installed."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "skill"
RISK = "config"
SCHEMA = {
    "required": ["name", "description", "content"],
    "types": {"name": str, "description": str, "content": str},
}
SKILLS_ROOT = Path.home() / ".claude" / "skills"


def _slug(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"


def target_path(payload):
    return SKILLS_ROOT / _slug(payload["name"]) / "SKILL.md"


def describe(payload):
    return f"New skill: {payload['name']} — {payload['description']}"


def _build_skill_md(payload):
    return f"---\nname: {_slug(payload['name'])}\ndescription: {payload['description']}\n---\n\n{payload['content']}\n"


def preview(payload):
    return _build_skill_md(payload)


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_build_skill_md(payload))
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 3: Extend `scripts/test_interpret.py`**

Add imports: `import rule, skill as skill_action`. Add:

```python
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
```

- [ ] **Step 4: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 46 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/actions/rule.py scripts/actions/skill.py scripts/test_interpret.py
git commit -m "feat: add rule and skill action handlers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Software handlers — `package_install`, `shell_snippet`

**Files:**
- Create: `scripts/actions/package_install.py`, `scripts/actions/shell_snippet.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- `package_install` RISK `exec`, runs the literal `command` via `subprocess.run(..., shell=True)` -- `preview()` renders exactly that string ("Two things to get right" #1). Manager restricted to `npm|uv|pip|cargo|apt|docker` via `SCHEMA["enum"]` (pipx/brew/go are **not** installed on this machine per the design spec).
- `shell_snippet` RISK `config`, target is always `~/.bashrc` (never agent-chosen -- only the snippet content and its marker comment are drafted, so there's no path-injection surface on this handler specifically).

- [ ] **Step 1: Write `scripts/actions/package_install.py`**

```python
#!/usr/bin/env python3
"""package_install -- installs a package via a manager already present on
this machine (npm, uv, pip, cargo, apt, docker -- design spec's registry
list; pipx/brew/go are NOT installed, so the enum below rejects them). RISK
exec: preview() renders the literal command that install() will run,
verbatim, before approval -- the only guard against a mis-transcribed
package name (this repo's own ASR has produced "Claude Det MD" and "Sonar or
Opus", PLAN.md:702-705)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "package_install"
RISK = "exec"
ALLOWED_MANAGERS = ("npm", "uv", "pip", "cargo", "apt", "docker")
SCHEMA = {
    "required": ["manager", "package", "command"],
    "types": {"manager": str, "package": str, "command": str},
    "enum": {"manager": ALLOWED_MANAGERS},
}


def target_path(payload):
    return None  # runs a package manager's own install, not a path we choose


def describe(payload):
    return f"Install `{payload['package']}` via {payload['manager']}"


def preview(payload):
    return payload["command"]


def collides(payload):
    return None  # package managers own their own dedup/upgrade semantics


def install(payload, target=None, context=None):
    command = payload["command"]
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600)
    return {
        "ok": result.returncode == 0,
        "path": None,
        "command": command,
        "exit_code": result.returncode,
        "output": (result.stdout + result.stderr)[-2000:],
        "error": None if result.returncode == 0 else f"exit {result.returncode}",
    }


register(sys.modules[__name__])
```

- [ ] **Step 2: Write `scripts/actions/shell_snippet.py`**

```python
#!/usr/bin/env python3
"""shell_snippet -- appends a managed block to ~/.bashrc (design spec: "a
managed block in ~/.bashrc (no ~/.bashrc.d on this machine)"). The target is
always ~/.bashrc, never agent-chosen -- only the snippet content and its
marker are drafted, so there is no path-injection surface here. RISK config
(a shell init line changes future session behaviour but runs no third-party
code by itself)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "shell_snippet"
RISK = "config"
SCHEMA = {
    "required": ["marker", "snippet", "description"],
    "types": {"marker": str, "snippet": str, "description": str},
}
BASHRC = Path.home() / ".bashrc"


def _block_markers(marker):
    return f"# >>> instagram-to-value: {marker} >>>", f"# <<< instagram-to-value: {marker} <<<"


def target_path(payload):
    return BASHRC


def describe(payload):
    return f"Add shell snippet to ~/.bashrc: {payload['description']}"


def preview(payload):
    start, end = _block_markers(payload["marker"])
    return f"{start}\n{payload['snippet']}\n{end}"


def collides(payload):
    start, _ = _block_markers(payload["marker"])
    if BASHRC.exists() and start in BASHRC.read_text():
        return BASHRC
    return None


def install(payload, target, context=None):
    start, end = _block_markers(payload["marker"])
    block = f"\n{start}\n{payload['snippet']}\n{end}\n"
    with open(target, "a") as f:
        f.write(block)
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": block, "error": None}


register(sys.modules[__name__])
```

Known rough edge, not exercised by this session's verification corpus: install_artifact.py's generic "Keep both" collision resolution (Task 7) suffixes the *target path*, which for every other handler means a distinct file/dir but for `shell_snippet` means a nonsensical `~/.bashrc-2`. Left as-is for Session 4/5 to refine if a real duplicate-marker collision ever occurs -- `collides()` here only fires on an exact marker match, which no post in the current corpus produces.

- [ ] **Step 3: Extend `scripts/test_interpret.py`**

Add imports: `import package_install, shell_snippet`. Add:

```python
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
    import registry
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
```

- [ ] **Step 4: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 57 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/actions/package_install.py scripts/actions/shell_snippet.py scripts/test_interpret.py
git commit -m "feat: add package_install and shell_snippet action handlers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: `git_repo`, `calendar_event`

**Files:**
- Create: `scripts/actions/git_repo.py`, `scripts/actions/calendar_event.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- `git_repo` RISK `config` (the clone itself runs no third-party code; any post-clone build step must be its own separate `shell_snippet`/`package_install` action, per the design spec's self-review note that a repo's clone and its build step carry different risk tiers). Targets `~/tools/<name>`.
- `calendar_event` RISK `inert`. Targets `<repo>/knowledge/calendar/<slug>.ics` — the design spec names Google Calendar MCP "when authenticated" as a future destination; not wired up this session (no verified auth path), so `<repo>/knowledge` (already an allowlisted root) is used instead. Writes a standard `.ics` file.

- [ ] **Step 1: Write `scripts/actions/git_repo.py`**

```python
#!/usr/bin/env python3
"""git_repo -- clones a repo into ~/tools/<name>, matching the existing
convention (context7, free-claude-code, jcode, notebooklm-py). RISK config:
the clone itself runs no third-party code. Any build step the agent thinks is
needed must be proposed as its own shell_snippet/package_install action (RISK
exec) -- see the design spec's self-review note that a git_repo's clone and
its build step carry different risk tiers."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "git_repo"
RISK = "config"
SCHEMA = {
    "required": ["name", "url"],
    "types": {"name": str, "url": str},
}
TOOLS_ROOT = Path.home() / "tools"


def target_path(payload):
    return TOOLS_ROOT / payload["name"]


def describe(payload):
    return f"Clone {payload['url']} to ~/tools/{payload['name']}"


def preview(payload):
    return f"git clone {payload['url']} {TOOLS_ROOT / payload['name']}"


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    result = subprocess.run(["git", "clone", payload["url"], str(target)],
                              capture_output=True, text=True, timeout=600)
    return {
        "ok": result.returncode == 0,
        "path": str(target),
        "command": f"git clone {payload['url']} {target}",
        "exit_code": result.returncode,
        "output": (result.stdout + result.stderr)[-2000:],
        "error": None if result.returncode == 0 else f"exit {result.returncode}",
    }


register(sys.modules[__name__])
```

- [ ] **Step 2: Write `scripts/actions/calendar_event.py`**

```python
#!/usr/bin/env python3
"""calendar_event -- writes a standard .ics file into <repo>/knowledge/calendar/
(design spec lists Google Calendar MCP "when authenticated" as a future
destination; not wired up this session -- no verified auth path). RISK
inert: an .ics file executes nothing."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "calendar_event"
RISK = "inert"
SCHEMA = {
    "required": ["title", "date"],
    "types": {"title": str, "date": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CALENDAR_ROOT = REPO_ROOT / "knowledge" / "calendar"


def _slug(title):
    return re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "event"


def target_path(payload):
    return CALENDAR_ROOT / f"{_slug(payload['title'])}.ics"


def describe(payload):
    time_part = f" {payload['time']}" if payload.get("time") else ""
    return f"Calendar event: {payload['title']} on {payload['date']}{time_part}"


def _build_ics(payload):
    dt = payload["date"].replace("-", "")
    time_part = (payload.get("time") or "000000").replace(":", "")[:6].ljust(6, "0")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//instagram-to-value//captured-event//EN",
        "BEGIN:VEVENT", f"UID:{_slug(payload['title'])}@instagram-to-value",
        f"DTSTAMP:{stamp}", f"DTSTART:{dt}T{time_part}", f"SUMMARY:{payload['title']}",
    ]
    if payload.get("location"):
        lines.append(f"LOCATION:{payload['location']}")
    if payload.get("description"):
        lines.append(f"DESCRIPTION:{payload['description']}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def preview(payload):
    return _build_ics(payload)


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_build_ics(payload))
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
```

- [ ] **Step 3: Extend `scripts/test_interpret.py`**

Add imports: `import git_repo, calendar_event`. Add:

```python
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
```

- [ ] **Step 4: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 64 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/actions/git_repo.py scripts/actions/calendar_event.py scripts/test_interpret.py
git commit -m "feat: add git_repo and calendar_event action handlers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: `scripts/install_artifact.py` — path allowlist + applying an approved action

**Files:**
- Create: `scripts/install_artifact.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Consumes: `registry.get_handler` (Task 2), `staging_lib.update_action_status`/`staging_dir` (Task 8 -- see note below on task ordering)
- Produces: `ALLOWED_ROOTS: list[Path]`, `ALLOWED_FILES: list[Path]`, `PathNotAllowedError(Exception)`, `validate_path(target, allowed_roots=None, allowed_files=None) -> Path` (raises `PathNotAllowedError`), `install_action(action, shortcode, staging_root, resolution="install") -> dict` (`resolution` one of `"install"`, `"replace"`, `"keep_both"`, `"cancel"`).

**Note on ordering:** this task's `install_action()` calls into `staging_lib.update_action_status()`, which Task 8 defines. Since Task 8 has no dependency the other direction, do Task 8 first if executing out of plan order; the plan lists installer before staging state only because the design spec numbers them that way (Components 5 and 6).

- [ ] **Step 1: Write `scripts/install_artifact.py`**

```python
#!/usr/bin/env python3
"""Applying an approved action (design spec Component 6). Central
path-allowlist enforcement so no handler needs to reimplement traversal/
symlink checks -- every handler's install() receives an already-validated
absolute target (or None, for handlers that don't write to a path we choose).
A path outside the allowlist fails the action; it is never silently
relocated (design spec: "Traversal (..) and symlink-escape are checked after
resolution")."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import registry
import staging_lib

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "rules",
    Path.home() / ".claude" / "commands",
    Path.home() / ".claude" / "agents",
    Path.home() / ".claude" / "templates",
    Path.home() / "tools",
    Path.home() / ".local" / "bin",
    Path.home() / "services",
    REPO_ROOT / ".claude",
    REPO_ROOT / "knowledge",
]
# Fixed, never agent-chosen (see shell_snippet.py) -- allowed as an exact file
# rather than widening the allowlist to all of $HOME.
ALLOWED_FILES = [Path.home() / ".bashrc"]


class PathNotAllowedError(Exception):
    pass


def validate_path(target, allowed_roots=None, allowed_files=None):
    """Resolves target (follows symlinks, collapses '..') and checks
    containment under an allowlisted root or exact allowed file. Works for a
    target that doesn't exist yet -- Path.resolve() doesn't require
    existence. Raises PathNotAllowedError rather than silently relocating."""
    allowed_roots = ALLOWED_ROOTS if allowed_roots is None else allowed_roots
    allowed_files = ALLOWED_FILES if allowed_files is None else allowed_files
    resolved = Path(target).resolve()
    for allowed_file in allowed_files:
        if resolved == Path(allowed_file).resolve():
            return resolved
    for root in allowed_roots:
        root = Path(root)
        root_resolved = root.resolve() if root.exists() else root
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise PathNotAllowedError(f"{target} resolves to {resolved}, outside every allowed root/file")


def _suffixed_target(target):
    if target.is_dir() or not target.suffix:
        return target.with_name(f"{target.name}-2")
    return target.with_name(f"{target.stem}-2{target.suffix}")


def install_action(action, shortcode, staging_root, resolution="install"):
    """resolution: 'install' (no collision, or ok to proceed as-is), 'replace',
    'keep_both', or 'cancel'. Returns the InstalledResult dict and, except for
    an unresolved collision, persists it onto the proposal via
    staging_lib.update_action_status()."""
    handler = registry.get_handler(action["type"])
    if handler is None:
        result = {"ok": False, "path": None, "command": None, "exit_code": None,
                    "output": None, "error": f"unknown action type {action['type']!r}"}
        staging_lib.update_action_status(shortcode, staging_root, action["id"], "failed", result)
        return result

    payload = action["payload"]
    target = handler.target_path(payload)
    context = {"shortcode": shortcode}

    if resolution == "cancel":
        result = {"ok": True, "path": None, "command": None, "exit_code": None,
                    "output": "cancelled by user", "error": None}
        staging_lib.update_action_status(shortcode, staging_root, action["id"], "skipped", result)
        return result

    if target is not None:
        try:
            target = validate_path(target)
        except PathNotAllowedError as e:
            result = {"ok": False, "path": None, "command": None, "exit_code": None,
                        "output": None, "error": str(e)}
            staging_lib.update_action_status(shortcode, staging_root, action["id"], "failed", result)
            return result

        existing = handler.collides(payload)
        if existing is not None and resolution == "install":
            # Not yet a final decision -- the caller (telegram_bot.py) prompts
            # Replace/Keep both/Cancel and calls back with that resolution.
            return {"ok": False, "path": str(existing), "command": None, "exit_code": None,
                    "output": None, "error": "collision", "collision": True}

        if existing is not None and resolution == "replace":
            backup_dir = staging_lib.staging_dir(shortcode, staging_root)
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"replaced-{target.name}"
            if target.is_dir():
                shutil.copytree(target, backup, dirs_exist_ok=True)
                shutil.rmtree(target)
            else:
                shutil.copy2(target, backup)
        elif existing is not None and resolution == "keep_both":
            target = _suffixed_target(target)

    result = handler.install(payload, target, context)
    staging_lib.update_action_status(shortcode, staging_root, action["id"],
                                       "installed" if result["ok"] else "failed", result)
    return result
```

- [ ] **Step 2: Extend `scripts/test_interpret.py`**

Add import: `import install_artifact`. Add:

```python
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
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: 75 passed (64 from Tasks 1-6 + 11 new; assumes Task 8 lands first per the ordering note above).

- [ ] **Step 4: Commit**

```bash
git add scripts/install_artifact.py scripts/test_interpret.py
git commit -m "feat: add install_artifact.py -- path allowlist + collision handling

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: `scripts/staging_lib.py` — the on-disk staging contract

**Files:**
- Create: `scripts/staging_lib.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Produces: `RISK_BADGES`, `TYPE_CATEGORY`, `AUTO_DISCARD_CONFIDENCE_THRESHOLD = 0.8`, `staging_dir`, `proposal_path`, `write_proposal`, `read_proposal`, `has_staging`, `list_pending`, `update_action_status`, `set_message_id`, `record_reject_reason`, `risk_badge`, `format_proposal_message`, `is_auto_discard_eligible`, `format_digest`, `aggregate_unsupported`, `format_unsupported_summary`.

**Must land before Task 7** (see Task 7's ordering note) since `install_artifact.install_action()` calls `staging_lib.update_action_status`/`staging_dir`.

- [ ] **Step 1: Write `scripts/staging_lib.py`**

```python
#!/usr/bin/env python3
"""On-disk staging contract for Phase 4a (design spec Component 5). Mirrors
jobs_lib.py's role: one place that knows the staging/<shortcode>/ layout, so
interpret.py, install_artifact.py and telegram_bot.py never hand-build paths.
See PLAN.md sec 7 Phase 4 and the design spec's "Data / schema additions"."""
import json
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
    interpreted -- makes the sweep safe to re-run."""
    return staging_dir(shortcode, staging_root).exists()


def list_pending(staging_root):
    """Shortcodes with status == 'pending', oldest proposal.json first."""
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
                candidates.append((p.stat().st_mtime, d.name))
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
    lines = [proposal.get("summary") or proposal["shortcode"]]
    resolver = proposal.get("resolver") or {}
    if resolver.get("status"):
        tool_bit = f" ({resolver['tool_name']})" if resolver.get("tool_name") else ""
        lines.append(f"Tool resolver: {resolver['status']}{tool_bit}")
    for action in proposal["actions"]:
        describe_fn = describe_fns.get(action["type"])
        text = describe_fn(action["payload"]) if describe_fn else action["type"]
        lines.append(f"{risk_badge(action['risk'])} [{action['id']}] `{action['type']}` — {text}")
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
```

- [ ] **Step 2: Extend `scripts/test_interpret.py`**

Add import: `import staging_lib`. Add:

```python
def test_write_and_read_proposal_roundtrip(tmp_path):
    staging_lib.write_proposal("ABC", tmp_path, {"shortcode": "ABC", "actions": []})
    assert staging_lib.read_proposal("ABC", tmp_path) == {"shortcode": "ABC", "actions": []}


def test_read_proposal_missing_returns_none(tmp_path):
    assert staging_lib.read_proposal("NOPE", tmp_path) is None


def test_has_staging_true_after_write(tmp_path):
    staging_lib.write_proposal("ABC", tmp_path, {"shortcode": "ABC", "actions": []})
    assert staging_lib.has_staging("ABC", tmp_path) is True
    assert staging_lib.has_staging("NOPE", tmp_path) is False


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
    text = staging_lib.format_proposal_message(proposal, {"package_install": lambda p: f"install {p['package']}"})
    assert "zoxide, a smarter cd" in text
    assert "resolved (zoxide)" in text
    assert "install zoxide" in text


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
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: all tests through Task 8 pass (this task adds 18; run after Task 6's 64 gives 82, before Task 7 is added).

- [ ] **Step 4: Commit**

```bash
git add scripts/staging_lib.py scripts/test_interpret.py
git commit -m "feat: add staging_lib.py -- on-disk staging contract + digest formatting

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: `scripts/resolve_tools.py` — the §4 hidden-tool-name resolver

**Files:**
- Create: `scripts/resolve_tools.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Produces: `extract_urls(text) -> list[str]`, `is_self_referential(url) -> bool`, `tier1_creator_replies(comments, channel) -> list[str]`, `tier2_caption_and_comments(description, comments) -> list[str]`, `resolve_pure_tiers(description, comments, channel) -> dict` (`{status: "resolved"|"uncertain"|"not_found", tier, candidates}`), `sample_frame_timestamps(duration_s, interval_s=5, max_frames=8) -> list[float]`, `resolve_via_frame_ocr(shortcode, media_root, duration_s) -> dict`, `resolve_via_web_search(search_agent_text, extract_json_fn) -> dict`.

- [ ] **Step 1: Write `scripts/resolve_tools.py`**

```python
#!/usr/bin/env python3
"""The §4 hidden-tool-name resolver (design spec Component 3). Tiers 1-2 are
pure Python URL extraction; tiers 3 (frame OCR) and 4 (web search) each wrap
one impure call and are exercised by interpret.py's orchestration, not run
standalone. See PLAN.md sec 4 and the design spec's escalation order (tier 3
moved last, deliberate deviation #3: the default fetch stays audio-only, only
a post that survives tiers 1/2/4 unresolved pays for a video download)."""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
URL_RE = re.compile(r"https?://[^\s)\]]+")


def _dedup(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def extract_urls(text):
    """Pure: every http(s) URL in text, order preserved, deduped, trailing
    punctuation stripped."""
    return _dedup(url.rstrip(".,;:!?)") for url in URL_RE.findall(text or ""))


def is_self_referential(url):
    """Pure: an instagram.com link back to the post itself/the platform is
    never the answer to "what tool did they demo"."""
    return "instagram.com" in url.lower()


def tier1_creator_replies(comments, channel):
    """Pure: URLs from comments authored by the post's own channel
    (case-insensitive), minus self-referential links."""
    channel_lower = (channel or "").lower()
    if not channel_lower:
        return []
    urls = []
    for comment in comments or []:
        if (comment.get("author") or "").lower() == channel_lower:
            urls.extend(u for u in extract_urls(comment.get("text") or "") if not is_self_referential(u))
    return _dedup(urls)


def tier2_caption_and_comments(description, comments):
    """Pure: URLs from the caption + every comment (not just the creator's),
    minus self-referential links -- the fallback when tier 1 finds nothing."""
    urls = list(extract_urls(description or ""))
    for comment in comments or []:
        urls.extend(extract_urls(comment.get("text") or ""))
    return _dedup(u for u in urls if not is_self_referential(u))


def resolve_pure_tiers(description, comments, channel):
    """Runs tier 1 then tier 2. Returns {status, tier, candidates} where
    status is 'resolved' (one candidate), 'uncertain' (several), or
    'not_found' (neither tier found anything -- not a final resolver status,
    just "the Python tiers have nothing"; interpret.py's agent call or tier 4
    may still resolve it from context/search)."""
    tier1 = tier1_creator_replies(comments, channel)
    if tier1:
        return {"status": "resolved" if len(tier1) == 1 else "uncertain", "tier": 1, "candidates": tier1}
    tier2 = tier2_caption_and_comments(description, comments)
    if tier2:
        return {"status": "resolved" if len(tier2) == 1 else "uncertain", "tier": 2, "candidates": tier2}
    return {"status": "not_found", "tier": None, "candidates": []}


def sample_frame_timestamps(duration_s, interval_s=5, max_frames=8):
    """Pure: evenly-spaced sample points for tier 3's frame OCR, capped so a
    long reel doesn't spend unbounded OCR calls. Always at least one frame."""
    if duration_s <= 0:
        return [0.0]
    count = min(max_frames, max(1, int(duration_s // interval_s) + 1))
    step = duration_s / count
    return [round(i * step, 2) for i in range(count)]


def resolve_via_frame_ocr(shortcode, media_root, duration_s):
    """Tier 3, last resort: re-fetch the mp4 (--keep-mp4), sample frames with
    ffmpeg, OCR each with the existing PP-OCRv6 pass (no escalation ladder --
    on-screen UI text is Latin-script screenshare, not the Hebrew-carousel
    case that ladder was built for), collect the recognized text as evidence
    for interpret.py's redraft call. Never raises -- a failed refetch/OCR
    degrades to no extra evidence rather than failing the whole run."""
    media_dir = Path(media_root) / shortcode
    mp4_path = media_dir / f"{shortcode}.mp4"
    if not mp4_path.exists():
        fetch_result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "fetch.py"),
             f"https://www.instagram.com/p/{shortcode}/", "--media-root", str(media_root), "--keep-mp4"],
            capture_output=True, text=True, timeout=900,
        )
        if fetch_result.returncode != 0 or not mp4_path.exists():
            return {"status": "unavailable", "texts": [], "error": fetch_result.stderr[-500:]}

    texts = []
    for ts in sample_frame_timestamps(duration_s):
        frame_path = media_dir / f"{shortcode}.frame_{ts:.2f}.jpg"
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", str(mp4_path), "-frames:v", "1", str(frame_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 or not frame_path.exists():
            continue
        ocr_result = subprocess.run(
            [str(Path.home() / ".local" / "venvs" / "paddleocr" / "bin" / "python"),
             str(REPO_ROOT / "scripts" / "ocr_local.py"), str(frame_path)],
            capture_output=True, text=True, timeout=120,
        )
        if ocr_result.returncode == 0:
            try:
                data = json.loads(ocr_result.stdout)
                if data.get("text"):
                    texts.append(data["text"])
            except json.JSONDecodeError:
                pass
    return {"status": "sampled" if texts else "no_text_found", "texts": texts, "error": None}


def resolve_via_web_search(search_agent_text, extract_json_fn):
    """Tier 4: parse the search agent's JSON response into the resolver
    contract. extract_json_fn is agents_lib.extract_json, passed in rather
    than imported to keep this module's dependency direction pointing away
    from agents_lib (resolve_tools doesn't need to know how the agent was
    invoked, only how to read its answer)."""
    try:
        data = extract_json_fn(search_agent_text)
    except ValueError:
        return {"status": "unresolved", "tool_name": None, "url": None, "sources": []}
    confidence = (data.get("confidence") or "low").lower()
    if not data.get("tool_name"):
        return {"status": "unresolved", "tool_name": None, "url": None, "sources": data.get("sources", [])}
    status = "resolved" if confidence == "high" else "uncertain"
    return {"status": status, "tool_name": data.get("tool_name"), "url": data.get("url"),
             "sources": data.get("sources", [])}
```

- [ ] **Step 2: Extend `scripts/test_interpret.py`**

Add import: `import resolve_tools`. Add:

```python
def test_extract_urls_dedupes_and_strips_punctuation():
    text = "check https://zoxide.dev/. also https://zoxide.dev/ and (https://other.com/x)!"
    assert resolve_tools.extract_urls(text) == ["https://zoxide.dev/", "https://other.com/x"]


def test_extract_urls_empty_text():
    assert resolve_tools.extract_urls("") == []
    assert resolve_tools.extract_urls(None) == []


def test_is_self_referential():
    assert resolve_tools.is_self_referential("https://www.instagram.com/p/ABC/") is True
    assert resolve_tools.is_self_referential("https://github.com/x/y") is False


def test_tier1_creator_replies_filters_by_author(mocker=None):
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
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: adds 18 tests to the running total.

- [ ] **Step 4: Real spot-check against the real corpus (not a substitute for the tests, a sanity check on real data)**

```bash
python3 -c "
import json, sys
sys.path.insert(0, 'scripts')
import resolve_tools
for sc in ['Dce_LrgCrFA', 'DW1O6ZBEfDa', 'DcmLKiSF75h']:
    info = json.load(open(f'media/{sc}/{sc}.info.json'))
    result = resolve_tools.resolve_pure_tiers(info.get('description', ''), info.get('comments', []), info.get('channel'))
    print(sc, result)
"
```

Expected: all three print `{'status': 'not_found', 'tier': None, 'candidates': []}` — confirmed during planning (Task 9's design note): none of the 7-post corpus's comments/captions contain an explicit URL, so every post in this corpus depends on agent call #1 recognizing the tool by name from the transcript/caption itself, not on tiers 1-2. This is expected, not a bug — record it as-is in the pilot brief (Task 14).

- [ ] **Step 5: Commit**

```bash
git add scripts/resolve_tools.py scripts/test_interpret.py
git commit -m "feat: add resolve_tools.py -- sec 4 hidden-tool-name resolver

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: `scripts/interpret.py` — the stage orchestrator

**Files:**
- Create: `scripts/interpret.py`
- Test: `scripts/test_interpret.py` (extend)

**Interfaces:**
- Consumes: `agents_lib.run_agent`/`extract_json`/`get_backend` (Task 1), `registry.load_all_handlers`/`validate_actions`/`get_handler`/`generate_prompt_catalogue` (Task 2), `resolve_tools.resolve_pure_tiers`/`resolve_via_web_search`/`resolve_via_frame_ocr` (Task 9), `staging_lib.write_proposal`/`staging_dir`/`is_auto_discard_eligible` (Task 8)
- Produces: `load_post_context`, `build_triage_prompt`, `build_search_prompt`, `build_redraft_prompt`, `call_agent_for_json`, `interpret_post(shortcode, media_root, extracted_root, staging_root, logs_root, backend=None, origin="telegram") -> dict` (raises `RuntimeError` on unrecoverable agent failure, `FileNotFoundError` if `extracted/<shortcode>.json` is missing), `backfill_sweep(media_root, extracted_root, staging_root, logs_root, backend=None, notify_fn=None, interpret_post_fn=interpret_post) -> dict`, CLI (`<shortcode>` or `--backfill-sweep`).

- [ ] **Step 1: Write `scripts/interpret.py`**

```python
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
several actions. Prefer the WEAKEST action that captures the value (a reference
beats a rule; do not propose a rule/skill unless it is a genuine reusable
procedure or always-on constraint).

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
    draft, agent_result = call_agent_for_json(triage_prompt, ACTION_LIST_SCHEMA, backend)
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
                redraft, _ = call_agent_for_json(build_redraft_prompt(triage_prompt, web_result),
                                                   ACTION_LIST_SCHEMA, backend)
                agent_calls += 1
                if redraft is not None:
                    draft = redraft
            elif context["extracted"].get("media_type") == "video":
                frame_result = resolve_tools.resolve_via_frame_ocr(shortcode, media_root, context["duration_s"])
                if frame_result["texts"]:
                    redraft, _ = call_agent_for_json(
                        build_redraft_prompt(triage_prompt, {"on_screen_text": frame_result["texts"]}),
                        ACTION_LIST_SCHEMA, backend)
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
```

- [ ] **Step 2: Extend `scripts/test_interpret.py`**

Add import: `import interpret`. Add (these stub `agents_lib.run_agent` via `monkeypatch` so no real CLI is invoked — Task 12's live verification is what exercises the real backends):

```python
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
    assert proposal["agent_calls"] == 1
    assert proposal["actions"][0]["type"] == "package_install"
    assert proposal["actions"][0]["risk"] == "exec"
    assert staging_lib.read_proposal("ABC", staging_root) == proposal


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
    import discard
    monkeypatch.setattr(discard, "DISCARDED_LOG", logs_root / "discarded.jsonl")
    proposal = interpret.interpret_post("ABC", media_root, extracted_root, staging_root, logs_root, origin="backfill")
    assert proposal["actions"][0]["status"] == "auto_discarded"
    assert proposal["status"] == "decided"
    assert (logs_root / "discarded.jsonl").exists()


def test_backfill_sweep_skips_already_staged(tmp_path, monkeypatch):
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
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_interpret.py -v
```

Expected: adds 11 tests to the running total.

- [ ] **Step 4: Commit**

```bash
git add scripts/interpret.py scripts/test_interpret.py
git commit -m "feat: add interpret.py -- Phase 4a stage orchestrator + backfill sweep

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 11: Wire the interpret stage into `worker.py` and `telegram_bot.py`

**Files:**
- Modify: `scripts/telegram_notify.py` (add `send_with_buttons`)
- Modify: `scripts/worker.py` (4th stage + origin-based notify)
- Modify: `scripts/telegram_bot.py` (source="telegram", callback handler, /agent, /pending, reject-reason capture)
- Test: `scripts/test_ingest.py` (extend — these are Phase 3 files, so new pure-function tests for them join the Phase 3 test file, matching the existing per-phase split)

**Interfaces:**
- Consumes: `interpret.py` as a subprocess (matching `fetch.py`/`extract.py`'s pattern), `staging_lib.format_proposal_message`/`read_proposal`/`record_reject_reason` (Task 8), `install_artifact.install_action` (Task 7), `registry.REGISTRY`/`load_all_handlers` (Task 2), `agents_lib.get_backend`/`set_backend` (Task 1).
- Produces: `telegram_notify.send_with_buttons(text, buttons, chat_id=None, secrets_path=...) -> int` (returns the sent message's id), `worker.build_proposal_buttons(actions) -> list[list[tuple]] | None`, `worker.notify_proposal_now(proposal, chat_id, staging_root)`.

- [ ] **Step 1: Extend `scripts/telegram_notify.py`**

Add after the existing `_send_async`/`send` functions:

```python
async def _send_with_buttons_async(text, token, chat_id, buttons):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in buttons])
    async with Bot(token) as bot:
        message = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return message.message_id


def send_with_buttons(text, buttons, chat_id=None, secrets_path=DEFAULT_SECRETS):
    """Like send(), but attaches an inline keyboard and returns the sent
    message's id (design spec's proposal.json "message_id" field). buttons:
    list of rows, each a list of (label, callback_data) tuples. Callback taps
    are received by whichever process is polling with this same bot token --
    telegram_bot.py's Application, not this send-only helper."""
    token = load_secret("TELEGRAM_BOT_TOKEN", secrets_path)
    if not token:
        raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_BOT_TOKEN not set in {secrets_path}")
    if chat_id is None:
        chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", secrets_path)
        if not chat_id:
            raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {secrets_path}")
    return asyncio.run(_send_with_buttons_async(text, token, chat_id, buttons))
```

- [ ] **Step 2: Modify `scripts/worker.py`**

Add to the imports block (after the existing `from telegram_notify import send as notify`):

```python
import telegram_notify
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import registry
import staging_lib

registry.load_all_handlers()
```

Add near the other `DEFAULT_*_ROOT` constants:

```python
DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"
DEFAULT_LOGS_ROOT = REPO_ROOT / "logs"
```

Add these two new functions above `process_job`:

```python
def build_proposal_buttons(actions):
    """None if nothing is left to decide (e.g. every action auto-discarded)
    -- design spec's approval-flow button row for a manual-origin proposal."""
    if not any(a["status"] == "pending" for a in actions):
        return None
    return [[("✅ All", f"approve_all:{{shortcode}}"), ("☑️ Pick…", f"pick:{{shortcode}}")],
             [("📄 Show full", f"show:{{shortcode}}"), ("❌ Discard", f"discard:{{shortcode}}")]]


def notify_proposal_now(proposal, chat_id, staging_root):
    """Manual-origin notification: full summary + buttons, immediately
    (design spec's "Manual origin" section). Wrapped like every other
    worker-owned notify call (open item #7's fix, Session 1) -- a Telegram
    blip here must not crash a job that otherwise finished successfully."""
    describe_fns = {t: h.describe for t, h in registry.REGISTRY.items()}
    text = staging_lib.format_proposal_message(proposal, describe_fns)
    buttons_template = build_proposal_buttons(proposal["actions"])
    try:
        if buttons_template:
            buttons = [[(label, data.format(shortcode=proposal["shortcode"])) for label, data in row]
                        for row in buttons_template]
            message_id = telegram_notify.send_with_buttons(text, buttons, chat_id=chat_id)
            staging_lib.set_message_id(proposal["shortcode"], staging_root, message_id)
        else:
            telegram_notify.send(text, chat_id=chat_id)
    except Exception as e:
        print(f"[worker] WARNING: proposal notify failed, continuing: {e}", file=sys.stderr)
```

In `process_job`, add the 4th stage right after the existing extract block (after the `if not ok:` failure branch for extract, before the `media_dir = Path(media_root) / shortcode` line that resolves the creator username) — the function signature grows one parameter, `staging_root`:

```python
def process_job(shortcode, url, chat_id, jobs_root, media_root, extracted_root, pages_root,
                  staging_root=DEFAULT_STAGING_ROOT, source="telegram"):
```

(update the existing `def process_job(...)` line to the signature above), then after extract succeeds and before the creator-enrollment block, insert:

```python
    note(f"⏳ interpreting {shortcode}")
    ok, out, err = run_subprocess(
        [sys.executable, str(REPO_ROOT / "scripts" / "interpret.py"), shortcode,
         "--media-root", str(media_root), "--extracted-root", str(extracted_root),
         "--staging-root", str(staging_root), "--origin", source],
        "interpret",
    )
    if not ok:
        move_job(shortcode, jobs_root, "running", "failed",
                  error=err[-2000:], failed_at=datetime.now(timezone.utc).isoformat())
        note(f"❌ {shortcode} failed at interpret")
        return
    try:
        proposal = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        proposal = {}
```

Then, at the very end of `process_job` (after the existing `move_job(..., "done", ...)` + done-notify `note(...)` lines), append:

```python
    if proposal and source == "telegram":
        notify_proposal_now(proposal, chat_id, staging_root)
```

Update `drain_once` and `main` to thread `staging_root`/`source` through: `drain_once(jobs_root, media_root, extracted_root, pages_root, staging_root=DEFAULT_STAGING_ROOT, ...)` calling `process_job_fn(..., staging_root=staging_root, source=job.get("source", "telegram"))` (design spec: "jobs missing the field default to 'telegram'"), and add `ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))` in `main()`, passed through to `drain_once`.

- [ ] **Step 3: Modify `scripts/telegram_bot.py`**

Add to imports (after the existing ones):

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler
import agents_lib
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import install_artifact
import registry
import staging_lib

registry.load_all_handlers()
```

Add `DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"` near `DEFAULT_JOBS_ROOT`.

In `handle_message`, two changes: read `staging_root` from `context.bot_data`, and check `context.user_data` for a pending reject-reason *before* the shortcode-extraction logic; add `source="telegram"` to the `write_job` call:

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = context.bot_data["allowed_chat_id"]
    jobs_root = context.bot_data["jobs_root"]
    staging_root = context.bot_data["staging_root"]
    chat_id = update.effective_chat.id

    if not is_allowed_chat(chat_id, allowed_chat_id):
        logger.warning("rejected message from unauthorized chat_id=%s", chat_id)
        return

    text = (update.message.text or "").strip()

    awaiting = context.user_data.get("awaiting_reason_for")
    if awaiting:
        del context.user_data["awaiting_reason_for"]
        staging_lib.record_reject_reason(awaiting, staging_root, text)
        await update.message.reply_text(f"Noted. {awaiting} discarded.")
        return

    try:
        shortcode = extract_shortcode(text)
    except SystemExit:
        await update.message.reply_text("Send an Instagram post/reel URL.")
        return

    state, _ = find_job(shortcode, jobs_root)
    if state is not None:
        await update.message.reply_text(status_reply(state, shortcode))
        return

    write_job(
        shortcode, jobs_root, "queued",
        url=text, chat_id=chat_id, source="telegram",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    await update.message.reply_text(f"Queued {shortcode}.")
```

Add the new command handlers and the callback handler:

```python
async def cmd_agent(update, context):
    if not is_allowed_chat(update.effective_chat.id, context.bot_data["allowed_chat_id"]):
        return
    if not context.args:
        await update.message.reply_text(f"Current backend: {agents_lib.get_backend()}. Usage: /agent claude|codex")
        return
    try:
        agents_lib.set_backend(context.args[0])
        await update.message.reply_text(f"Backend set to {context.args[0]}.")
    except ValueError as e:
        await update.message.reply_text(str(e))


async def cmd_pending(update, context):
    if not is_allowed_chat(update.effective_chat.id, context.bot_data["allowed_chat_id"]):
        return
    pending = staging_lib.list_pending(context.bot_data["staging_root"])
    if not pending:
        await update.message.reply_text("Nothing pending.")
        return
    lines = []
    for shortcode in pending:
        proposal = staging_lib.read_proposal(shortcode, context.bot_data["staging_root"])
        lines.append(f"{shortcode}: {proposal.get('summary', '')}")
    await update.message.reply_text(f"{len(pending)} pending:\n" + "\n".join(lines))


def _proposal_buttons(shortcode):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ All", callback_data=f"approve_all:{shortcode}"),
         InlineKeyboardButton("☑️ Pick…", callback_data=f"pick:{shortcode}")],
        [InlineKeyboardButton("📄 Show full", callback_data=f"show:{shortcode}"),
         InlineKeyboardButton("❌ Discard", callback_data=f"discard:{shortcode}")],
    ])


async def _report_install_results(query, shortcode, results):
    lines = []
    for action, result in results:
        if result.get("collision"):
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("Replace", callback_data=f"replace:{shortcode}:{action['id']}"),
                InlineKeyboardButton("Keep both", callback_data=f"keep_both:{shortcode}:{action['id']}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{shortcode}:{action['id']}"),
            ]])
            await query.message.reply_text(f"⚠️ {result['path']} exists — [{action['id']}] {action['type']}",
                                              reply_markup=buttons)
            continue
        status = "✅ installed" if result["ok"] else f"❌ {result.get('error')}"
        lines.append(f"[{action['id']}] {action['type']}: {status}")
    if lines:
        await query.message.reply_text("\n".join(lines))


async def handle_callback(update, context):
    query = update.callback_query
    if not is_allowed_chat(query.message.chat_id, context.bot_data["allowed_chat_id"]):
        return
    await query.answer()
    staging_root = context.bot_data["staging_root"]
    action_str, _, rest = query.data.partition(":")

    if action_str == "approve_all":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        results = [(a, install_artifact.install_action(a, shortcode, staging_root))
                    for a in proposal["actions"] if a["status"] == "pending"]
        await _report_install_results(query, shortcode, results)
        return

    if action_str == "pick":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        buttons = [[InlineKeyboardButton(f"{a['id']} {a['type']}", callback_data=f"toggle:{shortcode}:{a['id']}")]
                    for a in proposal["actions"] if a["status"] == "pending"]
        buttons.append([InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_pick:{shortcode}")])
        context.user_data.setdefault("picked", {})[shortcode] = set()
        await query.message.reply_text("Tap actions to include, then Confirm:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action_str == "toggle":
        shortcode, action_id = rest.split(":", 1)
        picked = context.user_data.setdefault("picked", {}).setdefault(shortcode, set())
        picked.symmetric_difference_update({action_id})
        await query.answer(f"{action_id}: {'included' if action_id in picked else 'excluded'}")
        return

    if action_str == "confirm_pick":
        shortcode = rest
        picked = context.user_data.get("picked", {}).get(shortcode, set())
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        results = [(a, install_artifact.install_action(a, shortcode, staging_root,
                                                           resolution="install" if a["id"] in picked else "cancel"))
                    for a in proposal["actions"] if a["status"] == "pending"]
        await _report_install_results(query, shortcode, results)
        return

    if action_str == "show":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        parts = [f"[{a['id']}] {a['type']}:\n{registry.get_handler(a['type']).preview(a['payload'])}"
                  for a in proposal["actions"]]
        text = "\n\n".join(parts) or "No actions."
        for start in range(0, len(text), 3500):
            await query.message.reply_text(text[start:start + 3500])
        return

    if action_str == "discard":
        context.user_data["awaiting_reason_for"] = rest
        await query.message.reply_text("why?")
        return

    if action_str in ("replace", "keep_both", "cancel"):
        shortcode, action_id = rest.split(":", 1)
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        action = next(a for a in proposal["actions"] if a["id"] == action_id)
        result = install_artifact.install_action(action, shortcode, staging_root, resolution=action_str)
        status = "✅ installed" if result["ok"] else f"❌ {result.get('error')}"
        await query.message.reply_text(f"[{action_id}] {action['type']}: {status}")
        return
```

In `main()`, add `ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))`, set `application.bot_data["staging_root"] = args.staging_root`, and register the new handlers before the existing `MessageHandler` (command/callback handlers must be added first — `python-telegram-bot` checks handlers in registration order, and the plain-text `MessageHandler`'s `~filters.COMMAND` filter would otherwise never let `CommandHandler` fire if order were reversed... it wouldn't actually collide since `CommandHandler` only matches `/`-prefixed text and the existing filter already excludes those, but callback queries are a different update type entirely and must be registered as `CallbackQueryHandler`, not `MessageHandler`, which is the actual reason order doesn't matter here — added first for readability, not necessity):

```python
    application.add_handler(CommandHandler("agent", cmd_agent))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
```

- [ ] **Step 4: Extend `scripts/test_ingest.py`**

Add imports: `import worker` (already imported per Task 5 of the Phase 3 plan), `import telegram_bot` (already imported). Add:

```python
def test_build_proposal_buttons_none_when_nothing_pending():
    actions = [{"status": "installed"}, {"status": "skipped"}]
    assert worker.build_proposal_buttons(actions) is None


def test_build_proposal_buttons_present_when_pending_exists():
    actions = [{"status": "pending"}]
    buttons = worker.build_proposal_buttons(actions)
    assert buttons is not None
    assert len(buttons) == 2
```

- [ ] **Step 5: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_ingest.py scripts/test_interpret.py -v
```

Expected: `test_ingest.py` gains 2, `test_interpret.py` unchanged from Task 10's total, zero regressions across every existing test file (`test_extract.py` included).

```bash
python3 -m pytest scripts/ -v
```

Expected: full suite passes.

- [ ] **Step 6: Commit**

```bash
git add scripts/telegram_notify.py scripts/worker.py scripts/telegram_bot.py scripts/test_ingest.py
git commit -m "feat: wire interpret stage into worker.py + telegram_bot.py approval flow

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 12: Live verification against the real 7-post corpus + backend parity

**Files:** none created — this task runs `interpret.py` for real, no code changes expected unless verification surfaces a bug (fix it, re-run, note the deviation in Task 13's writeup).

**Precondition:** all of Tasks 1-11 committed, full `scripts/` test suite green.

- [ ] **Step 1: Confirm the 7 posts are still on disk untouched**

```bash
for sc in Dce_LrgCrFA DW1O6ZBEfDa DcmLKiSF75h DchEg-1PNFF DbsDXkgJ_FB DcblDAVNrgn DcgAqIADbs2; do
  test -f extracted/$sc.json && echo "$sc: OK" || echo "$sc: MISSING"
done
```

Expected: all 7 print OK (they were present at session start and the backfill drain only *adds* new shortcodes).

- [ ] **Step 2: Run interpret.py against each of the 7, timing the first one**

```bash
mkdir -p /tmp/phase4a-verify
time python3 scripts/interpret.py Dce_LrgCrFA --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/Dce_LrgCrFA.json
python3 scripts/interpret.py DW1O6ZBEfDa    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DW1O6ZBEfDa.json
python3 scripts/interpret.py DcmLKiSF75h    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DcmLKiSF75h.json
python3 scripts/interpret.py DchEg-1PNFF    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DchEg-1PNFF.json
python3 scripts/interpret.py DbsDXkgJ_FB    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DbsDXkgJ_FB.json
python3 scripts/interpret.py DcblDAVNrgn    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DcblDAVNrgn.json
python3 scripts/interpret.py DcgAqIADbs2    --staging-root /tmp/phase4a-verify/staging > /tmp/phase4a-verify/DcgAqIADbs2.json
```

Record the `real`/`user`/`sys` line from `time` and each proposal's `"agent_calls"` field — Task 13's pilot brief needs both.

- [ ] **Step 3: Check each proposal against the pass bar**

```bash
python3 -c "
import json
bar = {
    'Dce_LrgCrFA': {'package_install', 'shell_snippet'},
    'DW1O6ZBEfDa': {'git_repo'},
    'DcmLKiSF75h': {'package_install'},
    'DchEg-1PNFF': {'calendar_event'},
    'DbsDXkgJ_FB': {'rule', 'skill'},  # either counts
    'DcblDAVNrgn': set(),
    'DcgAqIADbs2': set(),
}
for sc, expected in bar.items():
    data = json.load(open(f'/tmp/phase4a-verify/{sc}.json'))
    got = {a['type'] for a in data['actions']}
    if sc in ('DcblDAVNrgn', 'DcgAqIADbs2'):
        ok = not (got - {'discard', 'unsupported'})  # 'no tool actions' -- discard/note-only is fine
    elif sc == 'DbsDXkgJ_FB':
        ok = bool(got & expected)
    else:
        ok = expected <= got
    print(sc, 'PASS' if ok else 'FAIL', '-- got', sorted(got), 'agent_calls:', data.get('agent_calls'))
"
```

If any post fails, this is exactly the situation the plan's "Two things to get right" and Task 9 Step 4's note anticipated: the classification is an LLM judgment call the prompt can steer but not hard-code. Diagnose with `cat /tmp/phase4a-verify/<shortcode>.json` and the post's real transcript/caption; adjust `build_triage_prompt` (Task 10) if the fix is a prompting gap, or the registry `SCHEMA`/handler if it's a payload-shape gap. Re-run only the failing shortcode, then re-run the full suite (`python3 -m pytest scripts/ -v`) before moving on. Record every real deviation found here in Task 13's writeup — do not silently smooth over a fix.

- [ ] **Step 4: Backend parity — run one post under codex**

```bash
python3 scripts/agents_lib.py "reply with exactly: OK" --backend codex
```

Confirms codex is actually callable non-interactively before spending a real interpret run on it. Then:

```bash
python3 scripts/interpret.py DcmLKiSF75h --backend codex --staging-root /tmp/phase4a-verify/staging-codex \
  > /tmp/phase4a-verify/DcmLKiSF75h.codex.json
python3 -c "
import json
data = json.load(open('/tmp/phase4a-verify/DcmLKiSF75h.codex.json'))
assert data['backend'] == 'codex'
assert data['actions'], 'codex produced zero actions'
print('codex parity OK:', [a['type'] for a in data['actions']], 'agent_calls:', data['agent_calls'])
"
```

If `build_cmd`'s codex invocation needs adjustment (e.g. `--search`/`--output-schema` behaving differently live than the flags implied), fix `agents_lib.py`, re-run this step, re-run `python3 -m pytest scripts/test_interpret.py -v`, and record the fix in Task 13.

- [ ] **Step 5: Measure the tier-4/tier-3 escalation cost**

None of the 7 corpus posts hit tier 4/3 in practice (Task 9 Step 4 already confirmed tiers 1-2 find nothing for all 7, and the corpus's tools are named directly in the transcript/caption — see the design note there). To get a real number for the pilot brief, force one post through the escalation path by using a copy of `DW1O6ZBEfDa`'s data with the tool name redacted from the caption/transcript, so `unnamed_tool.present` is genuinely true:

```bash
mkdir -p /tmp/phase4a-verify/extracted /tmp/phase4a-verify/media/FAKE01
python3 -c "
import json, re
d = json.load(open('extracted/DW1O6ZBEfDa.json'))
d['text'] = re.sub(r'Mem Palace|Mila Jovovich|Milla Jovovich', 'this tool', d['text'], flags=re.I)
json.dump(d, open('/tmp/phase4a-verify/extracted/FAKE01.json', 'w'))
info = json.load(open('media/DW1O6ZBEfDa/DW1O6ZBEfDa.info.json'))
info['description'] = re.sub(r'Mem Palace|Mila Jovovich|Milla Jovovich', 'this tool', info['description'], flags=re.I)
json.dump(info, open('/tmp/phase4a-verify/media/FAKE01/FAKE01.info.json', 'w'))
"
time python3 scripts/interpret.py FAKE01 --media-root /tmp/phase4a-verify/media \
  --extracted-root /tmp/phase4a-verify/extracted --staging-root /tmp/phase4a-verify/staging \
  > /tmp/phase4a-verify/FAKE01.json
python3 -c "print(__import__('json').load(open('/tmp/phase4a-verify/FAKE01.json'))['agent_calls'])"
```

Record this `agent_calls` count and wall-clock next to the single-call cost from Step 2 — Task 13 needs "the observed agent-call cost of a post that escalates to tier 4 versus one that stops at agent call #1" as a stated number, not a guess.

- [ ] **Step 6: Clean up the scratch verification artifacts**

```bash
rm -rf /tmp/phase4a-verify
```

(Real `staging/` under the repo is untouched by this task — every run above used `--staging-root /tmp/...`, deliberately, so this task doesn't create real pending proposals that would confuse Task 13's drain check.)

---

### Task 13: Backfill sweep for real, pilot brief, and session close-out

**Files:**
- Modify: `PLAN.md` (mark Phase 4a done)
- Modify: `docs/superpowers/handoffs/SESSION-3-pilot.md`
- No code changes expected

- [ ] **Step 1: Check the 49-job drain's real progress**

```bash
for d in queued running done failed; do echo "$d: $(ls jobs/$d 2>/dev/null | wc -l)"; done
pgrep -af "worker.py --once" || echo "no drain process running"
```

Report the counts and, if still running, estimate the finish time from the observed per-job duration in this session's earlier facts (~28 min/job for audio, faster for image posts) — PLAN.md already has a running estimate to update, don't recompute from scratch if the counts haven't moved much.

- [ ] **Step 2: Run the real backfill sweep against everything the drain has produced so far**

```bash
python3 scripts/interpret.py --backfill-sweep
cat staging/*/proposal.json | python3 -c "
import json, sys
for line in sys.stdin:
    pass
" 2>/dev/null || true
ls staging/ | wc -l
```

This is the "without it the 49 drained posts sit unprocessed" gap the handoff called out — every `extracted/*.json` with no `staging/<shortcode>/` yet (the original 7 already have one from Task 12 only if you reused the real `staging/` root there; Task 12 deliberately used `/tmp/...` so the real `staging/` is still empty for all of them) gets interpreted now, for real, against whichever backend `config/agent.json` currently names. Expect one Telegram digest message to arrive, not one message per post.

- [ ] **Step 3: Re-run the backfill sweep to confirm it's a no-op**

```bash
python3 scripts/interpret.py --backfill-sweep
```

Expected: `{"interpreted": [], "failed": [...]}` (only items that genuinely fail stay unstaged; nothing already staged is redone) — this is the "must be safe to re-run" requirement from the handoff's scope, proven against real state, not just Task 10's unit test.

- [ ] **Step 4: Write the pilot brief into `docs/superpowers/handoffs/SESSION-3-pilot.md`**

Read the current file first, then rewrite it with real numbers filled in for every placeholder the handoff's "End this session by writing the pilot brief" section demanded:

1. Wall-clock and agent-call count for one interpret run (Task 12 Step 2's `time` output + `agent_calls` field).
2. Agent-call cost of a tier-4-escalating post vs. one that stops at call #1 (Task 12 Step 5's `FAKE01` result vs. Step 2's real single-call posts).
3. How many posts to send for the pilot and what mix of creators/content types would stress the registry hardest — the drained 49 are all `@chase.h.ai`; the 7-post design corpus already spans `@networkchuck` (3), `@chase.h.ai` (1), `@pnaiplus` (1), `@ynetgram` (1) and one Apple-official image post, so recommend Dan send a deliberately different mix: at least one carousel from a non-Hebrew creator (the corpus has zero), one post that genuinely hides its tool name behind "comment X" (the corpus has zero — every post in it names its tool directly, per Task 9 Step 4's finding), and one post with a real creator-authored comment link (also zero in the corpus) so tiers 1/2 get real exercise instead of always falling through to the agent.
4. How long the pilot will take given Task 12's measured numbers (posts × wall-clock estimate).
5. What got built this session (list Tasks 1-12's deliverables), what got deferred (Phase 4b's ~19 remaining handlers; the daily watchlist walk, weekly digest and `while True` daemon loop are Phase 5b; Google Calendar MCP integration for `calendar_event`, deferred per Task 6's note; `shell_snippet`'s "Keep both" rough edge, Task 5's note), and any handler that turned out shakier than expected during Task 12's live verification (fill in from what actually happened — do not claim "everything worked perfectly" unless it genuinely did).
6. The real drain progress/ETA from Step 1.

- [ ] **Step 5: Update `PLAN.md`'s Phase 4 section**

Change the `### Phase 4 — Interpret + staging gate — 🔨 DESIGN DONE 2026-08-30, plan pending` heading to `— ✅ Phase 4a DONE 2026-08-31` (adjust the date to whatever today's date is at execution time) and add a results block underneath, matching Phases 0-3's style: test count before/after (75 → final count), which of the 7 pass-bar rows passed on the first try vs. needed a fix (from Task 12 Step 3), the backend-parity result (Task 12 Step 4), the tier-4 escalation cost measurement (Task 12 Step 5), and the real backfill-sweep result (Task 13 Steps 2-3: how many posts, what categories, any failures). Note explicitly that Phase 4b (~19 more handlers) and Phase 5b (daemon, digest, watchlist walk) remain, per the session-chain table already in §7.

- [ ] **Step 6: Final full-suite run and commit**

```bash
python3 -m pytest scripts/ -v
```

Expected: full suite green, test count matches what Step 5's PLAN.md update claims.

```bash
git add PLAN.md docs/superpowers/handoffs/SESSION-3-pilot.md
git commit -m "docs: Phase 4a done -- pilot brief with real measurements, drain status

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** design spec Component 1 (agents_lib) → Task 1. Component 2 (registry) → Task 2. Component 3 (resolver) → Task 9. Component 4 (interpret.py) → Task 10. Component 5 (staging_lib) → Task 8. Component 6 (install_artifact) → Task 7. "Changes to existing scripts" → Task 11. "The action registry — initial contents" (the 10 corpus handlers) → Tasks 3-6. "Where knowledge actions install" (captured-knowledge skill) → Task 3. "Risk tiers" → every handler's `RISK` constant + the exec-tier preview requirement threaded through Tasks 3-6 and tested per-handler. "Data / schema additions" → `config/agent.json` (Task 1), `staging/`/`logs/` paths (Tasks 3, 8, 10). "Approval & install flow" (buttons, collisions, reject) → Task 11 + Task 7's collision resolution. "The pilot loop" (`unsupported`) → Task 3. "Error handling" → Task 10's `call_agent_for_json`/`_write_agent_error`. "Scope boundary" respected: no systemd/daemon/digest-schedule code added (Phase 5b). "Testing" section's every bullet has a task and named tests (cross-referenced per-task above). The handoff's "backfill sweep" requirement → Task 10 + Task 13 Steps 2-3.
- **No placeholders:** every step has complete, real code. The two genuinely deferred items (Google Calendar MCP for `calendar_event`, `shell_snippet`'s "Keep both" edge case) are called out explicitly in the relevant task's own text, not hidden, and again in Task 13's pilot brief.
- **Type/name consistency checked:** every handler's `install(payload, target, context=None)` signature matches `install_artifact.install_action`'s call in Task 7 and the auto-discard/unsupported inline calls in Task 10's `interpret_post`. `staging_lib.update_action_status`/`staging_dir`/`write_proposal`/`read_proposal` signatures match every caller across Tasks 7, 10, 11. `registry.validate_actions`/`validate_action`/`validate_payload`/`get_handler`/`generate_prompt_catalogue` all take the same optional `registry_dict=None` parameter, used consistently by Task 2's own tests and left at its default (the real `REGISTRY`) by every production caller in Tasks 7/10/11. `agents_lib.run_agent`'s `schema` kwarg matches `interpret.py`'s `call_agent_for_json` passing `ACTION_LIST_SCHEMA`/`SEARCH_SCHEMA`. `resolve_tools.resolve_via_web_search`'s `extract_json_fn` parameter matches `interpret.py` passing `agents_lib.extract_json`.
- **Ambiguity resolved, not deferred:** the design spec's six-vs-seven-member handler table discrepancy is resolved by Task 2's explicit 8-member contract (adding `target_path` and documenting why). `calendar_event`'s destination (no path given in the spec beyond "Google Calendar MCP when authenticated") is resolved to `<repo>/knowledge/calendar/` with the deferral stated plainly. `shell_snippet`'s target not fitting the general allowlist-of-roots shape is resolved with a small `ALLOWED_FILES` exact-match list in Task 7, justified by the target being hardcoded rather than agent-drafted.

