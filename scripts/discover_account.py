#!/usr/bin/env python3
"""Phase 3 account discovery & capped backfill -- PLAN.md sec 4.5. Ingesting
any post auto-enrolls its creator: on first sight of a username, list their
recent posts (authenticated, throttled) and enqueue up to posts_cap/days_cap
of them, whichever cap hits first. Existence of pages/<username>.json with
backfill.done=true means "watched" -- no separate watchlist file.

Enumeration mechanism verified live 2026-08-30 against @networkchuck (see the
Phase 3 design spec): `gallery-dl <profile>/posts/ --post-range 1-N -j`
returns a JSON array of [type_code, metadata] entries; type==2 entries are
posts carrying post_shortcode/post_date/owner_id/username, newest-first.
--sleep-request throttles gallery-dl's internal paging requests -- same
delay+jitter window scan_carousel.py already uses (PLAN.md sec 4.5: "a
trusted backfill is not an exemption from throttling").

Usage:
    python3 scripts/discover_account.py <username> [--posts-cap N] [--days-cap N]
        [--pages-root DIR] [--jobs-root DIR] [--cookies PATH]

Prints the pages/<username>.json record to stdout (or {"status": "already
watched"} if it was a no-op).
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobs_lib import find_job, write_job

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAGES_ROOT = REPO_ROOT / "pages"
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"
DEFAULT_COOKIES = Path.home() / ".config" / "instagram" / "cookies.txt"
DEFAULT_POSTS_CAP = 50
DEFAULT_DAYS_CAP = 90
SLEEP_REQUEST_RANGE = "8-18"  # matches scan_carousel.py's throttle window


def should_stop_backfill(index, post_date, first_seen, posts_cap, days_cap):
    """Pure: True once EITHER cap is hit (PLAN.md sec 4.5 -- 'whichever cap is
    hit first stops the backfill'). index is the 0-based count of posts
    already kept before considering this one; post_date/first_seen are
    datetime objects."""
    if index >= posts_cap:
        return True
    age_days = (first_seen - post_date).days
    return age_days > days_cap


def parse_gallery_dl_posts(raw_json_text):
    """Pure: extract post entries from `gallery-dl <profile>/posts/ -j`
    output. Entries are [type_code, payload]; type 2 is a post, type 3 is a
    raw media URL (skipped). Returns newest-first dicts with only the fields
    the backfill needs."""
    raw = json.loads(raw_json_text)
    posts = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        type_code, payload = entry[0], entry[1]
        if type_code != 2 or not isinstance(payload, dict):
            continue
        shortcode = payload.get("post_shortcode")
        date_str = payload.get("post_date")
        if not shortcode or not date_str:
            continue
        posts.append({
            "shortcode": shortcode,
            "date": date_str,
            "owner_id": payload.get("owner_id"),
            "username": payload.get("username"),
        })
    return posts


def select_backfill_shortcodes(posts, first_seen, posts_cap, days_cap):
    """Pure: PLAN.md sec 4.5 step 3 -- walk newest-first posts, stop at
    whichever cap hits first. Dedupes by shortcode (a carousel post can emit
    more than one type==2-adjacent entry upstream) while preserving
    newest-first order."""
    seen = set()
    kept = []
    for post in posts:
        shortcode = post["shortcode"]
        if shortcode in seen:
            continue
        seen.add(shortcode)
        post_date = datetime.strptime(post["date"], "%Y-%m-%d %H:%M:%S")
        if should_stop_backfill(len(kept), post_date, first_seen, posts_cap, days_cap):
            break
        kept.append(post)
    return kept


def build_page_record(username, first_seen_iso, posts_cap=DEFAULT_POSTS_CAP, days_cap=DEFAULT_DAYS_CAP):
    """Pure: the pages/<username>.json shape from PLAN.md sec 4.5 step 2."""
    return {
        "username": username,
        "first_seen": first_seen_iso,
        "backfill": {"posts_cap": posts_cap, "days_cap": days_cap, "done": False},
        "last_checked": None,
        "newest_known_shortcode": None,
    }


def enroll_if_new(username, pages_root=DEFAULT_PAGES_ROOT, jobs_root=DEFAULT_JOBS_ROOT,
                   cookies=DEFAULT_COOKIES, posts_cap=DEFAULT_POSTS_CAP, days_cap=DEFAULT_DAYS_CAP):
    """Orchestrator (PLAN.md sec 4.5 steps 2-4). Returns None (no-op) if
    already watched, else the written page record. Best-effort on the
    gallery-dl call: a listing failure leaves backfill.done=False (a future
    re-run of this script for the same username would need the page file
    removed first -- no untrack/retry mechanism exists yet, an accepted §4.5
    open item) but still records first_seen so the account isn't silently lost."""
    pages_root = Path(pages_root)
    page_path = pages_root / f"{username}.json"
    if page_path.exists():
        return None

    now = datetime.now(timezone.utc)
    page = build_page_record(username, now.isoformat(), posts_cap, days_cap)
    pages_root.mkdir(parents=True, exist_ok=True)
    page_path.write_text(json.dumps(page, indent=2, ensure_ascii=False))

    cmd = [
        "gallery-dl", "--cookies", str(cookies),
        "--post-range", f"1-{posts_cap}",
        "--sleep-request", SLEEP_REQUEST_RANGE,
        "-j",
        f"https://www.instagram.com/{username}/posts/",
    ]
    print(f"[discover_account] $ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[discover_account] WARNING: gallery-dl listing failed for {username}, "
              f"backfill left incomplete:\n{result.stderr[-800:]}", file=sys.stderr)
        return page

    posts = parse_gallery_dl_posts(result.stdout)
    # select_backfill_shortcodes/should_stop_backfill compare against
    # post_date, which gallery-dl reports timezone-naive -- drop tzinfo from
    # `now` for this comparison only (the page record below still stores the
    # real UTC-aware ISO timestamp). Found live: aware-minus-naive raises
    # TypeError.
    kept = select_backfill_shortcodes(posts, now.replace(tzinfo=None), posts_cap, days_cap)

    queued = []
    for post in kept:
        sc = post["shortcode"]
        state, _ = find_job(sc, jobs_root)
        if state is not None:
            continue
        write_job(sc, jobs_root, "queued", url=f"https://www.instagram.com/p/{sc}/",
                  chat_id=None, requested_at=now.isoformat(), source="backfill")
        queued.append(sc)

    page["backfill"]["done"] = True
    page["last_checked"] = now.isoformat()
    if kept:
        page["newest_known_shortcode"] = kept[0]["shortcode"]
    page_path.write_text(json.dumps(page, indent=2, ensure_ascii=False))
    print(f"[discover_account] enrolled {username}: {len(queued)} backfill jobs queued "
          f"(of {len(kept)} posts kept, {len(posts)} listed)", file=sys.stderr)
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--posts-cap", type=int, default=DEFAULT_POSTS_CAP)
    ap.add_argument("--days-cap", type=int, default=DEFAULT_DAYS_CAP)
    ap.add_argument("--pages-root", default=str(DEFAULT_PAGES_ROOT))
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--cookies", default=str(DEFAULT_COOKIES))
    args = ap.parse_args()

    page = enroll_if_new(args.username, pages_root=args.pages_root, jobs_root=args.jobs_root,
                          cookies=args.cookies, posts_cap=args.posts_cap, days_cap=args.days_cap)
    print(json.dumps(page if page is not None else {"status": "already watched"},
                      indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
