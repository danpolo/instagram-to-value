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
