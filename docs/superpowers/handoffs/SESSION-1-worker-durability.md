# SESSION 1 — Worker durability (Phase 5a) + launch the backfill drain

**Prerequisite:** none. This is the first session of the chain.
**Next:** `SESSION-2-phase4a-interpret-stage.md`
**Written:** 2026-08-30, branch `main`, clean at commit `55e3c61`.

## Your mandate

**Plan and implement in one go. No approval gate between them, and do not ask
Dan to review the plan.** Every decision here is already settled by `PLAN.md`.
Stop and ask only if you hit something the plan genuinely does not settle.

Write the plan to `docs/superpowers/plans/<date>-phase5a-worker-durability.md`
using `superpowers:writing-plans`, then execute it, then launch the drain.

## Why this session comes first

Extraction is the expensive stage (~28 min of local ASR per reel); interpretation
is one cheap agent call. There are **49 real jobs queued** — about 23 hours of
CPU. Starting that drain early means it runs unattended while Session 2 builds
the interpret stage, instead of Session 2 waiting a day for it.

But a 23-hour run is exactly what open items #7 and #8 break on, so durability
has to land first. That is this session.

## Read first

1. `PLAN.md` §7 Phase 5 → the **"Worker durability"** block (three required
   fixes, stated as blocking).
2. `PLAN.md` §9 open items **#7, #8, #9** — the bugs, with line numbers.
3. `PLAN.md` §8 risk #1 and the §9 fact "fp16 Qwen3-ASR-1.7B-hf peaks at 6.9 GB
   RSS and needs ~7.5 GB available" — that is the RAM gate's threshold.
4. `PLAN.md` §6 (jobs are idempotent by design, which is what makes requeueing
   an orphan safe).
5. `scripts/worker.py`, `scripts/jobs_lib.py`, `scripts/telegram_notify.py`,
   `scripts/test_ingest.py` (match its pure-function test style).
6. `tasks/lessons.md` — the second lesson especially: in this repo, check the
   code path before calling something broken.

## In scope

**The three durability fixes** (all in `worker.py` unless noted):

1. **Wrap `note()`** (`worker.py:87`, called at :89, :100, :129) so a Telegram
   failure degrades to a logged warning instead of killing the run. Also revisit
   `telegram_notify.send()`'s deliberate fail-loud contract in
   `scripts/telegram_notify.py` — it was written for a one-shot worker, and the
   right split is: `send()` stays honest, the worker's `note()` wrapper is what
   swallows. (Open item #7.)
2. **Requeue orphans on startup.** Scan `jobs/running/` and move anything found
   back to `jobs/queued/` — a job sitting there means the previous run died
   mid-flight. Log each requeue. (Open item #8.)
3. **RAM gate before launching a job.** Read available memory and refuse to
   start a job below the threshold, with a clear log line and a retry rather
   than an OOM. Use ~7.5 GB available as the bar. (§8 risk #1.)

Fixes #7 and #8 share a root and must land together — the `note()` at
`worker.py:89` fires *before* fetch, so a notify error there is what orphans the
job (open item #9).

**Deliberately NOT in this session:** turning on `while True`
(`worker.py:155`). The drain runs with `--once`, which drains everything queued
and exits. The daemon loop belongs to Session 5.

**Cleanup, ~15 min, while you are in `PLAN.md` anyway:**

- **Open item #2 is stale — close it.** It says the Groq API key is still
  needed, but Phase 2 reconciled against `groq-whisper-large-v3` at 87.1% and
  Phase 3 at 93.5%. The key demonstrably works. Mark it resolved with that
  evidence.
- **Open item #5** — delete the unused `~/.local/venvs/instaloader` (29 MB) and
  mark it resolved.
- **Open item #4** — `discover.sh` is already correctly documented as a
  no-cookie fallback. Verify the note still reads true; change nothing if so.

## Out of scope

- `while True` / the daemon loop, the systemd timer, the daily watchlist walk,
  the weekly digest — Session 5.
- Anything in `scripts/telegram_bot.py`. It is **running** and this session has
  no reason to touch it.
- The interpret stage — Session 2.
- §4.5's missing untrack mechanism and `enroll_if_new`'s missing retry path —
  Session 5, where the watchlist work lives.

## Verification

**Unit tests.** 68 currently pass; add roughly 8–12 and keep all 68 green.

- orphan requeue: a job file in `running/` moves to `queued/`; an empty
  `running/` is a no-op; an unreadable file logs and is skipped, not fatal
- RAM gate: above threshold proceeds, below threshold defers with a log line
- `note()` wrapper: a raising notifier produces a warning and the job continues
- `telegram_notify.send()` still raises on its own (the contract is unchanged;
  only the worker's use of it is wrapped)

**Live check before launching the drain** — do all three:

1. Kill a `--once` run mid-job, restart, confirm the orphaned job is requeued
   and completes.
2. Confirm a real Telegram progress message still arrives (the wrapper must not
   have silenced the happy path).
3. Confirm the RAM gate reads a plausible number on this machine.

## Launch the drain, at the very end

Only after all three live checks pass.

- Start `python3 scripts/worker.py --once` detached, with output to a logfile
  outside the repo.
- Watch the **first 2–3 jobs complete** before declaring success — do not launch
  and walk away unverified.
- Expect ~28 min per audio reel, 49 jobs, roughly 23 hours total.

## Report back to Dan

Test count before and after · which of the three live checks passed · the
drain's PID and logfile path · how many jobs completed while you watched · the
estimated finish time · and one line confirming Session 2 can start immediately
(it can — it works on the 7 already-extracted posts and never touches the job
queue).
