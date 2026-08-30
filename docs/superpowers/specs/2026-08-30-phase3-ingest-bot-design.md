# Phase 3 — Ingest Bot: Design

**Status:** approved 2026-08-30, proceeding straight to implementation (Dan: "no
need to ask for my approval anymore unless you have any question/decision that
is not clear").

**Spec:** `PLAN.md` §7 Phase 3, §4.5 (account discovery & watchlist), §6 (state
& idempotency), §0 Correction 3 (why this must be a standalone bot).

**Goal:** A URL sent to a dedicated Telegram bot becomes a queued job with an
ack within 2s; a separate worker drains the queue, runs fetch→extract, and
sends progress notifications; ingesting any post auto-enrolls its creator for
a capped, throttled backfill per §4.5.

---

## Architecture

Two decoupled, independently-runnable processes, talking only through the
filesystem (`jobs/{queued,running,done,failed}/<shortcode>.json`) — matches
§1's "each stage writes to disk and exits; nothing holds state in memory
across stages."

```
telegram_bot.py (long-poll)              worker.py (sleep-poll loop, foreground)
  URL in → validate chat_id                jobs/queued/ → move to running/
  → extract shortcode (fetch.extract_        → fetch.py
    shortcode, reused not duplicated)        → resolve creator username
  → already tracked? reply status,           → discover_account.enroll_if_new()
    don't re-queue                             → capped, throttled backfill,
  → jobs/queued/<sc>.json                        enqueues more jobs/queued/
  → ack reply, <2s                          → extract.py
                                             → move to done/ (or failed/)
                                             → Telegram progress msgs (send-only)
```

Rationale for two processes over one asyncio app: a fetch+ASR run can take
minutes to 2.5 hours (§0b). If ingest and draining shared a process/event
loop, a stuck or crashed drain risks taking new-URL acking down with it —
exactly the failure mode the plan's "async by construction" note warns about.
Splitting them also matches Phase 5's story ("systemd user timer or `/loop`
drains the queue") — that automation only needs to wrap `worker.py`, `telegram_bot.py`
is untouched.

## Components

### 1. `scripts/telegram_bot.py`

Long-poll app via `python-telegram-bot` (already installed, v22.7 — confirmed
via `pip show`). One message handler:

1. Reject silently (log only, no reply — don't confirm the bot's existence to
   an unauthorized chat) if `update.effective_chat.id != TELEGRAM_ALLOWED_CHAT_ID`
   (read from `secrets.env` via `secrets_lib.load_secret`, already populated).
2. Extract a shortcode from the message text using `fetch.extract_shortcode()`
   — **imported from `fetch.py`, not reimplemented** — reply with usage help if
   no Instagram URL is found.
3. Idempotency check: if `jobs/{queued,running,done,failed}/<shortcode>.json`
   already exists, reply with that job's current status and stop (no
   re-queue) — matches §6 "a re-sent URL is a no-op."
4. Else write `jobs/queued/<shortcode>.json`:
   `{"shortcode", "url", "chat_id", "requested_at"}` (ISO 8601 UTC).
5. Ack: `"Queued <shortcode>."` Must complete well under 2s — this step never
   shells out or blocks on I/O beyond one small file write.

### 2. `scripts/telegram_notify.py`

Shared helper for `worker.py` only (the bot's own handler replies inline
through its own context, no need for this). `send(text, chat_id=None)`:
wraps `python-telegram-bot`'s async `Bot(token).send_message(...)` in
`asyncio.run(...)` for use from a plain synchronous script. `chat_id`
defaults to `TELEGRAM_ALLOWED_CHAT_ID`.

### 3. `scripts/worker.py`

Foreground loop: `while True: drain once; sleep POLL_INTERVAL`. Draining one
job:

1. Move `jobs/queued/<sc>.json` → `jobs/running/<sc>.json`.
2. `telegram_notify.send(f"⏳ fetching {sc}")`.
3. Run `fetch.py <url>` (subprocess, matches `extract.py`'s
   `run_json_subprocess` convention). On failure → step 7 (fail).
4. `telegram_notify.send(f"⏳ extracting {sc}")`.
5. Run `extract.py <sc>` (subprocess). On failure → step 7 (fail).
6. Resolve the creator's username from what `fetch.py` already captured
   (`info.json`'s `uploader_id`/`uploader` for the video track, the
   gallery-dl per-slide metadata JSON's `username`/`owner_id` for the image
   track — both already on disk per §4.5 step 1, no extra fetch needed). Call
   `discover_account.enroll_if_new(username)` (best-effort: log and continue
   on any failure here — a backfill problem must never fail an otherwise-
   successful ingest of the post that triggered it).
   Move `running/` → `done/`, notify with a short summary (media type, text
   length, whether a new account was enrolled).
