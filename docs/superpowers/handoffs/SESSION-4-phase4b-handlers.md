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

> **Session 3: replace this block.** One row per proposed type: the type name,
> how many posts wanted it, an example rationale in the agent's own words, and
> your read on whether it deserves a handler or is a one-off. Order by count.
> Also record here anything you found force-fitted into an existing type.

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
