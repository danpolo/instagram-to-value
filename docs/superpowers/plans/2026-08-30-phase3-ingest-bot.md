# Phase 3 — Ingest Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A URL sent to a dedicated Telegram bot becomes a queued job with an ack within 2s; a separate worker drains the queue (fetch → extract), sends progress notifications, and auto-enrolls the post's creator for a capped, throttled backfill.

**Architecture:** Two decoupled, independently-runnable processes talking only through `jobs/{queued,running,done,failed}/<shortcode>.json` on disk. `telegram_bot.py` long-polls Telegram and only ever writes a queued job file + acks. `worker.py` is a separate foreground sleep-poll loop that drains the queue, shells out to the existing `fetch.py`/`extract.py` CLIs (same subprocess pattern `extract.py` already uses), and sends its own Telegram messages via a plain send-only `Bot` call — no shared process, no shared event loop.

**Tech Stack:** `python-telegram-bot` v22.7 (already installed, confirmed via `pip show`) for both the polling bot and the send-only notifier; stdlib `subprocess`/`json`/`argparse` for the worker and account-discovery scripts (matches `fetch.py`/`extract.py` conventions); `pytest` for pure-logic unit tests, no mocking of Telegram/network.

**Spec:** `/home/dan/projects/instagram-to-value/docs/superpowers/specs/2026-08-30-phase3-ingest-bot-design.md`, which implements `/home/dan/projects/instagram-to-value/PLAN.md` §7 Phase 3, §4.5, §6, §0 Correction 3.

## Global Constraints

