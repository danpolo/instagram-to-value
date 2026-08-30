# SESSION 5 — Phase 5b: automation, watchlist walk, weekly digest

**Prerequisite:** `SESSION-4-phase4b-handlers.md` complete.
**Next:** none — this closes the roadmap in `PLAN.md` §7.
**Written:** 2026-08-30.

## Your mandate

**Plan and implement in one go. No approval gate.** Stop and ask only for a
decision the plan genuinely does not settle.

Write the plan to `docs/superpowers/plans/<date>-phase5b-automation.md` using
`superpowers:writing-plans`, then execute it.

## Read first

1. `PLAN.md` §7 Phase 5 — the phase definition. Note its "Worker durability"
   block was **already done in Session 1**; confirm each of the three fixes is
   in the current `worker.py` rather than re-implementing them.
2. `PLAN.md` §4.5 — account discovery and watchlist, specifically the
   **"Ongoing watch — Phase 5, daily"** subsection, which specifies the
   incremental-crawl early-exit precisely.
3. `PLAN.md` §4.5's open questions — the untrack mechanism and the
   `enroll_if_new` retry gap are still open and belong to this session.
4. `PLAN.md` §6 (idempotency — jobs keyed by shortcode, `--download-archive`
   no-ops anything already fetched, so listing overlap never double-processes).
5. `scripts/discover_account.py`, `scripts/worker.py`, `scripts/staging_lib.py`.

## In scope

**1. Turn the worker into a daemon.** Enable `while True` (`worker.py:155`).
Session 1 built the three durability fixes specifically so this is safe —
verify all three are present before flipping it, because `PLAN.md` is explicit
that this switch is what makes those bugs real.

**2. Scheduling.** A systemd **user** timer, or `/loop`, per `PLAN.md` §7
Phase 5. If systemd: the unit files go in `scripts/` or a `systemd/` directory
in the repo, not written directly into `~/.config/systemd/`, and installing
them is a documented step Dan runs. Note the project convention in
`~/.claude/CLAUDE.md`: privileged commands go into one script for Dan to run,
never executed from the agent shell. A *user* timer needs no privilege, but
anything that does must follow that rule.

**3. The daily watchlist walk** (§4.5, ongoing watch). For each enrolled
account in `pages/`: re-list only the newest page of posts, walk newest-first
until `newest_known_shortcode` is hit, enqueue everything newer, stop. Update
`newest_known_shortcode` and `last_checked`. Reuse
`discover_account.py`'s existing throttled listing — §4.5 is emphatic that a
"trusted" walk is not an exemption from throttling, since the automation flag on
this account happened during exactly this kind of multi-post scan.

**4. The weekly digest.** This is the piece that genuinely required Phase 4 to
exist first — it summarises what was *created*. It should cover, at minimum:
artifacts installed by type, proposals still pending, the auto-discard rate, and
the `unsupported` backlog. `PLAN.md` §8 risk #4 is the reason the discard and
drift numbers belong in it: without them, classifier quality decays invisibly.

**5. Close §4.5's two open gaps:**
- An **untrack mechanism** — removing an account from the watchlist. Today the
  existence of `pages/<username>.json` with `backfill.done = true` *is* the
  watchlist, so untracking needs a real decision about whether to delete the
  file or add a flag. Prefer a flag: deleting loses `newest_known_shortcode` and
  the account silently re-enrols on the next post from it.
- A **retry path for `enroll_if_new`** — a failed listing leaves behind a page
  file that blocks a naive re-run. Same underlying gap as the untrack問題.

## Out of scope

- New action handlers — Session 4 owns those.
- Re-implementing Session 1's durability fixes.

## Watch out for

- **Unattended draining is where the RAM gate earns its keep.** §8 risk #1: ASR
  peaks at 6.9 GB of ~9 GB, and swap on this machine is ~975 MB and effectively
  full at baseline, so there is no paging headroom. A daemon that ignores
  available RAM will wedge rather than degrade.
- **The daily walk can enrol new accounts transitively** once `follow_account`
  and `queue_post` handlers exist (Session 4). A watchlist that grows itself
  plus a daily walk is a snowball risk — check whether a cap or a confirmation
  belongs on automatic enrolment, and raise it with Dan if you think it does.
- **Two writers on the job queue.** Once a timer drains automatically, a manual
  `worker.py --once` run races with it. Decide on a lockfile or document the
  hazard; do not leave it implicit.

## Verification

- Unit tests for the incremental-crawl early-exit: stops at
  `newest_known_shortcode`, enqueues only newer posts, handles a first-ever walk
  with a null cursor, handles an account whose newest post is unchanged.
- Untrack and the `enroll_if_new` retry path, each tested.
- Digest rendering tested against a fixture set of proposals with a known type
  distribution.
- The daemon: start it, kill it mid-job, confirm Session 1's orphan requeue
  recovers the job on restart.
- **Live:** one real watchlist walk against an enrolled account, confirming it
  lists only the newest page, enqueues correctly, and updates the cursor. One
  real weekly digest delivered to Telegram.
- Zero regressions across the whole suite.

## Report back to Dan

Test count before and after · the timer's install command and schedule · the
result of the live watchlist walk (account, posts listed, posts enqueued, cursor
before and after) · the live digest as delivered · how the two §4.5 gaps were
closed · and your read on the snowball and two-writer risks above.
