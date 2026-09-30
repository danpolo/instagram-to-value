# Session 7 — Phase B: the benchmark gate

Written 2026-09-15. Design approved in Session 6; **no Phase B code exists yet.**

## Read first

1. `docs/superpowers/handoffs/SESSION-6-provenance-enrichment.md` — § "Phase B — the benchmark gate"
   (B1–B4 table, state machine) and § "Status — 2026-09-15" (what Phase A and the /pending redesign
   actually built).
2. `PLAN.md` §9 open items #13 (this work) and #10–#11 (related, out of scope).
3. Memory: `pending-flat-action-list` — Dan's approval-UI preference; any Telegram message you add
   (the failure verdict) should follow it: plain words, what/why, no metadata noise.

## Goal

Every successfully installed skill/repo lands in a registry as `not_benchmarked`, gets an
agent-drafted benchmark from its own SKILL.md/README, is run and judged automatically, and a failure
sends Dan one Telegram message with Remove / Keep. **Nothing blocks use of a skill**; only an
explicit Remove tap deletes anything.

## Coordination

- `itv-bot` and `itv-worker` run in tmux from this repo and import `staging_lib`, `install_artifact`,
  `registry`. Code changes only take effect after a restart — and **the agent's auto-mode classifier
  blocks restarting them**; give Dan the commands:
  - `tmux kill-session -t itv-bot && tmux new-session -d -s itv-bot -c /home/dan/projects/instagram-to-value "python3 scripts/telegram_bot.py >> logs/telegram_bot.log 2>&1"`
  - `tmux kill-session -t itv-worker && tmux new-session -d -s itv-worker -c /home/dan/projects/instagram-to-value "python3 scripts/worker.py >> logs/worker.log 2>&1"`
    (check `jobs/running/` is empty first)
- Dan may tap Approve in `/pending` while you work — that writes `staging/*/proposal.json` and runs
  real installs. Don't hand-edit staging files; don't run installs yourself.
- `staging/` is gitignored. Commit locally; Dan pushes.

## Facts to build on (do not re-derive)

- **Install entry points:** `/pending` Approve → `telegram_bot.handle_callback` `act_ok` →
  `install_artifact.install_action`; also `approve_all`, `confirm_pick`, `replace`/`keep_both` from
  per-post messages. All successful installs funnel through `install_action` →
  `staging_lib.update_action_status(..., "installed", result)` — B1's hook point.
  Duplicates merged in `/pending` are marked `skipped` directly (not installed) — they must **not**
  create registry records.
- **Where things land:** `skill_install` → `~/agent-skills/skills/<name>` (then `agent-skills sync`;
  see `actions/_skills_root.py`); `git_repo` → `~/tools/<name>`; `package_install` → package manager.
  `result["path"]` holds the location when known.
- **Agent calls:** `agents_lib.run_agent(prompt, backend=None, schema=None, allow_search=False)`
  never raises, returns `{ok, text, error, ...}`. Backend `claude` = `claude -p --restricted` with no
  `--model` → Dan's settings: Opus 5, effort medium. Parse replies defensively — Claude dropped the
  closing brace on 4 of 28 explain calls; `explain._parse_items` shows the recovery.
- **Existing Telegram patterns:** callback data `verb:<shortcode>:<id>` (64-byte limit); buttons via
  `InlineKeyboardMarkup`; pushes from non-bot processes via `scripts/telegram_notify.py`
  (`send`, `send_with_buttons`).
- Tests: `python3 -m pytest -q` → **323 passed** at `bc1c635`. Fakes for bot handlers live in
  `scripts/test_ingest.py` (`_FakeUpdate`, `_FakeContext`, `_FakeQuery`).
- Already installed today (candidates for the first real benchmark): impeccable (several posts),
  last30days-skill, CLI-Anything (`~/tools/`).

## Decision still open — ask Dan once, bundled, before B3

