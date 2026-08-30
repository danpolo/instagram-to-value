# Phase 4 — Interpret + Staging Gate: Design

**Status:** approved 2026-08-30, proceeding to implementation plan.

**Spec:** `PLAN.md` §7 Phase 4, §1 stage 4 + "why a staging gate is
non-negotiable", §4 (hidden-tool-name resolver), §5 (knowledge taxonomy —
**superseded by this document, see below**), §6 (state & idempotency),
§8 risk #4.

**Goal:** A post that has been fetched and extracted produces a *proposal* —
an ordered list of concrete actions — staged on disk and summarised to
Telegram, where approving it applies the actions and rejecting it records why.

---

## Why §5's taxonomy is replaced, not extended

Before designing, all seven posts already in `extracted/` were classified
against §5's six types. The result:

| Post | What it actually offers | Fits §5? |
|---|---|---|
| `DW1O6ZBEfDa` | **Mem Palace** — open-source AI memory tool, ~5k stars | ✗ wants a repo clone |
| `Dce_LrgCrFA` | **zoxide** — "I don't use cd anymore" | ✗ wants a package install *and* a shell init line |
| `DcmLKiSF75h` | **termshark** — TUI for tshark | ✗ wants a package install |
| `DbsDXkgJ_FB` | 5 ways to cut Claude Code token costs | ✓ a genuine skill/rule |
| `DchEg-1PNFF` | Apple Event, 2026-09-09 10:00 PT, RSVP | ✗ wants a calendar event |
| `DcgAqIADbs2` | Hebrew carousel — LGBTQ-youth statistics, sourced | ✗ a dated fact, not a procedure |
| `DcblDAVNrgn` | Hebrew — personal essay on family size | ✗ not tech at all |

**§5's six types capture 1 of 7 real posts.** The most common valuable outcome
— *creator demos a tool, you want it on the machine* — is 3 of 7 and has no
home in the taxonomy at all. Two further posts are not tech content.

The failure is structural, not a missing row: a post is not one classification.
The zoxide post is three actions (install the binary, add the shell init line,
keep a note); Mem Palace is two (clone the repo, keep a note). Forcing either
into one `reference` throws away most of the value.

**Therefore:** a proposal is an **ordered list of actions**, each with its own
type drawn from an extensible registry, its own payload, confidence, and risk
tier, each individually approvable.

---

## Architecture

A fourth stage in the existing filesystem-coupled pipeline. Like `fetch.py` and
`extract.py`, `interpret.py` is a standalone script that reads from disk,
writes to disk, and exits.

```
extracted/<shortcode>.json + media/<shortcode>/
                  ↓
    ┌─ interpret.py ──────────────────────────────────┐
    │  tiers 1-2 (pure Python, free)                  │
    │        ↓                                        │
    │  agent call #1 — triage + draft action list     │
    │        ↓ (only if a tool is named but unlinked) │
    │  tier 4 — web search  →  tier 3 — frame OCR     │
    │        ↓                                        │
    │  agent call #3 — redraft (only if resolved)     │
    └─────────────────────────────────────────────────┘
                  ↓
    staging/<shortcode>/proposal.json + artifact/
                  ↓
    telegram_bot.py — buttons → install_artifact.py
```

**Fat Python, thin agent.** Python owns the escalation chain and every
confidence gate as individually-testable functions; the agent is invoked only
where judgment is required. This is the same shape `extract.py` already uses
for OCR escalation (mean-confidence 0.70 gate plus `looks_garbled`), and it is
the only split in which tier 3's lazy mp4 re-fetch is expressible — that is a
Python action the agent cannot reach from inside a prompt.

---

## Components

### 1. `scripts/agents_lib.py` — pluggable agent backend

Both backends are headless CLIs already installed and verified on this machine:

| | Claude (default) | Codex |
|---|---|---|
| Headless | `claude -p` | `codex exec` |
| JSON out | `--output-format json` (envelope; `.result` is free text) | `--output-schema <file>` (hard schema) + `-o <file>` |
| Web search | `WebSearch`, gated by `--allowed-tools` | `--search` |
| Sandbox | `--restricted` / `--permission-mode` | `--sandbox read-only` |
| Model | `--model` | `--model` |

Codex can be *forced* to a JSON shape; Claude can only be asked. The shared
contract is therefore "return JSON matching this schema", **validated
Python-side for both** — Codex simply gets a second belt for free.