- Fail loud on real failures; a failed job moves to `jobs/failed/` with the error attached and the worker moves on — **no auto-retry** (design spec's "Failure handling"; also PLAN.md §9, re-hammering a rate-limited session is what got the IG account flagged before).
- `jobs/queued/`, `jobs/running/`, `jobs/done/`, `jobs/failed/`, `pages/` are all already gitignored (existing `.gitignore` `jobs/`/`pages/` lines) — nothing under them is ever committed.
- Idempotency: a shortcode already present in *any* `jobs/*` state is never re-queued (PLAN.md §6, "a re-sent URL is a no-op").
- Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID`) already exist at `~/.config/instagram-to-value/secrets.env` (mode 600) — read via the existing `scripts/secrets_lib.load_secret`, never hardcoded.
- Reuse, don't duplicate: shortcode extraction is `fetch.extract_shortcode()`, imported.
- No systemd unit in this phase (Phase 5's job) — `worker.py`/`telegram_bot.py` run under `tmux`/`nohup`/foreground for now.
- Backfill enumeration mechanism is **verified**, not speculative: `gallery-dl <profile>/posts/ --post-range 1-N -j --sleep-request 8-18` was run live against `@networkchuck` during design and returns `[type_code, metadata]` entries, `type==2` = a post with `post_shortcode`/`post_date`/`owner_id`/`username`, newest-first. The fixture in Task 3 is trimmed from that real output.

---

### Task 1: `jobs_lib.py` — shared job-file state machine

**Files:**
- Create: `scripts/jobs_lib.py`
- Test: `scripts/test_ingest.py` (new file, started here)

**Interfaces:**
- Produces: `find_job(shortcode, jobs_root) -> (state: str|None, path: Path|None)`, `write_job(shortcode, jobs_root, state, **fields) -> Path` (raises `FileExistsError` if already tracked in that state dir), `move_job(shortcode, jobs_root, from_state, to_state, **extra_fields) -> Path`, `list_queued(jobs_root) -> list[str]` (shortcodes, oldest-queued-first).

- [ ] **Step 1: Write `scripts/jobs_lib.py`**

```python
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
```

- [ ] **Step 2: Write `scripts/test_ingest.py` (first slice)**

```python
"""Pure-logic unit tests for the Phase 3 ingest layer. Run with:
    python3 -m pytest scripts/test_ingest.py -v
No live Telegram/network calls -- those live inside functions these tests
don't invoke (matches test_extract.py's convention)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs_lib


def test_find_job_untracked_returns_none(tmp_path):
    assert jobs_lib.find_job("ABC123", tmp_path) == (None, None)


def test_write_job_then_find_job(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    state, path = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "queued"
    data = json.loads(path.read_text())
    assert data == {"shortcode": "ABC123", "url": "https://x/p/ABC123/"}


def test_write_job_raises_if_already_exists(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued")
    try:
        jobs_lib.write_job("ABC123", tmp_path, "queued")
        assert False, "expected FileExistsError"
    except FileExistsError:
        pass


def test_move_job_merges_fields_and_removes_source(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    dst = jobs_lib.move_job("ABC123", tmp_path, "queued", "running")
    assert not (tmp_path / "queued" / "ABC123.json").exists()
    assert dst == tmp_path / "running" / "ABC123.json"
    data = json.loads(dst.read_text())
    assert data["url"] == "https://x/p/ABC123/"

    dst2 = jobs_lib.move_job("ABC123", tmp_path, "running", "failed", error="boom")
    assert not dst.exists()
    data2 = json.loads(dst2.read_text())
    assert data2["error"] == "boom"
    assert data2["url"] == "https://x/p/ABC123/"


def test_move_job_missing_source_raises(tmp_path):
    try:
        jobs_lib.move_job("NOPE", tmp_path, "queued", "running")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_list_queued_empty_when_dir_missing(tmp_path):
    assert jobs_lib.list_queued(tmp_path) == []


def test_list_queued_oldest_first(tmp_path):
    import time
    jobs_lib.write_job("FIRST", tmp_path, "queued")
    time.sleep(0.01)
    jobs_lib.write_job("SECOND", tmp_path, "queued")
    assert jobs_lib.list_queued(tmp_path) == ["FIRST", "SECOND"]
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_ingest.py -v
```

Expected: 7 passed.

- [ ] **Step 4: Commit**

```bash
cd /home/dan/projects/instagram-to-value
git add scripts/jobs_lib.py scripts/test_ingest.py
git commit -m "feat: add jobs_lib job-state helpers for Phase 3

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `telegram_notify.py` — send-only Telegram helper

**Files:**
- Create: `scripts/telegram_notify.py`

**Interfaces:**
- Consumes: `secrets_lib.load_secret` (existing)
- Produces: `send(text, chat_id=None, secrets_path=DEFAULT_SECRETS) -> None` — fails loudly (`SystemExit`) if the token/chat id aren't configured or the send fails.

- [ ] **Step 1: Write `scripts/telegram_notify.py`**

```python
#!/usr/bin/env python3
"""Send-only Telegram helper for worker.py's progress notifications. Does not
poll -- telegram_bot.py owns the long-poll loop; this just posts one message
via a fresh Bot instance per call. See PLAN.md sec 7 Phase 3 ("Progress
notifications on stage transitions").

Usage (as a library):
    from telegram_notify import send
    send("fetching DW1O6ZBEfDa")

CLI (for manual testing):
    python3 scripts/telegram_notify.py "some message" [--chat-id ID]
"""
import argparse
import asyncio
import sys
from pathlib import Path

from telegram import Bot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret


async def _send_async(text, token, chat_id):
    async with Bot(token) as bot:
        await bot.send_message(chat_id=chat_id, text=text)


def send(text, chat_id=None, secrets_path=DEFAULT_SECRETS):
    """Fire-and-wait send of one Telegram message from synchronous code.
    Fails loudly rather than silently dropping a progress notification -- a
    worker that can't notify should be noticed, not silently degraded."""
    token = load_secret("TELEGRAM_BOT_TOKEN", secrets_path)
    if not token:
        raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_BOT_TOKEN not set in {secrets_path}")
    if chat_id is None:
        chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", secrets_path)
        if not chat_id:
            raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {secrets_path}")
    asyncio.run(_send_async(text, token, chat_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--chat-id", default=None)
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()
    send(args.text, chat_id=args.chat_id, secrets_path=args.secrets)
    print(f"[telegram_notify] sent to {args.chat_id or '(default chat)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Real end-to-end verification — send yourself a message**

```bash
python3 scripts/telegram_notify.py "Phase 3 smoke test: telegram_notify.py is live"
```

Expected: exits 0, and the message actually arrives in your allowed Telegram chat. This is the real proof the secrets file and API wiring work — no mock stands in for it (matches the repo's "real end-to-end verification" convention, e.g. Phase 2's Groq/Gemini smoke calls).

- [ ] **Step 3: Commit**

```bash
git add scripts/telegram_notify.py
git commit -m "feat: add telegram_notify send-only helper

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `discover_account.py` — account discovery & capped backfill (§4.5)

**Files:**
- Create: `scripts/discover_account.py`
- Test: `scripts/test_ingest.py` (extend)

**Interfaces:**
- Consumes: `jobs_lib.find_job`, `jobs_lib.write_job` (Task 1)
- Produces: pure `should_stop_backfill(index, post_date, first_seen, posts_cap, days_cap) -> bool`, `parse_gallery_dl_posts(raw_json_text) -> list[dict]`, `select_backfill_shortcodes(posts, first_seen, posts_cap, days_cap) -> list[dict]`, `build_page_record(username, first_seen_iso, posts_cap, days_cap) -> dict`; orchestrator `enroll_if_new(username, pages_root=..., jobs_root=..., cookies=..., posts_cap=50, days_cap=90) -> dict | None` (`None` means "already watched, no-op").

- [ ] **Step 1: Write `scripts/discover_account.py`**

```python
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
    kept = select_backfill_shortcodes(posts, now, posts_cap, days_cap)

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
```

- [ ] **Step 2: Extend `scripts/test_ingest.py`**

Add to the imports: `from datetime import datetime` and `import discover_account`. Add:

```python
# Trimmed from a real `gallery-dl https://www.instagram.com/networkchuck/posts/
# --post-range 1-5 -j` run against @networkchuck during Phase 3 design
# (2026-08-30) -- shortcodes/dates/owner_id are the actual values returned.
SAMPLE_GALLERY_DL_JSON = json.dumps([
    [2, {"post_shortcode": "DcmLKiSF75h", "post_date": "2026-08-28 20:02:57",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "ytdl:https://www.instagram.com/p/DcmLKiSF75h/1.mp4"],
    [2, {"post_shortcode": "DchEg-1PNFF", "post_date": "2026-08-26 20:28:27",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "https://instagram.fsdv5-1.fna.fbcdn.net/v/t51.82787-15/example.jpg"],
    [2, {"post_shortcode": "Dce_LrgCrFA", "post_date": "2026-08-26 01:03:30",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "ytdl:https://www.instagram.com/p/Dce_LrgCrFA/1.mp4"],
])


def test_should_stop_backfill_posts_cap():
    now = datetime(2026, 9, 1)
    assert discover_account.should_stop_backfill(2, now, now, posts_cap=2, days_cap=90) is True
    assert discover_account.should_stop_backfill(1, now, now, posts_cap=2, days_cap=90) is False


def test_should_stop_backfill_days_cap():
    first_seen = datetime(2026, 9, 1)
    old_post = datetime(2026, 5, 1)  # ~123 days before first_seen
    assert discover_account.should_stop_backfill(0, old_post, first_seen, posts_cap=50, days_cap=90) is True
    recent_post = datetime(2026, 8, 20)
    assert discover_account.should_stop_backfill(0, recent_post, first_seen, posts_cap=50, days_cap=90) is False


def test_parse_gallery_dl_posts_skips_non_post_entries():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    assert [p["shortcode"] for p in posts] == ["DcmLKiSF75h", "DchEg-1PNFF", "Dce_LrgCrFA"]
    assert posts[0]["username"] == "networkchuck"
    assert posts[0]["owner_id"] == "4440726664"


def test_select_backfill_shortcodes_respects_posts_cap():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    first_seen = datetime(2026, 9, 1)
    kept = discover_account.select_backfill_shortcodes(posts, first_seen, posts_cap=2, days_cap=90)
    assert [p["shortcode"] for p in kept] == ["DcmLKiSF75h", "DchEg-1PNFF"]


def test_select_backfill_shortcodes_respects_days_cap():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    first_seen = datetime(2026, 9, 1)  # posts are 4-6 days old
    kept = discover_account.select_backfill_shortcodes(posts, first_seen, posts_cap=50, days_cap=5)
    assert [p["shortcode"] for p in kept] == ["DcmLKiSF75h"]


def test_build_page_record_shape():
    record = discover_account.build_page_record("networkchuck", "2026-08-30T00:00:00+00:00")
    assert record == {
        "username": "networkchuck",
        "first_seen": "2026-08-30T00:00:00+00:00",
        "backfill": {"posts_cap": 50, "days_cap": 90, "done": False},
        "last_checked": None,
        "newest_known_shortcode": None,
    }


def test_enroll_if_new_noop_when_already_watched(tmp_path):
    pages_root = tmp_path / "pages"
    pages_root.mkdir()
    (pages_root / "networkchuck.json").write_text("{}")
    assert discover_account.enroll_if_new("networkchuck", pages_root=pages_root,
                                           jobs_root=tmp_path / "jobs") is None
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_ingest.py -v
```

Expected: 14 passed (7 from Task 1 + 7 new).

- [ ] **Step 4: Real end-to-end verification — small-cap live enrollment**

Use a deliberately small `--posts-cap` for this verification run so it stays cheap (the default 50/90 is for real production use, not a one-off test):

```bash
python3 scripts/discover_account.py networkchuck --posts-cap 3
cat pages/networkchuck.json
ls jobs/queued/
```

Expected: `pages/networkchuck.json` has `backfill.done: true` and a real `newest_known_shortcode`; `jobs/queued/` gained up to 3 new `<shortcode>.json` files (fewer if any of those 3 recent posts were already tracked — e.g. `DW1O6ZBEfDa`, `DcblDAVNrgn` etc. from earlier phases are NOT networkchuck's 3 newest so no collision expected, but don't treat a lower count as a bug without checking why). stderr shows the real gallery-dl command and an "enrolled networkchuck: N backfill jobs queued" line.

**Leave these 3 jobs in `jobs/queued/`** — Task 5's worker verification drains real queued jobs, and having a couple of small/cheap ones (recent networkchuck posts, likely short reels or images) ready there is useful. If any turn out to be long videos and that's undesirable, note it but don't delete — Task 5 decides what to actually drain.

- [ ] **Step 5: Commit**

```bash
git add scripts/discover_account.py scripts/test_ingest.py
git commit -m "feat: add discover_account.py — §4.5 account discovery + capped backfill

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

(`pages/` and `jobs/` are gitignored — nothing from Step 4's live run gets committed.)

---

### Task 4: `telegram_bot.py` — the ingest bot itself

**Files:**
- Create: `scripts/telegram_bot.py`
- Test: `scripts/test_ingest.py` (extend)

**Interfaces:**
- Consumes: `fetch.extract_shortcode` (existing), `jobs_lib.find_job`/`write_job` (Task 1), `secrets_lib.load_secret` (existing)
- Produces: pure `is_allowed_chat(chat_id, allowed_chat_id) -> bool`, `status_reply(state, shortcode) -> str`; CLI entrypoint that runs the long-poll bot.

- [ ] **Step 1: Write `scripts/telegram_bot.py`**

```python
#!/usr/bin/env python3
"""Phase 3 ingest bot: standalone Telegram bot, own token, allowlisted to one
chat ID. URL in -> jobs/queued/<shortcode>.json -> ack. This cannot be the
Claude Telegram bridge (PLAN.md sec 0 Correction 3: it binds to exactly one
session and isn't pointed at this project) -- it's a fully separate process
that owns nothing but the job queue.

Usage:
    python3 scripts/telegram_bot.py [--jobs-root DIR] [--secrets PATH]

Long-running; run under tmux/nohup (a systemd unit is Phase 5). Ctrl-C to stop.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import extract_shortcode
from jobs_lib import find_job, write_job
from secrets_lib import DEFAULT_SECRETS, load_secret

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telegram_bot")


def is_allowed_chat(chat_id, allowed_chat_id) -> bool:
    """Pure: PLAN.md sec 0b's access decision -- only one chat may ever queue
    jobs. Compared as strings since Telegram chat IDs and the secrets-file
    value may differ in type (int vs str)."""
    return str(chat_id) == str(allowed_chat_id)


def status_reply(state: str, shortcode: str) -> str:
    """Pure: the idempotent-resend reply (PLAN.md sec 6, 'a re-sent URL is a
    no-op')."""
    return f"Already tracked: {shortcode} is in {state}/."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = context.bot_data["allowed_chat_id"]
    jobs_root = context.bot_data["jobs_root"]
    chat_id = update.effective_chat.id

    if not is_allowed_chat(chat_id, allowed_chat_id):
        logger.warning("rejected message from unauthorized chat_id=%s", chat_id)
        return

    text = update.message.text or ""
    try:
        shortcode = extract_shortcode(text)
    except SystemExit:
        await update.message.reply_text("Send an Instagram post/reel URL.")
        return

    state, _ = find_job(shortcode, jobs_root)
    if state is not None:
        await update.message.reply_text(status_reply(state, shortcode))
        return

    write_job(
        shortcode, jobs_root, "queued",
        url=text.strip(), chat_id=chat_id,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    await update.message.reply_text(f"Queued {shortcode}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()

    token = load_secret("TELEGRAM_BOT_TOKEN", args.secrets)
    if not token:
        raise SystemExit(f"[telegram_bot] FATAL: TELEGRAM_BOT_TOKEN not set in {args.secrets}")
    allowed_chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", args.secrets)
    if not allowed_chat_id:
        raise SystemExit(f"[telegram_bot] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {args.secrets}")

    application = Application.builder().token(token).build()
    application.bot_data["allowed_chat_id"] = allowed_chat_id
    application.bot_data["jobs_root"] = args.jobs_root
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot starting, jobs_root=%s", args.jobs_root)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Extend `scripts/test_ingest.py`**

Add `import telegram_bot` to the imports. Add:

```python
def test_is_allowed_chat_matches():
    assert telegram_bot.is_allowed_chat(12345, "12345") is True
    assert telegram_bot.is_allowed_chat("12345", "12345") is True


def test_is_allowed_chat_rejects_other():
    assert telegram_bot.is_allowed_chat(999, "12345") is False


def test_status_reply_text():
    assert telegram_bot.status_reply("done", "ABC123") == "Already tracked: ABC123 is in done/."
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_ingest.py -v
```

Expected: 17 passed (importing `telegram_bot` must not require a live token — verify no network call happens at import time, only inside `main()`).

- [ ] **Step 4: Start the bot and send a real message from your phone**

```bash
cd /home/dan/projects/instagram-to-value
nohup python3 scripts/telegram_bot.py > /tmp/telegram_bot.log 2>&1 &
echo "started, pid $!"
```

Then, from your phone's Telegram app, send a real Instagram post/reel URL to the bot.

```bash
tail -20 /tmp/telegram_bot.log
ls jobs/queued/
```

Expected, per PLAN.md's own Phase 3 done-criterion: within 2s of sending, you receive a `"Queued <shortcode>."` reply, and `jobs/queued/<shortcode>.json` exists on disk with the right `url`/`chat_id`. Re-send the same URL and confirm you instead get the `"Already tracked..."` reply and no duplicate file is created.

Leave the bot running (or note its PID) — Task 5 needs it alive to send the worker's progress notifications... actually no, `worker.py` sends its own messages independently via `telegram_notify.py` and doesn't need the bot process running. Feel free to stop it after this verification (`kill <pid>`) — restart when doing real end-to-end use.

- [ ] **Step 5: Commit**

```bash
git add scripts/telegram_bot.py scripts/test_ingest.py
git commit -m "feat: add telegram_bot.py — Phase 3 ingest bot

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: `worker.py` — queue drainer + orchestration, close out Phase 3

**Files:**
- Create: `scripts/worker.py`
- Test: `scripts/test_ingest.py` (extend)
- Modify: `PLAN.md` (mark Phase 3 done)

**Interfaces:**
- Consumes: `fetch.py`/`extract.py` as subprocesses; `discover_account.enroll_if_new` (Task 3); `jobs_lib.list_queued`/`move_job` (Task 1); `telegram_notify.send` (Task 2)
- Produces: pure-ish `resolve_creator_username(media_dir, shortcode) -> str | None` (file IO only, no network); CLI that drains the queue once (`--once`) or forever.

- [ ] **Step 1: Write `scripts/worker.py`**

```python
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
```

- [ ] **Step 2: Extend `scripts/test_ingest.py`**

Add `import worker` to the imports. Add:

```python
def test_resolve_creator_username_from_info_json(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    (media_dir / "ABC123.info.json").write_text(json.dumps({"uploader_id": "networkchuck"}))
    assert worker.resolve_creator_username(media_dir, "ABC123") == "networkchuck"


def test_resolve_creator_username_from_gallery_dl_sidecar(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    (media_dir / "ABC123_01.json").write_text(json.dumps({"username": "ynetgram", "owner_id": "999"}))
    assert worker.resolve_creator_username(media_dir, "ABC123") == "ynetgram"


def test_resolve_creator_username_none_when_absent(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    assert worker.resolve_creator_username(media_dir, "ABC123") is None
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_ingest.py -v
```

Expected: 20 passed.

- [ ] **Step 4: Real end-to-end verification — drain the real queue**

Use whatever landed in `jobs/queued/` from Task 3 Step 4 (the small-cap `networkchuck` backfill) and Task 4 Step 4 (the real URL sent from your phone, if you left that job queued — check first: `find jobs -name '*.json'`). If both are still queued, run:

```bash
cd /home/dan/projects/instagram-to-value
python3 scripts/worker.py --once 2>&1 | tee /tmp/worker_run.log
```

Expected: for each queued job, in order — a "⏳ fetching" Telegram message arrives, then fetch runs (subprocess output visible in the log), then "⏳ extracting", then extract runs, then either "✅ ... done — <media_type>, N chars" (and, for the very first new-username job, "(enrolled new account @... for backfill)") or "❌ ... failed at <stage>" with the job now sitting in `jobs/failed/` and its `error` field populated. Confirm on disk:

```bash
ls jobs/done/ jobs/failed/ 2>/dev/null
```

If a job takes an unexpectedly long time (a video job can legitimately run 15 min–2.5 hours per PLAN.md §0b — the 30× ASR budget), that is expected, not a bug; let it finish or interrupt and re-queue by re-sending the URL later — `--once` with nothing left queued just exits immediately, it doesn't wait on a job it isn't running. If every queued job happens to be a long video job and running one to completion isn't wanted right now, it is enough to verify the flow up through the "⏳ fetching"/"⏳ extracting" notifications actually arriving and the job file correctly sitting in `jobs/running/` — note in the plan's completion notes that the full done/failed transition was verified separately (e.g. against the already-slower-but-bounded image/carousel case) rather than skipped.

- [ ] **Step 5: Update `PLAN.md`'s Phase 3 section to done**

Add a `— ✅ DONE 2026-08-30` marker to the `### Phase 3 — Ingest bot` heading (matching Phases 0/1/2's style) and a short results bullet list underneath: confirmation the phone-to-ack path met the <2s bar (Task 4 Step 4), the real `discover_account.py` enrollment result (Task 3 Step 4 — account, posts queued), and the real `worker.py` drain result (Task 5 Step 4 — which jobs, done vs failed vs still-running-and-why). Record any real deviations found while building (matching Phase 2's "Design deviations found while building" section) rather than silently smoothing them over.

- [ ] **Step 6: Commit**

```bash
git add scripts/worker.py scripts/test_ingest.py PLAN.md
git commit -m "feat: add worker.py — drains the job queue, closes out Phase 3

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** design spec's Component 1 (bot) → Task 4. Component 2 (notify helper) → Task 2. Component 3 (worker) → Task 5. Component 4 (discover_account) → Task 3. "Data/schema additions" (job + page shapes) → Task 1's `write_job`/`move_job` and Task 3's `build_page_record`. "Error handling" (fail-loud, no retry) → Task 5's `process_job` fail branches. "Scope boundary" (no systemd) → every task's CLI is a plain script, no unit files added. "Testing" section → each task's `test_ingest.py` slice plus the four real end-to-end verifications (Tasks 2/3/4/5 Step 4-ish).
- **No placeholders:** every step has complete, real code; the one genuinely open item (no untrack/retry mechanism) is called out explicitly in `discover_account.py`'s docstring and `enroll_if_new`'s comment, not hidden.
- **Type/name consistency checked:** `jobs_lib.write_job`/`move_job`/`find_job`/`list_queued` signatures match every caller in Tasks 3/4/5. `discover_account.enroll_if_new`'s keyword args (`pages_root`, `jobs_root`, `cookies`, `posts_cap`, `days_cap`) match both its own `main()` and `worker.py`'s call. `telegram_notify.send(text, chat_id=None, secrets_path=...)` matches `worker.py`'s `notify()` closure. `fetch.extract_shortcode` (existing, unmodified) is imported, not reimplemented, in `telegram_bot.py`.