- **Cost cap.** Draft + run + judge ≈ 3–5 agent calls per skill. Proposed default: benchmark only on
  first install (no upstream-push re-runs yet), max 1 draft + 1 run per artefact per day, and a
  global daily cap (e.g. 10 artefacts). Confirm or adjust.

## In scope

- B1 `state/benchmarks.json` + `scripts/benchmarks_lib.py` (pure state transitions, test-first).
- B2 draft `benchmarks/<name>/bench.md` (2–4 tasks, explicit pass criteria) via one agent call.
- B3 `scripts/benchmark.py`: run each task in a throwaway workdir, judge against criteria, archive
  every run under `benchmarks/<name>/runs/`.
- B4 verdict: pass → silent record update; fail → one Telegram message quoting the failing task,
  Remove / Keep; `/benchmarks` lists not-benchmarked and failing.
- Backfill: register the already-installed artefacts as `not_benchmarked`.

## Out of scope

- Auto-reject or auto-remove on anything. Re-benchmarking on upstream pushes.
- Open items #10 (broken `npx skills add` force-fit proposals) and #11 (cross-post identity index).
- npm/PyPI metadata for `package_install` cards.

## Verification — report these numbers

1. `python3 -m pytest -q` count (must be > 323, all green).
2. Registry record count after backfill, by state.
3. One real end-to-end run on an already-installed skill (suggest impeccable): agent calls used,
   wall time, verdict, path to the archived run.
4. A forced failure (stub judge) produces exactly one Telegram message with Remove / Keep; tapping
   Keep sets `kept_despite_failure` and deletes nothing.
5. Update PLAN.md §9 #13 and this doc's status, commit.

## Status — 2026-09-15 (Phase B built)

**Cost cap, as Dan decided:** no fixed count. Each benchmark runs on whichever of four quotas is furthest
ahead of weekly pace — largest `pace_delta` from the QuotaPulse bridge (`GET /usage`, token
`~/.quotapulse/.bridge_token`). A quota is skipped if its week is used up or its current 5-hour window is at
90% or more. If none qualify, the benchmark waits. Work / judge models: claude sonnet/opus (medium effort),
codex gpt-5.6-terra/gpt-5.6-sol (medium), agy gemini-3.8-flash-low/-high, agy-3p
claude-sonnet-4-6/claude-opus-4-6-thinking. Benchmarks run on first install only; an agent error is retried
after 6 h.

**Built:** `benchmarks_lib.py` (state machine + `running → drafted` retry edge, flock'd JSON shared by bot
and worker), `quota_router.py`, `benchmark.py` (CLI `--backfill/--list/--next/--record [--stub-verdict]`),
`agents_lib` gains an `agy` backend plus `effort` and `workdir`. The run agent can only write inside its
throwaway directory: no shell, no network, so a repo's own code never runs. The hook in `install_artifact`,
`maybe_benchmark` in the worker (only when the queue is empty, at most one check per 10 min) and
`/benchmarks` plus the `bench_rm` / `bench_keep` callbacks in the bot. Remove deletes only paths strictly
inside an install root.

**Verification:**
1. pytest: 365 passed (was 323).
2. Backfill: 5 records, all `not_benchmarked` (impeccable = 4 installs). After the real run: 1 passing.
3. impeccable end-to-end: agy-gemini, 5 agent calls, 192 s, **pass 3/3**,
   `benchmarks/impeccable/runs/2026-09-15T121317Z/`. Found live: agy's `-p` takes the prompt as its value
   (fixed), and agy ignores its cwd (fixed with `--add-dir`).
4. Forced failure (stub draft/run plus `stub_verdict=fail`, real Telegram): exactly 1 message with Remove /
   Keep for the record `benchmark-gate-selftest` (`4292fac114`, files in the session scratchpad, outside every
   install root). Keep → `kept_despite_failure` is covered by a test; **the live tap needs the bot restarted
   first.** Afterwards, delete that record from `state/benchmarks.json`.

**Needs Dan:** restart itv-bot and itv-worker (commands above) so installs register and benchmarks run.