Public surface:
- `build_cmd(backend, prompt, *, schema_path=None, allow_search=False, model=None) -> list[str]` — pure, one branch per backend, unit-tested without running anything.
- `run_agent(prompt, **kw) -> AgentResult` — the only impure function; `{text, backend, model, duration_s, ok, error}`.
- `extract_json(text) -> dict` — tolerant extraction (fenced block, else first balanced object).
- `get_backend()` / `set_backend(name)` — reads/writes `config/agent.json`.

Adding a third backend later is one `build_cmd` branch plus one test.

### 2. `scripts/actions/` — the action registry

One module per action type. Each declares six things:

| Member | Purpose |
|---|---|
| `TYPE` | registry key |
| `RISK` | `inert` \| `config` \| `exec` |
| `SCHEMA` | payload shape, validated before anything is staged |
| `describe(payload)` | the one-line Telegram summary |
| `preview(payload)` | what `📄 Show full` renders (file body, diff, or literal command) |
| `collides(payload)` | existing target path, or `None` |
| `install(payload)` | the effect; returns `InstalledResult` |

`registry.py` maps `TYPE → module` and — critically — **generates the agent
prompt's action catalogue from the registry**. Adding a handler automatically
teaches the classifier that the action exists. No prompt is edited by hand.

### 3. `scripts/resolve_tools.py` — the §4 resolver

Returns `{status, tool_name, url, tier, evidence, candidates, reasoning}` where
status is `resolved | uncertain | unresolved | not_applicable`. §4's confidence
gate is preserved exactly: `resolved` may state the tool as fact, `uncertain`
records the candidate *and* the reasoning without promoting it, `unresolved`
stores the synthesized description. `not_applicable` is added for the common
case of a post that demos no tool.

Escalation order (see *Design deviations* — tier 3 is moved last):

1. **Tier 1 — creator's own replies.** Pure Python. Filter `info.json` comments to those authored by the post's own `channel`; extract URLs.
2. **Tier 2 — caption + comment links.** Pure Python. URLs from `description` and all comments, minus self-referential `instagram.com` links. One candidate → `resolved`; several → `uncertain` with all of them.
3. **Agent call #1 — triage + draft.** Receives transcript, caption, comments, tier 1–2 findings, and `reconciliation.disagreement_spans`. Returns the action list plus `unnamed_tool: {present, description}`. **For most posts this is the only agent call.**
4. **Tier 4 — web search.** Agent call #2, search enabled. Only if `unnamed_tool.present` and tiers 1–2 came up empty. Returns a candidate with `high|medium|low` confidence and its sources.
5. **Tier 3 — frame OCR.** Last resort, only if still unresolved and `media_type == "video"`. Re-fetches the mp4 (`fetch.py --keep-mp4`), samples frames with ffmpeg, runs them through the existing `ocr_local → ocr_ocrspace → ocr_gemini` ladder, feeds the on-screen text back for a final identification.
6. **Agent call #3 — redraft.** Only if a tier resolved something the draft did not know.

Cost profile: **one agent call and zero downloads for the common post**; three
calls plus one video fetch only for a post that genuinely hides its tool behind
*"comment X and I'll DM you."*

