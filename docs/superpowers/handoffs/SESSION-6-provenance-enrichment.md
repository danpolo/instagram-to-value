# Session 6 — Provenance enrichment (Phase A) and the benchmark gate (Phase B)

Written 2026-09-11. Design decided and approved this session; **no code written yet.**
Visual plan: `~/.agent/diagrams/itv-provenance-and-benchmarks-plan.html` (delivered to Telegram).

## What triggered this

Dan ran `/pending`, hit `DbJvV3BpnO6`, and could not decide. The proposal asks to install
three skills and says nothing about any of them — not in the `/pending` line, not behind
**Show full**. His words: *"im not approving anything without knowing what it is."*

Two durable requirements came out of that:

1. **Complete the data the video didn't give.** If a post names a repo or skill, the
   proposal must carry what a person would look up before approving — repo age, stars,
   last commit, licence, directory standing — and *where the name came from*.
2. **Benchmark every new skill/repo.** New installs land on a not-benchmarked list.
   Each gets a benchmark. It stays usable in real sessions from first approval, but a
   failing benchmark produces a removal suggestion.

## Research already done — do not re-derive

All three fetched live 2026-09-11 from the GitHub REST API and `skills.sh/api/search`:

| Action | Source | Stars | Created | Last push | skills.sh | Provenance |
|---|---|---|---|---|---|---|
| `a1` impeccable | `pbakaus/impeccable`, Apache-2.0, 4,118 forks | 67,185 | 2025-11-16 | 2026-09-10 | 271,152 installs, dominant | spoken in the video |
| `a2` taste | `leonxlnx/taste-skill`, MIT, 5,883 forks | 86,135 | 2026-02-19 | 2026-08-24 | 466,153 installs, dominant | spoken in the video |
| `a3` page-foundry | `taylorbanks/page-foundry`, MIT, 0 forks, 1 contributor (206 commits) | 2 | 2026-07-07 | 2026-08-14 | **not listed** | **comment only** |

- `leonxlnx/taste-skill` has **no git tags**, so the video's "Taste 2.0" claim is not
  verifiable from the repo.
- `a3` is not in the video at all. It comes from a comment by `fuckyeahtaylor` in
  `media/DbJvV3BpnO6/DbJvV3BpnO6.info.json` — the repo's own author advertising it
  (*"that led me to build page-foundry … npx skills add taylorbanks/page-foundry"*).
  The only other cached comment on the post is *"Don't fall for this guy's BS."*
  Both comments were already on disk when the proposal was written; the proposal
  surfaced neither and called `a3` "comment-recommended".

**Decision: `DbJvV3BpnO6` stays pending.** Re-render it after Phase A and decide then.

## Decisions locked (do not reopen)

- Enrichment runs **at interpret time** and is **persisted into `proposal.json`**, so
  `/pending` and **Show full** stay offline and instant and the record says what was
  known at approval time.
- The benchmark is **drafted by an agent from the installed skill's own SKILL.md/README**,
  **run automatically**, and its **verdict goes to Telegram**; failure offers Remove/Keep.
- **Phase A first**, Phase B after.

## Phase A — build steps

| # | Step | Files |
|---|---|---|
| A1 | Provenance classifier: pure fn over transcript + caption + comments → `video` / `caption` / `comment` / `agent_inferred`, plus the quoting comment's author and whether that comment itself links the repo it recommends | `scripts/enrich.py`, `scripts/test_enrich.py` |
| A2 | Three injectable fetchers: skills.sh (reuse `actions/_skill_directory.resolve`), GitHub `/repos` + `/contributors`, README head. Each returns `{status: ok\|unavailable, …}`; **none may raise** | `scripts/enrich.py` |
| A3 | Flag derivation, pure, thresholds in one dict: `low_adoption`, `solo_author`, `stale`, `not_in_directory`, `self_promoted_in_comment`, `not_in_video`, `no_licence`, `archived` | `scripts/enrich.py` |
| A4 | Call between `registry.validate_actions` and the proposal write; each action dict gains optional `evidence` and `flags` (old proposals still read) | `scripts/interpret.py` |
| A5 | Render: one evidence tail line per action in `/pending`; full block incl. the quoted comment and its author behind **Show full** | `scripts/staging_lib.py`, `scripts/telegram_bot.py` |
| A6 | `enrich.py --pending`: idempotent in-place re-enrichment of the 41 already-staged proposals, **no agent calls** | `scripts/enrich.py` |

Covers action types `skill_install`, `git_repo`, `package_install`.

## Phase B — the benchmark gate

| # | Step | Files |
|---|---|---|
| B1 | Registry: one record per installed artefact (name, source, originating shortcode, `installed_at`, state, benchmark path, last run), appended by `install_artifact` on every successful install | `state/benchmarks.json`, `scripts/benchmarks_lib.py` |
| B2 | Draft: agent reads the installed SKILL.md/README, writes 2–4 concrete tasks with explicit pass criteria | `benchmarks/<name>/bench.md` |
| B3 | Run + judge: each task via `agents_lib` in a throwaway workdir; judge agent scores against the criteria; every run archived | `scripts/benchmark.py`, `benchmarks/<name>/runs/` |
| B4 | Verdict: pass updates the record silently; fail sends one Telegram message quoting the failing task with Remove/Keep on the existing callback pattern. `/benchmarks` lists not-benchmarked and failing | `scripts/telegram_bot.py` |

