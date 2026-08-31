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
