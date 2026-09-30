# Current working state

Verified on 2026-09-30. Continue development from this repository's `main` branch.
This snapshot incorporates the working project's source through local commit
`f09d4cd2ad5074308f962f48bff90afdbc8728bc`, while retaining the existing public
repository's sanitized history.

## Implemented

- Durable Instagram ingestion, extraction, staged proposals, and Telegram approval.
- Skill installation by source or a conservatively resolved name, targeting the
  canonical `~/agent-skills` repository.
- Proposal provenance and repository metadata, explanations, and a flat `/pending`
  list that merges duplicate install recommendations.
- A benchmark registry, quota-routed draft/run/judge workflow, and Telegram
  Remove/Keep verdicts. See the final **Status** section in
  [Session 7](superpowers/handoffs/SESSION-7-benchmark-gate.md); its earlier sections
  describe the pre-implementation plan.
- The Glitch Cat Club Lab scraper, a 16-item catalog, RSS feed, and JSON feed in
  `artifacts/`. Run `python3 scripts/scrape_lab_artifacts.py --help` for sync options.
  Catalog thumbnails in this public snapshot link to their public source URLs.

## Verification and next work

`python3 -m pytest -q` passes all **374 tests**. The suite uses fakes for external
integrations; this synchronization does not verify or restart live services.

The remaining development backlog is in `PLAN.md` and the tracked session notes:
binary release and plugin installation handlers, cross-post identity/update
handling, and migration of the two older skill-install proposals (open items
#10 and #11). Session 7 also records a live benchmark callback check and service
restart that must be verified on the deployment host before being marked done.

## Continuing on another machine

Follow the setup commands in `README.md`. Live processing also needs external
credentials and cookies, downloader/extraction dependencies, installed agent CLIs,
and any quota bridge used by the benchmark runner. Configure those separately for
the new host.

Git contains source, tests, plans, benchmark definitions, and the Lab catalog.
Secrets, downloaded Instagram media, extracted transcripts, queue/staging state,
live benchmark registry, archived benchmark runs, logs, and private local handoffs
remain outside the published snapshot. The raw Session 3 pilot record remains
excluded, as in the prior sanitized public repository. Moving an active deployment
requires transferring its runtime state separately; cloning this repository starts
with the versioned project state.
