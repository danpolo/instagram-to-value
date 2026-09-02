# SESSION 4 — Phase 4b: the remaining action handlers

**Prerequisite:** `SESSION-3-pilot.md` complete, with its ranked backlog written
into this file.
**Next:** `SESSION-5-phase5b-automation.md`
**Written:** 2026-08-30. **Session 3 must fill in "Ranked backlog from the
pilot" below** — that section, not this document's default list, is the real
scope.

## Your mandate

**Plan and implement in one go. No approval gate.** Stop and ask only if the
pilot surfaced something that contradicts the design rather than extending it —
see "When to stop and ask" below.

Write the plan to `docs/superpowers/plans/<date>-phase4b-handlers.md` using
`superpowers:writing-plans`, then execute it.

## Read first

1. **"Ranked backlog from the pilot"** in this file — Session 3 fills it in.
2. `docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md` — the
   handler contract, the registry's initial contents, and the risk tiers.
3. `scripts/actions/` — the handlers Session 2 built. **Match their shape
   exactly.** A handler that invents its own conventions is worse than a missing
   one, because the registry's whole value is uniformity.
4. Session 3's report, for the force-fitting and duplicate-artifact findings.

## Scope

**Priority 1 — whatever the pilot ranked.** Real demand from a real feed beats
anything predicted at design time. Build these first, highest count first.

**Priority 2 — the known remainder from the spec**, for types the pilot did not
happen to surface but which are already designed:

| Family | Types |
|---|---|
| Agent config | `command`, `subagent`, `workflow`, `prompt_template`, `mcp_server`, `plugin`, `settings_patch` |
| Software | `binary_release`, `script`, `docker_stack`, `model_download` |
| Knowledge | `bookmark`, `reading_item`, `dataset_or_stat` |
| Time & people | `reminder`, `follow_account`, `queue_post` |
| Meta | `update_existing` |

`claude mcp add-json` and `claude plugin install` were both verified present on
this machine during the design session; `pipx`, `brew` and `go` are **not**
installed, so `package_install` must not offer them.

**Priority 3 — anything Session 3 flagged as force-fitted or duplicated.**
If the pilot showed posts landing in `note` because nothing better existed, the
missing type is real demand even though it never reached the `unsupported` log.
If `update_existing` was not firing on the 49 near-duplicate posts from one
creator, that is a bug in its trigger, not a missing handler.

## Out of scope

- Changes to the registry framework, resolver, orchestrator or installer, unless
  the pilot proved one wrong — and if it did, see below.
- The systemd timer, watchlist walk, weekly digest — Session 5.

## Parallelism

This is the one phase where fanning out genuinely pays. Each handler is an
independent module implementing a fixed six-member contract, with its own test
and no shared state. Session 2's framework was built precisely so this is true.
Batch them; do not build 19 handlers serially.

Two constraints on the fan-out: every handler must be reviewed against an
already-merged one for shape consistency, and handlers that write to the same
destination (`~/.claude/skills/captured-knowledge/`, `~/.local/bin/`) must agree
on their collision behaviour before any of them merge.

## When to stop and ask

The design assumed a proposal is a list of independent actions, each installable
on its own. Stop and raise it with Dan if the pilot showed otherwise — for
example if actions turn out to need ordering guarantees (install the package
*before* appending the shell line), or if one action's payload depends on
another's result. That is a design change, not a handler, and it is the one
thing in this phase worth a conversation.

## Ranked backlog from the pilot

Measured over the full 42-post corpus (69 actions, 45 agent calls). Counts
below are **actions**, with the distinct posts in brackets. The `unsupported`
log alone understates demand badly — it only catches what the agent *knew* it
could not express. The force-fit columns are where the real signal was.

### 1. `skill_install` — ~12 actions across 9 posts. **Build this first.**

The single highest-value gap by a wide margin, and the only one the pilot found
in all three states at once: logged, force-fitted, and *silently mis-installing*.

| How it showed up | Count | Posts |
|---|---|---|
| Explicit `unsupported: skill_install` | 4 | `DbJvV3BpnO6` ×2, `Db_vaVNJSAI` ×2 |
| Force-fit into `package_install` as `npx skills add …` | 3 | `Db9fLwkpvFC`, `DbJvV3BpnO6`, `DboafZtRTgd` |
| Force-fit into `git_repo` (clone to `~/tools` instead of installing) | 7 | `pbakaus/impeccable` ×3, `Leonxlnx/taste-skill`, `mvanhorn/last30days-skill` ×2, `bradautomates/claude-video` |

