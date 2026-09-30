# Instagram to Value

A durable Python pipeline that turns a submitted Instagram post into a reviewed knowledge proposal. It separates ingestion, extraction, interpretation, and installation so a failed process can resume from disk and untrusted content never changes a local agent configuration without explicit approval.

## Why it is interesting

The project treats short-form social content as unreliable input to a stateful data system:

- **Durable ingestion.** A dedicated, allowlisted Telegram bot validates URLs and writes jobs to an on-disk queue. The worker is idempotent, processes the oldest queued job first, and recovers orphaned running jobs after interruption.
- **Layered extraction.** The fetch stage persists media locally; extraction routes audio through ASR and image posts through local OCR, escalating low-confidence or garbled results to independent OCR providers before reconciliation.
- **Safe value extraction.** An agent turns extracted context into typed action proposals rather than executing free-form output. Every action is schema-validated, staged on disk, classified by risk, and surfaced for Telegram approval.

## Architecture

```text
Telegram URL
    │  validate + deduplicate
    ▼
jobs/queued/<shortcode>.json ──► worker ──► fetch ──► media/ (ignored)
    ▲                                │
    │ restart recovery                ▼
jobs/running|done|failed      extracted/<shortcode>.json (ignored)
                                         │
                                         ▼
                                interpret + schema validation
                                         │
                                         ▼
                              staging/<shortcode>/proposal.json (ignored)
                                         │
                              Telegram approval / rejection
                                         ▼
                              allowlisted artifact installation
```

## State and contracts

The filesystem is the checkpoint boundary between every major stage. `jobs_lib.py` owns job transitions across `queued`, `running`, `done`, and `failed`; `staging_lib.py` owns proposal paths and action state. This makes restarts observable and avoids relying on process memory for correctness.

Action handlers register a narrow contract: a type, risk tier (`inert`, `config`, or `exec`), payload schema, preview, collision check, and installer. The interpretation stage rejects an entire invalid action list rather than partially applying an agent response. Installation is constrained to allowlisted destinations and collision-safe filenames.

## Failure handling

- A low-memory guard defers ASR work rather than risking an out-of-memory failure.
- Network-facing notification failures are non-fatal to job processing.
- The worker requeues interrupted jobs and records terminal failures on disk.
- OCR ambiguity is escalated and reconciled; a hidden-tool resolver reports `resolved`, `uncertain`, or `unresolved` instead of silently promoting a guess.
- No proposal is installed automatically from a manually submitted post. Risky actions always remain reviewable.

## Local setup and verification

The automated test suite covers pure orchestration, state transitions, validation, safety boundaries, and prompt/action parsing. It does not call Telegram, Instagram, model APIs, or local ASR/OCR models.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

For live integrations, place only the required values in `~/.config/instagram-to-value/secrets.env` (see `.env.example` for names). Instagram cookies are likewise read from `~/.config/instagram/cookies.txt`; neither location is part of the repository. Fetching also requires locally installed `yt-dlp`, `gallery-dl`, and `ffmpeg`; model-backed extraction needs its corresponding runtime and model files.

## Repository hygiene

Downloaded media, extracted content, queue and staging state, logs, cookies, and secrets are intentionally ignored. Use the code only with content you are entitled to process and in accordance with Instagram's terms and applicable law.

## Development

The latest verified implementation and remaining work are recorded in
[Current working state](docs/WORKING_STATE.md). This includes proposal provenance,
skill installation, the benchmark gate, and the [Lab artifact catalog](artifacts/INDEX.md).

Continuous integration runs the same `pytest -q` command on pushes and pull requests. The codebase is intentionally organized as small scripts with explicit filesystem contracts, keeping the pipeline inspectable without a framework or database layer.

Licensed under the [MIT License](LICENSE).
