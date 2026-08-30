# Lessons

Corrections Dan made to how I work on this project. Append, don't rewrite.

## 2026-08-30 — Check the project's own capabilities before declaring one unavailable

**What I did:** Saw `plugin:telegram:telegram` in the failed-MCP-servers notice and
told Dan the report was "terminal-only until that's restarted." He pushed back: *"why
cant you [send] it to the telegram bot that we configured for this project?"* He was
right — `scripts/telegram_notify.py` was sitting there, working, and sent the report
on the first try.

**Why I got it wrong:** I reasoned from my *tool list* rather than from the *project*.
The tool list is the harness's inventory of what it can offer me; it is not an
inventory of what the project can do. This repo builds its own Telegram integration —
that's most of Phase 3 — so "Telegram is unavailable" was never a claim the tool list
could support.

**How to apply:** Before reporting any capability as missing or broken, grep the repo
for it. A project that *builds* an integration almost certainly has a working path to
it that owes nothing to my MCP servers. Cheap check, and the failure mode is telling
Dan he can't have something he already built.

**Generalised:** the same trap applies to any capability this project implements
itself — fetching, OCR, ASR, scheduling. Ask "does the repo do this?" before "do I
have a tool for this?"

## 2026-08-30 — Verify the alarming-looking thing before reporting it

**What happened:** While investigating, three things looked like bugs: `jobs/failed/`
didn't exist, the worker wasn't running, and a job had finished with no content
delivered. Two were fine — `move_job` creates the dir on demand (`jobs_lib.py:48`),
and the worker had been run with `--once`. Only the deeper issues (open items #7–#9)
were real, and they were invisible on the surface.

**How to apply:** In this repo, check the code path before calling something broken —
the surface signal has been wrong more often than right. Equally: the real problems
here don't announce themselves, so "nothing looks wrong" isn't a finding either.

## 2026-08-30 — Handoffs go in a file, not a chat block

**What Dan said:** *"dont write me long paste ready prompts anymore, i would
rather you write the details in a md file and write me a short prompt
referencing the file."* — after I produced a ~70-line paste-ready handoff in
chat.

**How to apply:** Handoff detail goes to
`docs/superpowers/handoffs/YYYY-MM-DD-<topic>.md`, committed. The chat message
is a short prompt naming the task and the file path, nothing more. A committed
file survives scrollback, diffs, and can be read directly by the next session.

**Same message, second correction:** when the design left no open decisions,
the next session should plan *and* implement without an approval gate between
them. Say so explicitly in the handoff file.