Agent's own words:
> "Impeccable is explicitly named and described as a tool to install, but the
> post supplies no repository URL or supported package identifier."
> — `DbJvV3BpnO6`

> "The skill is named, but the post provides no source URL or package
> identifier and the registry has no action that resolves skill names through a
> skill directory." — `Db_vaVNJSAI`

**Three hard proofs this is real, not a taxonomy preference:**

1. **The same skill gets three different classifications.** "Claude Video" is a
   `git_repo` in `Dboal_gxvyh` (`bradautomates/claude-video`) and an
   `unsupported: skill_install` in `Db_vaVNJSAI`. Impeccable is a `git_repo` in
   three posts and a `package_install` in a fourth. The registry has no stable
   home for a skill, so identical content lands wherever the model guesses.
2. **The `package_install` force-fit is actively broken.** `npx skills add`
   installs **relative to the current working directory**, and
   `package_install.install()` calls `subprocess.run(command, shell=True)` with
   **no `cwd=`**. Running it from `scripts/` created `scripts/.agents/skills/`,
   `scripts/.claude/skills/` and `scripts/skills-lock.json` *inside this repo*
   and still returned `ok: true, exit_code: 0`. Under `worker.py` the install
   location is simply wherever the worker was started. A `skill_install`
   handler must pin an absolute destination.
3. **The payloads are internally inconsistent even within the force-fit.**
   `npx skills add nutlope/hallmark` was recorded as `package: "skills"` (the
   CLI) while `npx skills add pbakaus/impeccable` was recorded as
   `package: "pbakaus/impeccable"` (the skill). Any dedup keyed on `package`
   will mismatch.

Design note: `skills.sh` / `npx skills add <owner>/<repo>` is the ecosystem
convention this corpus actually uses, and it resolves a bare `owner/repo`
without a URL — which is exactly what the four `unsupported` rationales say the
registry lacks. It reports a risk rating per skill ("Med Risk", "0 alerts")
that is worth surfacing in `preview()`.

### 2. `binary_release` — 4 actions across 4 posts

| Post | Tool |
|---|---|
| `Da3XotHJHnw` | Herder (agent multiplexer) |
| `Da3uGGzJOBB` | ChatGPT desktop app |
| `DbeS4wdJ5Ze` | ChatGPT desktop app |
| `DcOy_dspg9t` | Obsidian (AppImage / deb) |

> "Obsidian is the installable desktop tool demonstrated, but the registry has
> no binary-release installer and no valid supported package-manager
> installation was provided." — `DcOy_dspg9t`

Real but lower-value than its count suggests: two of the four are the same
ChatGPT desktop app, and GUI-app downloads are the least automatable class here
(no stable URL, platform-specific, often behind a marketing page). A handler
that records the download page and platform, and stops short of executing an
installer, is probably the right scope.

### 3. `claude_plugin_install` — 1 action, 1 post

`DchXj5DnNQd` (claudex-loop).

> "The registry has no handler for installing a Claude Code plugin through its
> marketplace commands; cloning the repository alone does not activate the
> plugin."

One-off by count, but the rationale is exactly right and the fix is cheap —
`claude plugin marketplace add` + `claude plugin install` were both verified
present on this machine at design time. Note the post *also* produced a
`git_repo` for the same repo, so building this handler removes a wrong action,
not just adds a missing one.

### 4. `update_existing` — 0 log entries, but **proven necessary by the pilot**

Never reached the `unsupported` log, because the agent has no way to know
another post already produced the artifact. Found by measurement instead.

Three organic duplicate pairs in the corpus, same tool from different posts,
identical URLs:

| Artifact | Posts |
|---|---|
| `github.com/pbakaus/impeccable` | `DbJvSDzpf47`, `DbZMxr4ptIt`, `Dcmn-CYnKXq` |
| `github.com/mvanhorn/last30days-skill` | `DbWtIbkJraI`, `Db_vaVNJSAI` |
| `github.com/HKUDS/CLI-Anything` | `Db_vJnXpnHS`, `Db_vaVNJSAI` |

