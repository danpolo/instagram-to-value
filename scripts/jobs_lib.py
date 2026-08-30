#!/usr/bin/env python3
"""Shared job-file helpers for jobs/{queued,running,done,failed}/<shortcode>.json
(PLAN.md sec 6). Both telegram_bot.py (writer) and worker.py (state machine)
use this so the on-disk contract lives in one place instead of two."""
import json
from pathlib import Path

STATES = ("queued", "running", "done", "failed")


def find_job(shortcode, jobs_root):
    """Return (state, path) for the state dir that currently holds this
    shortcode's job file, or (None, None) if it isn't tracked anywhere yet.
    This is the idempotency check PLAN.md sec 6 requires: 'a re-sent URL is a
    no-op'."""
    jobs_root = Path(jobs_root)
    for state in STATES:
        path = jobs_root / state / f"{shortcode}.json"
        if path.exists():
            return state, path
    return None, None


def write_job(shortcode, jobs_root, state, **fields):
    """Create a new job file in the given state dir. Raises FileExistsError if
    one already exists there -- callers must call find_job() first to make
    the idempotency check explicit at the call site, not implicit here."""
    jobs_root = Path(jobs_root)
    dir_path = jobs_root / state
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{shortcode}.json"
    if path.exists():
        raise FileExistsError(f"job already exists: {path}")
    path.write_text(json.dumps({"shortcode": shortcode, **fields}, indent=2, ensure_ascii=False))
    return path


def move_job(shortcode, jobs_root, from_state, to_state, **extra_fields):
    """Move a job file between state dirs, merging extra_fields into it (e.g.
    an error message on the way to failed/). Returns the new path."""
    jobs_root = Path(jobs_root)
    src = jobs_root / from_state / f"{shortcode}.json"
    if not src.exists():
        raise FileNotFoundError(f"no job at {src}")
    data = json.loads(src.read_text())
    data.update(extra_fields)
    dst_dir = jobs_root / to_state
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{shortcode}.json"
    dst.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    src.unlink()
    return dst


def list_queued(jobs_root):
    """Shortcodes currently in jobs/queued/, oldest file first (mtime) so the
    worker drains in roughly FIFO order."""
    jobs_root = Path(jobs_root)
    queued_dir = jobs_root / "queued"
    if not queued_dir.exists():
        return []
    files = sorted(queued_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return [p.stem for p in files]