7. **Fail path:** move `running/` → `failed/<sc>.json` with the subprocess's
   stderr tail attached, notify failure, continue to the next queued job. No
   auto-retry (§ open risk: re-hammering a rate-limited/dead-cookie session is
   exactly what got the account flagged before, per §9).

### 4. `scripts/discover_account.py`

Implements §4.5.

- `enroll_if_new(username, media_root=..., jobs_root=..., pages_root=...)`:
  no-op if `pages/<username>.json` already exists (already watched). Else:
  1. Write the initial `pages/<username>.json` (schema per §4.5 step 2:
     `username`, `first_seen`, `backfill: {posts_cap: 50, days_cap: 90, done:
     false}`, `last_checked: null`, `newest_known_shortcode: null`).
  2. Run, as one subprocess:
     `gallery-dl https://instagram.com/<username>/posts/ --post-range 1-{posts_cap}
     -j --sleep-request 8-18 --cookies <cookies>` — **verified live during
     design** against `@networkchuck`: returns one JSON array of
     `[type_code, metadata]` entries, `type==2` entries are posts with
     `post_shortcode`, `post_date` (`"YYYY-MM-DD HH:MM:SS"`), `owner_id`,
     `username`, newest-first. `--sleep-request 8-18` throttles between the
     HTTP requests gallery-dl issues internally while paging — same
     delay+jitter window `scan_carousel.py` already uses, satisfying §4.5's
     "a trusted backfill is not an exemption from throttling."
  3. Walk the `type==2` entries in order; stop at whichever of `posts_cap` /
     `days_cap` (post older than `first_seen - days_cap` days) is hit first
     — pure function `should_stop_backfill(index, post_date, first_seen,
     posts_cap, days_cap)`.
  4. For each shortcode kept, write `jobs/queued/<shortcode>.json` **unless**
     it already exists in any `jobs/*` state (idempotency, same check as the
     bot's).
  5. Update `pages/<username>.json`: `backfill.done = true`,
     `newest_known_shortcode` = the newest shortcode seen, `last_checked` =
     now.
- Resolving `username` itself is the caller's job (`worker.py`, from data
  already on disk) — `discover_account.py` never re-derives it, keeping this
  module testable without touching `media/`.

## Data / schema additions

Confirms and slightly narrows §6:

- `jobs/{queued,running,done,failed}/<shortcode>.json`: `{shortcode, url,
  chat_id, requested_at}`, plus on `failed/`: `error` (stderr tail) and
  `failed_at`.
- `pages/<username>.json`: exactly the §4.5 shape.

Both dirs are already gitignored (`.gitignore` lines for `jobs/`, `pages/`).

## Error handling

Fail-loud, no auto-retry, matches the repo-wide convention (`fetch.py`,
`extract.py`). A failed job sits in `jobs/failed/` until a human re-sends the
URL to re-queue it (no `/retry` command in this phase — not asked for, YAGNI).

## Scope boundary

No systemd unit in this phase — `worker.py` runs under `tmux`/`nohup` for
now; wrapping it in a scheduler is Phase 5. `telegram_bot.py` needs to run
continuously too (any long-poll bot does) but that's an operational detail,
not a phase-5 dependency — the ingest-side "done when" criterion only needs
it running while a real test message is sent.

## Testing

Pure-logic unit tests in `scripts/test_ingest.py` (new file, mirrors
`test_extract.py`'s pattern — pytest, no mocking of Telegram/network):

- Idempotency check (job-exists-in-any-state lookup).
- `discover_account.should_stop_backfill` — posts_cap boundary, days_cap
  boundary, neither hit.
- `discover_account`'s post-filtering from a captured sample of the real
  gallery-dl JSON shape (fixture, not a live call).
- `fetch.extract_shortcode` re-import sanity (already covered in principle by
  `fetch.py` itself; a smoke import here just proves `telegram_bot.py`
  doesn't duplicate the regex).

Real end-to-end verification (the plan's own done-criterion): send an actual
Instagram URL from Dan's phone to the live bot, confirm ack <2s, confirm
`worker.py` (run manually in the foreground for the test) drains it through
to `extracted/<shortcode>.json` with Telegram progress messages arriving at
each stage.

## Self-review

- No placeholders/TBDs.
- Internal consistency: job schema here matches §6; `pages/<username>.json`
  schema is copied verbatim from §4.5.
- Scope: single implementation plan, mirrors Phase 2's task-by-task size.
- Ambiguity resolved: process shape (two, not one), backfill enumeration
  (gallery-dl CLI, verified live), automation scope (foreground only, no
  systemd), failure handling (fail-loud, no retry) — all locked above, no
  open questions left for the plan to improvise on.
