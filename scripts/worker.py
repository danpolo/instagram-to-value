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
from extract import get_available_ram_gb, check_ram_guard, MIN_RAM_GB_FOR_ASR
from jobs_lib import list_queued, move_job
from telegram_notify import send as notify
import telegram_notify
import staging_lib
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import registry

registry.load_all_handlers()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"
DEFAULT_MEDIA_ROOT = REPO_ROOT / "media"
DEFAULT_EXTRACTED_ROOT = REPO_ROOT / "extracted"
DEFAULT_PAGES_ROOT = REPO_ROOT / "pages"
DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"
DEFAULT_LOGS_ROOT = REPO_ROOT / "logs"
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
        # `channel` is yt-dlp's actual lowercase handle -- matches the URL
        # slug gallery-dl's image-track sidecar reports as "username".
        # `uploader_id` is frequently numeric and `uploader` can be a display
        # name in different casing than the real handle; found live
        # 2026-08-30: a post's uploader_id was the numeric account ID, which
        # enrolled a bogus duplicate pages/<id>.json instead of recognizing
        # the already-watched account. Lowercased so both tracks always
        # produce the same key for the same account.
        username = info.get("channel") or info.get("uploader_id") or info.get("uploader")
        if username:
            return str(username).lower()

    for meta_path in sorted(media_dir.glob(f"{shortcode}_*.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        username = meta.get("username") or meta.get("owner_id")
        if username:
            return str(username).lower()
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


def safe_notify(text, chat_id, notify_fn=notify):
    """Send a progress notification, degrading to a logged warning instead of
    raising (PLAN.md sec 9 open item #7). telegram_notify.send() itself keeps
    its fail-loud contract -- that split is deliberate: send() stays honest
    for anyone calling it directly, this wrapper is what the *worker*
    specifically needs, because a transient Telegram blip must not kill a
    23-hour unattended drain. Open item #9: the call before fetch is exactly
    what orphaned jobs before this fix + requeue_orphans() landed."""
    try:
        notify_fn(text, chat_id=chat_id)
    except Exception as e:
        print(f"[worker] WARNING: notify failed, continuing: {e}", file=sys.stderr)


def build_proposal_buttons(actions):
    """None if nothing is left to decide (e.g. every action auto-discarded)
    -- design spec's approval-flow button row for a manual-origin proposal."""
    if not any(a["status"] == "pending" for a in actions):
        return None
    return [[("✅ All", "approve_all:{shortcode}"), ("☑️ Pick…", "pick:{shortcode}")],
             [("📄 Show full", "show:{shortcode}"), ("❌ Discard", "discard:{shortcode}")]]


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


def process_job(shortcode, url, chat_id, jobs_root, media_root, extracted_root, pages_root,
                  staging_root=DEFAULT_STAGING_ROOT, source="telegram"):
    def note(text):
        safe_notify(text, chat_id)

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

    move_job(shortcode, jobs_root, "running", "done", done_at=datetime.now(timezone.utc).isoformat())
    note(f"✅ {shortcode} done — {extracted.get('media_type', '?')}, {text_len} chars{enrolled_note}")

    if proposal and source == "telegram":
        notify_proposal_now(proposal, chat_id, staging_root)


def requeue_orphans(jobs_root):
    """Scan jobs/running/ and move anything found back to queued/ -- a job
    sitting there means the previous run died mid-flight (OOM, reboot, or a
    notify error before bug #7's fix landed -- PLAN.md sec 9 open item #8).
    Jobs are idempotent by design (PLAN.md sec 6), so a replay is safe.
    Called once at worker startup, before the drain loop."""
    jobs_root = Path(jobs_root)
    running_dir = jobs_root / "running"
    if not running_dir.exists():
        return
    for job_path in sorted(running_dir.glob("*.json")):
        shortcode = job_path.stem
        try:
            move_job(shortcode, jobs_root, "running", "queued")
            print(f"[worker] requeued orphaned job {shortcode}", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[worker] WARNING: could not requeue orphaned job {job_path}: {e}", file=sys.stderr)


def drain_once(jobs_root, media_root, extracted_root, pages_root, staging_root=DEFAULT_STAGING_ROOT,
                available_ram_gb_fn=get_available_ram_gb, process_job_fn=process_job):
    for shortcode in list_queued(jobs_root):
        job_path = Path(jobs_root) / "queued" / f"{shortcode}.json"
        try:
            job = json.loads(job_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[worker] WARNING: unreadable job {job_path}: {e}", file=sys.stderr)
            continue
        available_gb = available_ram_gb_fn()
        if not check_ram_guard(available_gb):
            print(f"[worker] deferring {shortcode}: only {available_gb:.1f}GB available, "
                  f"need >={MIN_RAM_GB_FOR_ASR}GB (PLAN.md sec 8 risk 1) -- will retry next drain",
                  file=sys.stderr)
            continue
        move_job(shortcode, jobs_root, "queued", "running")
        # jobs missing 'source' default to "telegram" (design spec: discover_account.py's
        # backfill jobs write source="backfill"; older/hand-written jobs have neither).
        process_job_fn(shortcode, job.get("url") or f"https://www.instagram.com/p/{shortcode}/",
                        job.get("chat_id"), jobs_root, media_root, extracted_root, pages_root,
                        staging_root=staging_root, source=job.get("source", "telegram"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--media-root", default=str(DEFAULT_MEDIA_ROOT))
    ap.add_argument("--extracted-root", default=str(DEFAULT_EXTRACTED_ROOT))
    ap.add_argument("--pages-root", default=str(DEFAULT_PAGES_ROOT))
    ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    ap.add_argument("--once", action="store_true", help="Drain whatever is queued now and exit")
    args = ap.parse_args()

    requeue_orphans(args.jobs_root)

    while True:
        drain_once(args.jobs_root, args.media_root, args.extracted_root, args.pages_root,
                    staging_root=args.staging_root)
        if args.once:
            return
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
