# SESSION 2 — Phase 4a: interpret stage + action registry

**Prerequisite:** `SESSION-1-worker-durability.md` complete, and the 49-job
drain launched and running.
**Next:** `SESSION-3-pilot.md`
**Written:** 2026-08-30, branch `main`, clean at commit `55e3c61`.

## Your mandate

**Plan and implement in one go. No approval gate between them, and do not ask
Dan to review the plan.** The Phase 4 design session settled every open
decision. Stop and ask only if you hit a decision the spec genuinely does not
settle.

Write the plan to
`docs/superpowers/plans/<date>-phase4a-interpret-stage.md` using
`superpowers:writing-plans`, then execute it.

## Read first, in this order

1. **`docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md`**
   (442 lines, commit `15f8d9f`) — the whole design, and your source of truth.
   It supersedes `PLAN.md` §5 and records five deliberate deviations from §4/§2.
2. `PLAN.md` §7 Phase 4 (locked decisions), §4 (resolver), §6 (state),
   §8 risks #1 and #4. **§5 is marked SUPERSEDED** — read the banner, do not use
   the table.
3. `docs/superpowers/plans/2026-08-30-phase3-ingest-bot.md` — match its format,
   granularity and tone. It is 999 lines for 5 scripts; this is comparable.
4. `tasks/lessons.md` — two corrections, both of which apply here.
5. Code you will extend: `scripts/jobs_lib.py`, `scripts/worker.py` (Session 1
   changed it — read the current version), `scripts/telegram_bot.py`,
   `scripts/extract.py` (its OCR escalation gate is the pattern the resolver's
   confidence gates should mirror), `scripts/test_extract.py` and
   `scripts/test_ingest.py`.

## Coordination — read this before running anything

- **A 49-job drain is running** (started by Session 1, ~23 h). Do **not** run
  `worker.py` against the live queue — `--once` drains everything queued in one
  process and two workers would race. All verification calls `interpret.py`
  directly against posts already in `extracted/`. This costs you nothing; it is
  how the verification is specified anyway.
- **`scripts/telegram_bot.py` is RUNNING** — a long-poll loop up across
  sessions. This session modifies it. Include stopping and restarting it as
  explicit plan steps; do not assume a fresh process.
- `extracted/` will be growing under you as the drain completes jobs. Treat the
  original 7 shortcodes as the fixed verification set; anything extra is a
  bonus, not a target.

## In scope

- `scripts/agents_lib.py` — `build_cmd()` per backend (pure), `run_agent()`,
  `extract_json()`, `get_backend()`/`set_backend()` over `config/agent.json`.
- `scripts/actions/` — registry framework: the six-member handler contract
  (`TYPE`, `RISK`, `SCHEMA`, `describe`, `preview`, `collides`, `install`),
  `registry.py`, and registry → prompt-catalogue generation.
- Handlers the 7-post corpus demands: `package_install`, `shell_snippet`,
  `git_repo`, `calendar_event`, `rule`, `skill`, `reference`, `note`,
  `discard`, `unsupported`.
- `scripts/resolve_tools.py` — tiers 1, 2, 4, then 3 (lazy mp4 re-fetch), with
  §4's `resolved | uncertain | unresolved | not_applicable` contract.
- `scripts/interpret.py`, `scripts/staging_lib.py`,
  `scripts/install_artifact.py` (including the path allowlist with traversal and
  symlink-escape checks).
- `worker.py` 4th stage + origin branch; `telegram_bot.py` callback handler,
  `/agent`, `/pending`, reject-reason capture, and `source="telegram"` on
  `write_job`.
- Wrapping only the **new** `note()` calls. Session 1 already wrapped the
  existing ones — do not redo that work.
- Creating `~/.claude/skills/captured-knowledge/`.
- **A backfill sweep** (new, because the drain runs before interpret exists):
  a mode that runs interpret over every shortcode in `extracted/` that has no
  `staging/` directory. Without it the 49 drained posts sit unprocessed. It must
  be safe to re-run and must respect the backfill origin's digest behaviour
  rather than firing one message per post.

## Out of scope

- The remaining ~19 registry handlers — Session 4.
- The systemd timer, daily watchlist walk, weekly digest, `while True` —
  Session 5.
- Running the pilot itself — Session 3.

## Two things to get right

1. **`preview()` must render the literal command, verbatim, for every exec-tier
   action.** Dan chose to let exec actions run on approval, so this is the only
   guard between a mis-transcribed package name and a real installer — this
   repo's own ASR already produced "Claude Det MD" and "Sonar or Opus"
   (`PLAN.md`:702-705). Make it a tested requirement, not a convention.
2. **`~/.claude/skills/captured-knowledge/` does not exist yet.** Creating it
   with a well-written `description` deserves its own task: that single line is
   the entire trigger surface for everything the pipeline will ever file there,
   and it is the only part loaded into every session.

## Verification

**Unit tests, no agent invoked.** Add roughly 40+ to the existing count; zero
regressions.

- `build_cmd` per backend, with and without search
- `extract_json` on fenced, bare, and trailing-prose responses
- action-list validation: unknown type, missing payload field, bad risk tier
- each resolver tier's URL extraction, including creator-authored filtering
- `collides()` per handler
- path allowlist: valid targets, traversal attempts, symlink escape
- auto-discard eligibility across origin × type × confidence
- digest counting and `unsupported` aggregation
- registry → prompt catalogue generation
- backfill sweep: skips shortcodes that already have `staging/`, is re-runnable

**Live verification** against the original 7 extracted posts, calling
`interpret.py` directly, no re-fetching:

| Shortcode | Expected |
|---|---|
| `Dce_LrgCrFA` | `package_install` **and** `shell_snippet` |
| `DW1O6ZBEfDa` | `git_repo` targeting `~/tools/` |
| `DcmLKiSF75h` | `package_install` (termshark) |
| `DchEg-1PNFF` | `calendar_event` dated 2026-09-09 |
| `DbsDXkgJ_FB` | `rule` or `skill` |
| `DcblDAVNrgn`, `DcgAqIADbs2` | **no** tool actions |

**Backend parity:** at least one post interpreted under both `claude` and
`codex`, each returning a schema-valid action list.

## End this session by writing the pilot brief

This is a required deliverable, not a nicety. Session 3 is the pilot, and Dan
has to send it real posts beforehand. Before you finish:

1. **Measure, don't guess.** From your verification runs, record the actual
   wall-clock and agent-call count for one interpret run, and the observed
   agent-call cost of a post that escalates to tier 4 versus one that stops at
   agent call #1.
2. **Tell Dan concretely:** how many posts to send, roughly what mix of
   creators and content types would stress the registry hardest (the drained 49
   are all one creator — variety is the gap), and how long the pilot will take
   given the numbers you just measured.
3. **Check the drain.** Report how many of the 49 have completed and the revised
   finish estimate — Session 3 should not start before it is done.
4. **Update `SESSION-3-pilot.md`** with the real state: what got built, what the
   corpus actually looks like, anything you deferred, and any handler that
   turned out shakier than expected.

## Report back to Dan

Test count before and after · which of the 7 pass bars were met · wall-clock and
agent-call count for one interpret run · backend parity result · drain progress
and revised ETA · and the pilot brief from the section above.