The `disagreement_spans` input is deliberate. `PLAN.md`:702-705 records that
ASR mis-hears domain proper nouns ("Claude Det MD" for CLAUDE.md, "Sonar or
Opus" for Sonnet); the resolver must be tolerant of that noise rather than
string-matching, and the spans tell it exactly where the transcript is unsure.

### 4. `scripts/interpret.py` — the stage orchestrator

`python3 scripts/interpret.py <shortcode>` — invoked exactly like `fetch.py`
and `extract.py`. Reads `extracted/<shortcode>.json` and `media/<shortcode>/`,
runs the chain above, validates the action list against the registry, writes
`staging/<shortcode>/`.

### 5. `scripts/staging_lib.py` — the on-disk staging contract

Mirrors `jobs_lib.py`'s role: one place that knows the layout, so the bot, the
worker and the installer never hand-build paths.

### 6. `scripts/install_artifact.py` — applying an approved action

Per-action install with the never-overwrite policy and **path allowlisting**.
The agent drafts target paths, so every path is validated against an allowlist
of roots before anything is written:

```
~/.claude/{skills,rules,commands,agents,templates}
~/tools            ~/.local/bin        ~/services
<repo>/.claude     <repo>/knowledge
```

Traversal (`..`) and symlink-escape are checked after resolution. A path
outside the allowlist **fails the action** — it is never silently relocated.

### 7. Changes to existing scripts

- `worker.py` — call `interpret.py` as a 4th stage; branch on job origin at notify time.
- `telegram_bot.py` — `CallbackQueryHandler` for the buttons, `/agent claude|codex`, `/pending`, and reject-reason capture.
- `telegram_bot.py` `write_job` — add `source="telegram"`. `discover_account.py:156` already writes `source="backfill"`; jobs missing the field default to `"telegram"`.

---

## The action registry — initial contents

Targets verified against what actually exists on this machine.

**Agent configuration** — `skill` → `~/.claude/skills/` · `rule` →
`~/.claude/rules/` · `command` → `~/.claude/commands/` (exists, 3 entries) ·
`subagent` → `~/.claude/agents/` · `workflow` → `.claude/workflows/` ·
`prompt_template` → `~/.claude/templates/` (created on first use) ·
`mcp_server` (`claude mcp add-json` — verified) · `plugin`
(`claude plugin install` — verified) · `settings_patch` (JSON-merge diff
against `settings.json`)

**Software on the machine** — `git_repo` → `~/tools/<name>` (matches the
existing convention: context7, free-claude-code, jcode, notebooklm-py) ·
`package_install` (npm / uv / pip / cargo / apt / docker are present; pipx,
brew, go are **not**) · `binary_release` → `~/.local/bin/` (90 entries
already) · `script` → `~/.local/bin/` · `docker_stack` → `~/services/`
(exists) · `shell_snippet` → a managed block in `~/.bashrc` (no `~/.bashrc.d`
on this machine) · `model_download` (HF weights + exact command)

**Knowledge** — `reference` · `note` · `bookmark` · `reading_item` ·
`dataset_or_stat`

**Time & people** — `calendar_event` (`.ics`; Google Calendar MCP when
authenticated) · `reminder` · `follow_account` → writes
`pages/<username>.json`, so a post saying "follow @X" enrols @X for backfill ·
`queue_post` → a referenced reel is enqueued into `jobs/queued/`. The last two
make the pipeline feed itself through §4.5's existing machinery.

**Meta** — `update_existing` (patch an installed artifact instead of creating
a near-duplicate — necessary because 49 queued posts from one Claude Code
creator would otherwise produce 49 overlapping skills) · `discard` ·
`unsupported`

### Where knowledge actions install

`PLAN.md` §5 sends `reference` to `~/.claude/projects/-home-dan/memory/`. That
path **does exist** (3 files, its own `MEMORY.md`), but it is the auto-memory
directory for sessions launched in `$HOME` — it is not global.

Researched against the machine and the [official memory
docs](https://code.claude.com/docs/en/memory):

| Mechanism | Global? | Context cost |
|---|---|---|
| `~/.claude/CLAUDE.md` | ✓ | every session; `@imports` also load at launch, so they save nothing |
| `~/.claude/rules/*.md` | ✓ ("apply to every project on your machine") | every session |
| `~/.claude/skills/<name>/` | ✓ | **only the `description` line**; body and files load on demand |
| `autoMemoryDirectory` at user scope | ✓ | would orphan the 46 existing per-project memories |
| `~/.claude/reference/` | ✗ | referenced by no config — a manual stash, not a load path |

The docs are explicit: *"Rules load into context every session… For
task-specific instructions that don't need to be in context all the time, use
skills instead."* Confirmed empirically: all 32 skills in `~/.claude/skills/`
load in a session running inside `instagram-to-value`.

**Decision — a global knowledge skill:**

```
~/.claude/skills/captured-knowledge/
├── SKILL.md          # one description line (always in context) + an index (on demand)
├── tools/<slug>.md
├── facts/<slug>.md
├── notes/<slug>.md
└── bookmarks.md
```

Global across every project, grows without bound at a fixed cost of one
description line, native and documented (`claude plugin init` scaffolds this
exact layout), and plain markdown that can be grepped, edited or deleted.
`reference` / `note` / `bookmark` / `dataset_or_stat` / `reading_item` install
here. Only a genuine always-on constraint earns `~/.claude/rules/`, which stays
expensive on purpose — this preserves §5's "default to the weakest artifact"
rule while making both destinations global.

---

## Risk tiers

- **inert** — writes a file nothing executes (`reference`, `note`, `bookmark`, `calendar_event`, `prompt_template`, `reading_item`, `dataset_or_stat`).
- **config** — changes how future sessions behave (`skill`, `rule`, `command`, `subagent`, `workflow`, `settings_patch`, `mcp_server`, `plugin`, `shell_snippet`, and the clone half of `git_repo` — `git clone` itself executes nothing). Never automatic: this is §1's *"one Instagram reel should never be able to write a global rule unattended."*
- **exec** — causes third-party code to run (`package_install`, `binary_release`, `script`, `docker_stack`, and any build step after a clone).

**Exec actions run on approval, like any other action** (Dan's decision,
2026-08-30). The mitigations built in, since the tier is enabled:

- `preview()` renders the **literal command** that will run, verbatim, before approval.
- The resolver's evidence (resolved URL, tier, sources) is shown beside it, so approval is made against provenance rather than a name alone.
- Every executed command is written back into the action record with its exit code and output tail — `proposal.json` is the audit trail for what ran and why.

Recorded for the record, once: this repo's own data shows ASR mangling proper
nouns (`PLAN.md`:702-705), and a mis-transcribed package name reaching a real
installer is a supply-chain exposure that no post-hoc staging UI recovers. The
`preview()`-shows-the-literal-command rule exists specifically to put a human
read of the exact string in front of every exec action.

---

## Data / schema additions

```
staging/<shortcode>/proposal.json      # action list + decision record; kept after decision
staging/<shortcode>/artifact/<path>    # drafted files, byte-identical to what installs
staging/<shortcode>/replaced-<name>    # backup written before any Replace
staging/<shortcode>/agent-error.txt    # raw output of a failed agent call
logs/discarded.jsonl                   # §8 risk #4's classifier-tuning log
logs/unsupported_actions.jsonl         # the pilot backlog
config/agent.json                      # {"backend": "claude", "model": null}
```

`proposal.json`:

```json
{ "shortcode": "Dce_LrgCrFA", "origin": "telegram|backfill",
  "backend": "claude", "model": "...", "created_at": "...",
  "summary": "zoxide — a smarter cd that ranks by frequency and recency",
  "resolver": { "status": "resolved", "tool_name": "zoxide", "url": "...",
                "tier": 4, "evidence": ["..."] },
  "status": "pending|decided",
  "message_id": 1234,
  "actions": [
    { "id": "a1", "type": "package_install", "risk": "exec", "confidence": 0.91,
      "payload": {"manager": "cargo", "package": "zoxide",
                  "command": "cargo install zoxide"},
      "status": "pending|installed|skipped|failed",
      "decided_at": null, "result": null },
    { "id": "a2", "type": "shell_snippet", "risk": "config", "confidence": 0.88,
      "payload": {"...": "..."}, "status": "pending" }
  ] }
```

Staging directories survive their decision — that is the audit trail for why a
reel became a global rule. `/pending` is a directory scan; nothing needs a
database.

---

## Approval & install flow

**Manual origin** — one message per post, immediately: summary, resolver
verdict, then one line per action with its risk badge. Buttons:
`[✅ All] [☑️ Pick…] [📄 Show full] [❌ Discard]`. `Pick…` expands to per-action
toggles, so a post can contribute its reference while its install is skipped.

**Backfill origin** — staged silently, then one digest per drain:
*"14 proposals: 6 tools, 4 references, 2 rules, 2 notes · 9 auto-discarded ·
⚠️ 3 unsupported types"*, with `[Review]` walking pending proposals one at a
time.

**Reject** — `❌ Discard` replies "why?"; the next message becomes the reason in
`logs/discarded.jsonl`. Pending-reason state lives in `context.user_data`; a bot
restart mid-reason degrades to the existing "Send an Instagram post/reel URL."
reply, which is harmless.

**Auto-discard** — backfill origin only, `type == discard` and confidence ≥
threshold. Logged with reason and a transcript excerpt; counted in the digest.
**Never** on a URL sent by hand — those always come back, pre-marked, for one
tap.

**Collisions** — `collides()` runs before any install. If the target exists:
stop, keep staging, reply `⚠️ <path> exists — [Replace] [Keep both] [Cancel]`.
`Replace` backs the original up to `staging/<shortcode>/replaced-<name>` first;
`Keep both` installs as `<name>-2`. `update_existing` is the type that *intends*
to touch an installed artifact: it stages a diff and shows it before applying.

---

## The pilot loop

The registry cannot anticipate everything a real feed contains. The agent is
instructed: if the right action is not in the registry, do **not** force-fit it
— emit

```json
{ "type": "unsupported", "proposed_type": "browser_extension",
  "payload_sketch": {"...": "..."}, "rationale": "..." }
```

`unsupported` actions never install. They append to
`logs/unsupported_actions.jsonl` and surface in the digest as
*"⚠️ 4 posts wanted actions I can't do yet: browser_extension ×2,
rss_subscribe ×1, font_install ×1."*

The pilot therefore does not depend on noticing gaps by hand: it emits a ranked
backlog of the action types the corpus is actually asking for, and each entry
becomes one handler module plus one test.

---

## Error handling

- **Agent call fails** (timeout, non-zero exit, unparseable JSON) → one retry with a "return only JSON" nudge, then the job moves to `jobs/failed/` with raw output in `staging/<shortcode>/agent-error.txt`. A failed call never yields a fabricated proposal.
- **Schema violation** in the returned action list → same path as an unparseable response. Nothing partially-valid is staged.
- **An action's `install()` fails** → that action is marked `failed` with its error; sibling actions are unaffected. Partial application is recorded, not rolled back.
- **Notify failures** — Phase 4 adds several new `note()` calls. These are wrapped so a Telegram blip degrades to a logged warning. The three existing unwrapped calls (open items #7/#9) stay as they are, fixed in Phase 5 as planned — this change does not add to that debt.
- **Tier 3 re-fetch** checks `free -h` available RAM before the OCR ladder, per §8 risk #1.

---

## Scope boundary

**In:** the interpret stage, the action registry and its initial handlers, the
staging format, the Telegram approval loop, the installer, backend switching.

**Out:** `while True` worker durability (open items #7/#8 — Phase 5), the daily
watchlist walk (§4.5 ongoing watch — Phase 5), the weekly digest (Phase 5),
draining the 49 queued backfill jobs (a deliberate operator decision, ~28 min of
local ASR each).

---

## Testing

Pure-logic unit tests, no agent invoked:

- `build_cmd()` for each backend, with and without search
- `extract_json()` against fenced, bare, and trailing-prose responses
- action-list validation: unknown type, missing payload field, bad risk tier
- each resolver tier's URL extraction, including creator-authored filtering
- `collides()` per handler
- path allowlist: valid targets, traversal attempts, symlink escape
- auto-discard eligibility (origin × type × confidence)
- digest counting and `unsupported` aggregation
- registry → prompt catalogue generation

**Live verification** runs against the 7 real extracted posts — no re-fetching
needed. Pass bar:

| Post | Expected |
|---|---|
| `Dce_LrgCrFA` | `package_install` **and** `shell_snippet` |
| `DW1O6ZBEfDa` | `git_repo` targeting `~/tools/` |
| `DcmLKiSF75h` | `package_install` (termshark) |
| `DchEg-1PNFF` | `calendar_event` dated 2026-09-09 |
| `DbsDXkgJ_FB` | `rule` or `skill` |
| `DcblDAVNrgn`, `DcgAqIADbs2` | **no** tool actions |

Backend parity: at least one post interpreted under both `claude` and `codex`,
producing schema-valid action lists from each.

---

## Design deviations from PLAN.md

1. **§5's six-type taxonomy is superseded** by the action registry, for the corpus evidence above. §5's types survive as registry entries; its "default to the weakest artifact" principle survives as the rule that knowledge lands in the on-demand skill and only constraints reach `~/.claude/rules/`.
2. **One post yields many actions**, where §1 stage 4 says "write PROPOSAL to staging/" in the singular.
3. **§4's tier 3 (frame OCR) moves from third to last**, and re-fetches the mp4 lazily. The default fetch stays audio-only; only a post that survives tiers 1, 2 and 4 unresolved pays for a video download.
4. **§5's `reference` destination changes** from `~/.claude/projects/-home-dan/memory/` to a global on-demand skill, because the original path is the auto-memory directory for `$HOME` sessions rather than a global store.
5. **Interpret is not "Claude Code" specifically** (§2) but a pluggable backend, with headless Claude Code as the default and headless Codex as the second implementation, switchable from the bot with `/agent`.

---

## Self-review

- **Placeholders:** none. Every path, CLI flag and package manager named here was verified against this machine on 2026-08-30.
- **Consistency:** the risk-tier table, the registry list and the install-flow section all agree that `git_repo`'s clone is config-tier and its build step is exec-tier. `origin` drives exactly two behaviours and they are stated identically in both places they appear (notification style; auto-discard permission).
- **Scope:** large but single-purpose. The registry is the bulk of it, and each handler is independent — the implementation plan should sequence the framework first, then handlers in corpus-priority order, so the stage is verifiable before the long tail exists.
- **Ambiguity resolved:** "confidence" is per-action, not per-post; a proposal has no single confidence score. Auto-discard reads the confidence of a `discard`-typed action, not an aggregate.
