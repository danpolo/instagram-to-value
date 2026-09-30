#!/usr/bin/env python3
"""skill_install -- installs a published agent skill by `owner/repo`, or by name alone resolved against the skills.sh directory, via `npx skills add`.
RISK exec: it downloads and runs a third-party CLI. Distinct from `skill`,
which *writes* a SKILL.md we generated ourselves; this one pulls someone
else's. A post that names a skill without linking it is the common case --
four `unsupported: skill_install` rationales in the pilot were exactly that --
so `source` is optional and _skill_directory resolves the name on approval.

The install is two steps, because `npx skills add` knows nothing about this
machine's canonical skill repo: it copies into a throwaway staging directory,
then the downloaded skill folder is moved into ~/agent-skills/skills/ and
`agent-skills sync` links it into every agent. Installing it straight into
~/.claude/skills/ (what `--global` does) would leave a third-party skill
unversioned, Claude-only, and in a directory the next sync rearranges."""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _skill_directory
import _skills_root
from registry import register

TYPE = "skill_install"
RISK = "exec"
SCHEMA = {
    # `source` is optional: a caption that says "install the Impeccable skill"
    # and links nothing is still actionable, because the name resolves against
    # the skills.sh directory at approval time.
    "required": ["name"],
    "types": {"name": str, "source": str},
    # One canonical form: a bare `owner/repo`, which is exactly what
    # `npx skills add` resolves without a URL -- the thing the four
    # `unsupported: skill_install` rationales said the registry lacked.
    # Pinning it also fixes the force-fit's internal inconsistency, where
    # the same field held the CLI name ("skills") in one post and the skill
    # ("pbakaus/impeccable") in another, so no dedup keyed on it could match
    # (SESSION-4-phase4b-handlers.md, backlog item #1, proof #3).
    "patterns": {"source": r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"},
}
SKILLS_ROOT = _skills_root.SKILLS_ROOT
# `npx skills add` installs relative to the *current working directory*, and it
# is the handler's job to pin that. package_install's force-fit ran the same
# command with no cwd= and created scripts/.claude/skills/ inside this repo
# while still returning ok:true -- under worker.py the destination was simply
# wherever the worker happened to be started (backlog proof #2). Here cwd is a
# fresh temp dir per run, and the result is adopted into SKILLS_ROOT.
STAGING_PREFIX = "skill-install-"
# --copy, not the default symlink: the copy is about to be moved into the
# canonical repo and committed, so it must not be a link into npx's cache.
# `--agent claude-code` picks one layout to harvest from (<staging>/.claude/
# skills/<name>/); the skill is agent-neutral, and sync fans it out to the rest.
ADD_FLAGS = ["--copy", "--skill", "*", "--agent", "claude-code", "--yes"]
STAGED_SUBDIR = Path(".claude") / "skills"
INSTALL_TIMEOUT_S = 600


def _slug(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"


def target_path(payload):
    return SKILLS_ROOT / _slug(payload["name"])


def describe(payload):
    source = payload.get("source")
    if source:
        return f"Install skill `{payload['name']}` from {source}"
    # Offline: describe() renders every action in /pending, so it must not
    # spend a directory lookup per line.
    return f"Install skill `{payload['name']}` — resolved by name in the skills.sh directory"


def _command_line(source):
    return (f"npx --yes skills add {source} {' '.join(ADD_FLAGS)}"
            f" → {SKILLS_ROOT}/ (then agent-skills sync)")


def preview(payload):
    # Approval must show *where* it lands, not just the command: an install
    # that silently picks up the worker's cwd is the exact bug this replaces.
    source = payload.get("source")
    if source:
        return _command_line(source)
    # A name-only action is exec risk aimed at a repo nobody has named yet, so
    # "Show full" spends the lookup and shows the runners-up it beat.
    resolution = _skill_directory.resolve(payload["name"])
    lines = [f"`{payload['name']}` → skills.sh query `{resolution.get('query')}`: {resolution['status']}"]
    if resolution["status"] == "resolved":
        lines.append(_command_line(resolution["source"]))
    else:
        lines.append(_unresolved_error(payload["name"], resolution))
    if resolution["candidates"]:
        lines.append("candidates: " + "; ".join(resolution["candidates"]))
    return "\n".join(lines)


def _unresolved_error(name, resolution):
    """Pure: why a bare name could not become an owner/repo, with the evidence.
    Never guesses -- installing the top hit of a crowded, low-install name
    ("Claude Video": 13, 6 and 2 installs across three owners) would run a
    stranger's code under an action Dan approved for something else."""
    if resolution["candidates"]:
        return (f"skills.sh has no dominant match for {name!r} — closest: "
                + "; ".join(resolution["candidates"])
                + ". Re-send with an explicit owner/repo to install one.")
    detail = f" ({resolution['error']})" if resolution.get("error") else ""
    return (f"skills.sh lists no skill named {name!r}{detail}. "
            "Re-send with an explicit owner/repo.")


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def _adopt(staging, skills_root):
    """Move every skill the CLI copied into the canonical repo.

    Returns (adopted, skipped). A repo can publish several skills (pbakaus/
    impeccable ships `impeccable` and `polish`), and `--skill '*'` takes them
    all; one whose name is already canonical is skipped rather than
    overwritten -- replacing a skill is the approval flow's decision
    (install_artifact's Replace/Keep both), never a silent side effect of
    installing a different one."""
    adopted, skipped = [], []
    for staged in sorted((staging / STAGED_SUBDIR).glob("*")):
        if not (staged / "SKILL.md").is_file():
            continue
        destination = skills_root / staged.name
        if destination.exists():
            skipped.append(staged.name)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(destination))
        adopted.append(staged.name)
    return adopted, skipped


def install(payload, target=None, context=None):
    source = payload.get("source")
    notes = []
    if not source:
        resolution = _skill_directory.resolve(payload["name"])
        if resolution["status"] != "resolved":
            return {"ok": False, "path": None, "command": None, "exit_code": None,
                    "output": None, "note": None,
                    "error": _unresolved_error(payload["name"], resolution)}
        source = resolution["source"]
        # Reported back into the Telegram result line: an exec install that
        # says only "installed" hides whose repo just ran.
        notes.append(f"{payload['name']} → {source}@{resolution['skill_id']}"
                     f" ({resolution['installs']} installs, skills.sh)")
    command = ["npx", "--yes", "skills", "add", source] + ADD_FLAGS
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX))
    try:
        result = subprocess.run(command, cwd=str(staging), capture_output=True,
                                 text=True, timeout=INSTALL_TIMEOUT_S)
        adopted, skipped = _adopt(staging, SKILLS_ROOT)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if adopted:
        notes.append("adopted into " + str(SKILLS_ROOT) + ": " + ", ".join(adopted))
        sync_note = _skills_root.sync()
        if sync_note:
            notes.append(sync_note)
    if skipped:
        notes.append("already canonical, left untouched: " + ", ".join(skipped))

    error = None
    if result.returncode != 0:
        error = f"exit {result.returncode}"
    elif not adopted:
        # The CLI said ok but nothing reached the repo -- exactly the failure
        # mode that left a proposal marked installed with path:null and no
        # artifact anywhere on disk (PLAN.md open item #10).
        error = ("`skills add` exited 0 but left no SKILL.md in " + str(staging / STAGED_SUBDIR)
                 + (f" (all names already canonical: {', '.join(skipped)})" if skipped else ""))
    installed_path = SKILLS_ROOT / (adopted[0] if adopted else _slug(payload["name"]))
    # Prefer the skill that matches the action's own name when the repo
    # shipped several, so the recorded path is the one Dan approved.
    if _slug(payload["name"]) in adopted:
        installed_path = SKILLS_ROOT / _slug(payload["name"])
    return {
        "ok": error is None,
        "path": str(installed_path) if adopted else None,
        "command": f"cd {staging} && {' '.join(command)}",
        "note": "; ".join(notes) or None,
        "exit_code": result.returncode,
        # skills.sh reports a per-skill risk rating ("Med Risk", "0 alerts")
        # that is worth keeping in the record, so capture stdout too.
        "output": (result.stdout + result.stderr)[-2000:],
        "error": error,
    }


register(sys.modules[__name__])
