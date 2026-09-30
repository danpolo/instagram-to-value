# Benchmark: impeccable

Drafted 2026-09-15T12:13:17+00:00 by agy-gemini (gemini-3.8-flash-low) from `/home/dan/tools/impeccable/.claude/skills/impeccable/SKILL.md`.

## t1 — Command mapping and setup protocol

You have been provided documentation for the 'impeccable' design tool. Review the following 4 user scenarios and write a file named COMMAND_MAP.md:

1. Scenario A: The user wants to inspect existing project code and automatically generate DESIGN.md.
2. Scenario B: The user needs to improve confusing UX copy, error messages, and form labels.
3. Scenario C: The user needs technical quality checks covering accessibility (a11y), performance, and responsive layout for a checkout page.
4. Scenario D: The user wants to pull reusable tokens and components out of existing code into a design system.

In COMMAND_MAP.md:
- Identify the exact command name, category, and reference file path from the Commands table for each scenario.
- State whether `reference/craft-floor.md` should be loaded if the agent is performing planning-only work, citing the documented rule.

**Pass criteria**

- COMMAND_MAP.md maps Scenario A to 'document' (Build, reference/document.md), Scenario B to 'clarify' (Fix, reference/clarify.md), Scenario C to 'audit' (Evaluate, reference/audit.md), and Scenario D to 'extract' (Build, reference/extract.md).
- COMMAND_MAP.md explicitly states that reference/craft-floor.md must not be loaded for planning-only work.

## t2 — Surface mode classification and scoping

You are configuring a project using the 'impeccable' skill documentation. A company has four different digital surfaces:
1. Surface 1: A marketing landing page and pricing tiers intended to drive visitor signups.
2. Surface 2: An administrative analytics dashboard and database query editor where staff complete operational tasks.
3. Surface 3: Developer documentation, guides, and an API changelog.
4. Surface 4: An interactive 3D brand museum and showcase gallery.

Write a file named SURFACE_MODES.md that:
- Assigns each surface to one of the four documented Modes (Persuade, Operate, Read, Experience).
- Summarizes what visitor success looks like for each assigned mode based on the documentation.
- Explains the documented rule regarding whether the mode is determined by the overall product or by the individual surface.

**Pass criteria**

- SURFACE_MODES.md classifies Surface 1 as Persuade, Surface 2 as Operate, Surface 3 as Read, and Surface 4 as Experience.
- SURFACE_MODES.md defines visitor success for each mode matching the docs: Persuade is visitor decides/acts, Operate is visitor completes a task, Read is visitor understands something, and Experience is visitor is inside the work itself.
- SURFACE_MODES.md explicitly states that the mode must be chosen from the requested surface, not the product, and persisted only in that surface brief.

## t3 — Refinement versus redesign and drift handling

You are assessing design requests against the rules in 'impeccable'. Evaluate the following two situations and document your decisions in EVALUATION.md:

Situation 1: An engineer is running a refinement pass on an existing enterprise settings page. The engineer wants to unilaterally rewrite marketing claims and legal disclaimer copy to sound punchier. Additionally, during session setup, a `CONTEXT_STALE` directive was reported.
Situation 2: A team wants to execute a redesign of a legacy app shell, but proposes keeping the existing layout and color palette while merely polishing a few buttons and borders.

In EVALUATION.md, explain:
1. The rule governing factual copy, claims, and incumbent identity during a refinement pass.
2. How `CONTEXT_STALE` findings and artifact drift must be handled during a design task.
3. Why the proposal in Situation 2 violates the documented definition of a redesign, and what a proper redesign is required to do instead.

**Pass criteria**

- EVALUATION.md states that refinement preserves incumbent identity, behavior, and copy outside scope, and explicitly requires asking before replacing factual copy or adding claims.
- EVALUATION.md states that drift/CONTEXT_STALE must never be repaired as a side effect of a design task (reported, not acted on, unless the user asks or the finding is marked 'auto').
- EVALUATION.md states that redesign must never split the difference into polish on a discarded look, but must treat the old look as evidence and anti-reference while choosing a replacement visual world and replacing DESIGN.md.
