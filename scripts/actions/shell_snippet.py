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