States: `not_benchmarked → drafted → running → passing \| failing → verdict_sent →
uninstalled \| kept_despite_failure`. **Nothing in this machine blocks use of a skill**;
only an explicit Remove tap deletes anything.

## Open questions

- **GitHub rate limit — counted 2026-09-14, the earlier "~80 calls" was an estimate and
  was wrong.** `staging/` holds 42 proposals, 28 pending, carrying 15 GitHub-bound actions
  (12 `git_repo` + 3 `skill_install`); across all 42 it is 19. At two REST calls each
  (`/repos` + `/contributors`) the backfill is **~30 calls for the pending set, ~38 for
  all of it** — under the unauthenticated 60/h. `package_install` (7 pending) resolves
  against npm/PyPI, not GitHub, and README heads come from `raw.githubusercontent.com`,
  which does not draw on the REST quota. Nothing reads commit history: `pushed_at` is a
  field of the repo object already being fetched.
  A token in `secrets.env` (5,000/h) is still worth adding — the 60/h is per-IP and shared
  with anything else on this machine, and a re-run inside the same hour doubles the draw —
  but it is headroom, not a blocker.
- **Self-promotion detection is a heuristic.** `fuckyeahtaylor` → `taylorbanks` was
  inferred from the install command in the comment text. The flag should claim only what
  is checkable — *the name came from a comment, and that comment links the repo it
  recommends* — never identity.
- **Auto-reject on flags** (e.g. `exec` + `not_in_video` + `low_adoption`) is deliberately
  **not** in this plan.
- **Benchmark cost:** 3–5 agent calls per skill per draft+run. Needs a cap before it is
  wired to upstream-push triggers.

## Verification

- `python -m pytest -q` green, with new `scripts/test_enrich.py` covering the pure
  classifier and flag rules against injected fetcher stubs.
- `python scripts/enrich.py --pending` then re-read
  `staging/DbJvV3BpnO6/proposal.json`: `a1`/`a2` clean, `a3` carries `not_in_video`,
  `self_promoted_in_comment`, `low_adoption`, `solo_author`, `not_in_directory`.
- Re-run it: byte-identical result (idempotent).
- Restart `itv-bot` (tmux `kill-session` + `new-session`, see SESSION-4 facts) and run
  `/pending` — the evidence tail must be visible without tapping **Show full**.

## Status — 2026-09-15 (Phase A shipped, then /pending redesigned)

Committed as `bc1c635`; 323 tests green. Phase B not started — see `SESSION-7-benchmark-gate.md`.

**Phase A as built** (`scripts/enrich.py`):
- Provenance: exact word-run match in transcript → caption → comments; single-word names ≥6 chars
  also fuzzy-match ASR mishearings (shared 4-letter prefix, ratio ≥ 0.65 — "Imperfectible" →
  impeccable, but not "trademark" → termshark). Multi-word names never fuzzy-match (live bug:
  `@deepseek-ai/dsh` matched "DeepSeek").
- Fetchers never raise; an `unavailable` fetch adds no flag and is retried by `--pending`; a 404 is
  marked `not_found` and not retried. GitHub `NOASSERTION` licence → `"other"`, not `no_licence`.
- `--pending` touches only pending proposals (28 of 42 staged), skips actions with complete evidence,
  writes only on change → second run byte-identical, zero API calls. 14 proposals gained evidence.
- Verification outcomes: `a1`/`a2` clean; `a3` = `not_in_video`, `self_promoted_in_comment`,
  `low_adoption`, `solo_author`. **No `not_in_directory`**: skills.sh lists
  `taylorbanks/page-foundry` (6 installs) as of 2026-09-14 — checked directly.

**Redesign Dan asked for after seeing it** (supersedes A5's "evidence tail line"):
- `/pending` is a flat list: one message per action across all posts, ✅ Approve / ❌ Skip each,
  10 per batch. Card = action · stars · "What:" · "Why:" · at most one plain-words ⚠️ line. No dates,
  licence, contributor counts or shortcodes on the card. Full evidence stays behind Show full.
- `scripts/explain.py`: one agent call per post writes `explanation: {what, why}` per pending action,
  cached in proposal.json (at interpret time, and `--pending` for backlog: 32 calls, 47/47 explained).
  Claude sometimes drops the closing `}` of `{"actions": [...]}`; `_parse_items` recovers the items.
- Duplicates: `staging_lib.action_key` = repo (from evidence, `source`, GitHub URL or
  `npx skills add owner/repo`), else package/name/url. One card per key; representative is
  skill_install > package_install > git_repo. Approve installs the representative and, only on
  success, marks the others `skipped` with output `duplicate of <sc>/<id>`; Skip skips all.
  Card warns "Already installed from another post" when any proposal has that key `installed`.
  Live: 47 pending actions → 46 cards; impeccable, last30days-skill, CLI-Anything flagged installed.
- New-post notifications (`worker.notify_proposal_now`) stay one message per post but stack the same
  cards, labelled `[a1]` so Pick… still maps. Buttons unchanged.
- Callbacks: `act_ok:<shortcode>:<id>` / `act_skip:<shortcode>:<id>`; the post link lives only there
  and in proposal.json.