This is no longer hypothetical: installing two of them produced
`~/tools/impeccable` and `~/tools/impeccable-2`, **byte-identical excluding
`.git`** (`diff -rq` clean). The collision machinery worked exactly as designed
— it detected, refused to overwrite, left the action `pending`, and offered
keep_both/replace — but "keep both" is the wrong answer for the same repo from
a second post, and nothing upstream proposes "you already have this."

### Force-fitting found (Priority 3)

- **Into `git_repo` / `package_install`: systemic, see §1.** This is the whole
  `skill_install` story and the pilot's main finding.
- **Into `reference`: not systemic.** 14 `reference` actions; 5 co-occur with a
  real install action (complementary, correct). Of the 9 standalone ones, 7 are
  genuinely reference-shaped (model release notes, cost comparisons, a Hebrew
  LGBTQ-statistics post). Only 2 are arguable: `DcFeZa0JgXi` (an Obsidian
  "command-center pattern" that reads like a `skill`) and `DbtvMqgJDPI`
  (`agentgrid.sh`, a named tool from an unverified third-party commenter).
  **The registry's weakest-artifact instruction is holding.** Session 2's fix to
  narrow it did its job — do not re-tune it.
- **`note` fired 0 times in 42 posts.** `reference` absorbs everything
  note-shaped. Either give `note` a distinct trigger or retire it; a registry
  entry that never fires is prompt-catalogue noise that costs tokens on every
  call.

### Not a handler — carry these into Session 4/5 as bugs

1. **Resolver false positive (highest severity).** The corpus's *only*
   non-`not_found` verdict is wrong. `DbtvcDuJ1AI` returned
   `status: resolved, tier: 2` with `tool_name: null, url: null` and evidence
   `https://github.com/nelsonpretti/baymax` — a **third-party self-promo
   comment** ("I've built a 2 way conversation skill … check it out"), unrelated
   to AnyDoc, the tool the post is about. Cause: `resolve_pure_tiers()` tier 2
   scans *every* comment, not just the creator's, and returns `resolved`
   whenever it finds **exactly one URL**, with no check that the URL relates to
   the tool. Consequences: (a) the verdict is injected verbatim into the triage
   prompt (`interpret.py:156`), so a stranger can inject a repo URL into the
   pipeline by commenting on a post Dan saves; (b) a `resolved` verdict
   **blocks tier-4 escalation** (`interpret.py:285` requires `not_found`).
   The agent ignored the bad evidence here and got AnyDoc right on its own —
   luck, not a guard. **Resolver's true corpus score is 0/42 correct
   resolutions, 1 confident false positive.**
2. **`package_install` runs with no `cwd=`** — see §1 proof 2.
3. **Captured facts are unattributed.** `reference.install()` writes only the
   bare content paragraph — no title heading, no date, no source shortcode. The
   installed fact cannot be traced back to the post it came from.
4. **`logs/discarded.jsonl` can no longer be measured.** Auto-discards
   (`discard.py`) and human rejections (`staging_lib.record_reject_reason`)
   append the **same four fields** to the **same file** with no discriminator.
   Auto-discard rate is still recoverable from proposal `status:
   auto_discarded`, but not from the log. Add a `source` field.
5. **Exec output is unreadable.** `npx`'s ANSI spinner frames filled the stored
   `output` (last 2000 chars, nearly all `◓ Installing skills…`), so the
   install record is useless for auditing. Strip ANSI before storing.
6. **A schema-valid empty response can still hide a real miss** (Session 2's
   `DbJvV3BpnO6` finding). Nothing in this sweep reproduced it — 0 empty
   proposals across 42 — but the hole is unchanged.

## Verification

- Every handler: unit tests for `SCHEMA` validation, `collides()`, and
  `describe()`/`preview()` output shape. `install()` tested against a temp
  directory, never the real target.
- Every `exec`-tier handler: a test asserting `preview()` contains the literal
  command string, character for character.
- Path allowlist: every new destination is either already allowlisted in
  `install_artifact.py` or added there with its own traversal test.
- Registry: the prompt catalogue regenerates and includes every new type.
- Zero regressions across the whole suite.
- **Re-run the pilot corpus** after the new handlers land. The point is
  measurable movement: the `unsupported` count should drop, and posts previously
  force-fitted into `note` should now classify correctly. Report the before and
  after numbers.

## Report back to Dan

Handlers built, split by pilot-ranked versus known-remainder · test count before
and after · the `unsupported` count before and after re-running the pilot corpus
· any type you deliberately skipped and why · whether anything in the pilot
contradicted the design rather than extending it.
