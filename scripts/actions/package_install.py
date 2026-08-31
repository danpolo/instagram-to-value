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
