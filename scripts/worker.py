#!/usr/bin/env python3
"""Phase 3 job runner: drains jobs/queued/, running fetch.py -> extract.py,
enrolling the creator's account (PLAN.md sec 4.5), and notifying Telegram on
each stage transition. Foreground loop -- wrap in tmux/systemd yourself
(a systemd unit is Phase 5, see PLAN.md sec 7).

Usage:
    python3 scripts/worker.py [--jobs-root DIR] [--media-root DIR]
        [--extracted-root DIR] [--pages-root DIR] [--poll-interval SECONDS]
        [--once]

Runs forever, polling jobs/queued/ every --poll-interval seconds (default 15).
--once drains whatever is queued right now and exits (used for testing).
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_account import enroll_if_new
from jobs_lib import list_queued, move_job
from telegram_notify import send as notify

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"
DEFAULT_MEDIA_ROOT = REPO_ROOT / "media"
DEFAULT_EXTRACTED_ROOT = REPO_ROOT / "extracted"
DEFAULT_PAGES_ROOT = REPO_ROOT / "pages"
DEFAULT_POLL_INTERVAL = 15


def resolve_creator_username(media_dir: Path, shortcode: str):
    """File IO only, no network: pull the creator's username out of whichever
    metadata fetch.py already wrote -- PLAN.md sec 4.5 step 1 says this is
    'already available for free'. Video track: <shortcode>.info.json's
    uploader_id/uploader. Image track: a slide's gallery-dl sidecar JSON's
    username/owner_id. Returns None if neither is present/parseable."""
    media_dir = Path(media_dir)
    info_json = media_dir / f"{shortcode}.info.json"
    if info_json.exists():
        try:
            info = json.loads(info_json.read_text())
        except (json.JSONDecodeError, OSError):
            info = {}
        username = info.get("uploader_id") or info.get("uploader")
        if username:
            return username

    for meta_path in sorted(media_dir.glob(f"{shortcode}_*.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        username = meta.get("username") or meta.get("owner_id")
        if username:
            return username
    return None


def run_subprocess(cmd, label):
    """Run a scripts/*.py CLI; return (ok, stdout, stderr). Never raises --
    the caller (process_job) decides what a subprocess failure means for the
    job's state, unlike extract.py's internal subprocess calls which fail the
    whole process immediately."""
    print(f"[worker] $ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0
    if not ok:
        print(f"[worker] {label} FAILED:\n{result.stderr[-2000:]}", file=sys.stderr)
    return ok, result.stdout, result.stderr


def process_job(shortcode, url, chat_id, jobs_root, media_root, extracted_root, pages_root):
    def note(text):
        notify(text, chat_id=chat_id)

    note(f"⏳ fetching {shortcode}")
    ok, _, err = run_subprocess(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch.py"), url, "--media-root", str(media_root)],
        "fetch",
    )
    if not ok:
        move_job(shortcode, jobs_root, "running", "failed",
                  error=err[-2000:], failed_at=datetime.now(timezone.utc).isoformat())
        note(f"❌ {shortcode} failed at fetch")
        return

    note(f"⏳ extracting {shortcode}")
    ok, out, err = run_subprocess(
        [sys.executable, str(REPO_ROOT / "scripts" / "extract.py"), shortcode,
         "--media-root", str(media_root), "--extracted-root", str(extracted_root)],
        "extract",
    )
    if not ok:
        move_job(shortcode, jobs_root, "running", "failed",
                  error=err[-2000:], failed_at=datetime.now(timezone.utc).isoformat())
        note(f"❌ {shortcode} failed at extract")
        return

    media_dir = Path(media_root) / shortcode
    username = resolve_creator_username(media_dir, shortcode)
    enrolled_note = ""
    if username:
        try:
            page = enroll_if_new(username, pages_root=pages_root, jobs_root=jobs_root)
            if page is not None:
                enrolled_note = f" (enrolled new account @{username} for backfill)"
        except Exception as e:  # best-effort: a backfill problem must never fail an otherwise-successful ingest
            print(f"[worker] WARNING: enroll_if_new({username!r}) failed: {e}", file=sys.stderr)

    try:
        extracted = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        extracted = {}
    text_len = len(extracted.get("text") or "")
    move_job(shortcode, jobs_root, "running", "done", done_at=datetime.now(timezone.utc).isoformat())
    note(f"✅ {shortcode} done — {extracted.get('media_type', '?')}, {text_len} chars{enrolled_note}")


def drain_once(jobs_root, media_root, extracted_root, pages_root):
    for shortcode in list_queued(jobs_root):
        job_path = Path(jobs_root) / "queued" / f"{shortcode}.json"
        try:
            job = json.loads(job_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[worker] WARNING: unreadable job {job_path}: {e}", file=sys.stderr)
            continue
        move_job(shortcode, jobs_root, "queued", "running")
        process_job(shortcode, job.get("url") or f"https://www.instagram.com/p/{shortcode}/",
                    job.get("chat_id"), jobs_root, media_root, extracted_root, pages_root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--media-root", default=str(DEFAULT_MEDIA_ROOT))
    ap.add_argument("--extracted-root", default=str(DEFAULT_EXTRACTED_ROOT))
    ap.add_argument("--pages-root", default=str(DEFAULT_PAGES_ROOT))
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    ap.add_argument("--once", action="store_true", help="Drain whatever is queued now and exit")
    args = ap.parse_args()

    while True:
        drain_once(args.jobs_root, args.media_root, args.extracted_root, args.pages_root)
        if args.once:
            return
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
