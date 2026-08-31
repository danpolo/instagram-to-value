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
