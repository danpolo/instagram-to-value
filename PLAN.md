# instagram-to-value — Revised Spec & Implementation Plan

**Status:** decisions locked · 2026-08-25
**Goal:** Instagram post URL → extracted content → Claude-authored knowledge artifact (skill / rule / workflow / reference / prompt) or a logged discard.

---

## 0. Fact-check of the original spec

Verified before planning. Five of five model names are real; two assumptions are wrong.

| Claim | Verdict | Notes |
|---|---|---|
| Qwen3-ASR-1.7B, local | ✅ real | Apache-2.0, Alibaba/Qwen. **Also does language ID natively** — collapses your separate "language detection" stage. |
| Caspi-1.7B, Hebrew, local | ✅ real | OzLabs. A **fine-tune of Qwen3-ASR-1.7B** — same architecture, so one runtime serves both. ~5% WER on Hebrew. GGUF quant exists. |
| Groq Whisper Large V3 fallback | ✅ real | Good universal fallback. |
| PP-OCRv6 local OCR | ⚠️ real, **but no Hebrew** | 50 languages = CJK + English + 46 **Latin-script**. No RTL/Semitic scripts at all. |
| Gemini 3.7 Flash | ✅ real | Current Flash tier. |

### Correction 1 — Hebrew OCR has no local path

PP-OCRv6 cannot read Hebrew. Your "local primary, VLM fallback" ordering holds for
English/Latin only. For Hebrew images the VLM is not a fallback, it is **the only
option**. Routing must branch on script, not on difficulty:

```
image → script detect → Latin/CJK ? PP-OCRv6 (local)
                      → Hebrew/RTL ? Gemini 3.7 Flash  (Google Cloud Vision as 2nd)
                      → PP-OCRv6 low confidence ? escalate to Gemini
```

> **Superseded twice — see §7's Phase 2 design note and the 2026-08-30 entry.**
> "Script detect" was collapsed into a single confidence gate (2026-08-26), and
> the single Gemini escalation target became a two-vendor chain after the quota
> wall (2026-08-30). Current shape:
> ```
> image → PP-OCRv6 (local) → confident & not garbled ? done
>                          → else escalate: OCR.space Engine 3   (authoritative)
>                                         + Gemini Flash rotation (corroborating)
>                                         → reconcile.py diffs the two
> ```

### Correction 2 — there is no GPU on this machine

```
CPU : Intel i5-6200U — 2 cores / 4 threads, Skylake (2015)
GPU : Intel HD 520 integrated — no CUDA (torch reports cuda=False)
RAM : 15 GB total, ~5.6 GB available (9.9 GB already in use)
Disk: 726 GB free
```

Two 1.7B ASR models on a 2-core laptop CPU is viable **only** because your volume is
~5 min/day. Consequences that must be designed in, not discovered later:

- **Async by construction.** Expect roughly 3–10× realtime on this CPU, and you have
  authorized up to 30×: a 5-minute reel may take anywhere from 15 min to 2.5 hours.
  Telegram must acknowledge on receipt and notify on completion. A synchronous
  "send URL, get transcript" UX is not achievable here at any precision setting.
- **One model resident at a time.** ~5.6 GB free will not hold two 1.7B models plus
  PaddleOCR. Load-run-unload, never keep both warm. See §0b — at fp16 this is not a
  preference but a hard requirement.
- **Precision over speed.** See §0b: because latency is nearly free, run fp16/Q8 rather
  than the aggressive Q4 quant a latency-bound design would pick.
- **RSS, not speed, is the binding constraint.** Phase 0 measures it first.

### Correction 3 — Telegram cannot work the way the spec implies

Measured, not assumed: the Claude Code Telegram plugin **binds to exactly one session**
(`server.ts` exits if it is not the bridge session) and is currently pointed at
`/home/dan/projects/AbuAliArchive`. It also only fetches document attachments on demand,
and inbound messages reach that one paired session — not this project.

So "the server downloads and transcribes it" cannot be the Claude bridge. Ingest must be
a **standalone bot process** that owns nothing but the job queue. This is better anyway:
transcription then does not depend on a Claude session being alive.

---

## 0b. Decisions locked

| Decision | Choice | Consequence |
|---|---|---|
| ASR siting | Accuracy is priority #1. Phase 0 decides, **budget up to 30× realtime** | A 5-min reel may take 150 min. That is a huge budget — it buys precision settings elsewhere. |
| Approval | Staging + Telegram approve/reject | Nothing reaches `~/.claude/` unattended |
| Ingest | New dedicated bot + own token | You create it via @BotFather; fully decoupled from the bridge |

### What the 30× budget actually buys

Three design changes follow directly, and they matter more than the latency relaxation itself:

**1. Do not quantize aggressively.** The plan originally said "use GGUF Q4". With
latency effectively free, that is now the wrong call — quantization trades accuracy for
speed, and we no longer want that trade. Run **fp16 or Q8**. 1.7B at fp16 ≈ 3.4 GB of
weights, which fits the ~5.6 GB available, though tightly; Q8 (~1.8 GB) is the safe
default with negligible accuracy loss. Phase 0 tests both.

**2. Groq Whisper v3 is an *availability* fallback, not an accuracy upgrade.**
Qwen3-ASR-1.7B **outperforms Whisper-large-v3** on MLS, Common Voice and MLC-SLM per
the Qwen3-ASR technical report. So falling back to Groq means accepting *worse*
transcripts, not better ones. It should fire only when local inference fails or OOMs —
never as a routine speed shortcut.

**3. Transcribe twice and reconcile.** At 5 min/day the cost is trivial and the accuracy
gain is real. Run the local model *and* Groq, diff the two transcripts, and have Claude
reconcile — flagging spans where they disagree. This matters specifically because
**ASR fails hardest on proper nouns, and tool names are proper nouns.** The
hidden-tool-name resolver is the highest-value part of this pipeline, and disagreement
between two engines is the single best signal for "this token is unreliable, verify it."

### Note on model ceiling

The open Qwen3-ASR series is **0.6B and 1.7B only** — 1.7B is the top. There is no
larger checkpoint to spend the latency budget on, so the budget goes into precision
(fp16/Q8), dual-engine reconciliation, and full-file rather than chunked inference.

---

## 1. Revised architecture

Four decoupled stages. Each writes to disk and exits; nothing holds state in memory
across stages. A crash resumes from the last completed stage.

```
┌── STAGE 1 · INGEST ──────────────────────────────────────────┐
│  standalone Telegram bot (own token, own process)            │
│  URL in → validate → jobs/queued/<shortcode>.json → ack      │
│  Cost: instant. Never blocks.                                │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌── STAGE 2 · FETCH ───────────────────────────────────────────┐
│  yt-dlp (video) / gallery-dl (images) + ~/.config/instagram/ │
│  → mp4, 16 kHz mono WAV, caption, comments, thumbnails       │
│  → media/<shortcode>/                                        │
│  Branch decision made HERE from yt-dlp's own error:          │
│    "No video formats found!" → image track                   │
│    (verified 2026-08-25; original guess "There is no video   │
│     in this post" was wrong, see §9)                          │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌── STAGE 3 · EXTRACT ─────────────────────────────────────────┐
│  VIDEO: Qwen3-ASR-1.7B → reads its own detected language     │
│         └─ if Hebrew → re-run through Caspi-1.7B             │
│         └─ if fails/timeout → Groq Whisper Large V3          │
│  IMAGE: script detect → PP-OCRv6 (Latin/CJK)                 │
│         └─ Hebrew or low confidence → Gemini 3.7 Flash       │
│         all carousel slides, in order                        │
│  → extracted/<shortcode>.json                                │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌── STAGE 4 · INTERPRET (Claude Code) ─────────────────────────┐
│  transcript + caption + comments + creator replies           │
│  → resolve hidden tool names (web search)                    │
│  → classify: skill|rule|workflow|reference|prompt|discard    │
│  → write PROPOSAL to staging/ (not straight to ~/.claude/)   │
│  → Telegram: summary + proposed type + diff → you approve    │
└──────────────────────────────────────────────────────────────┘
```

### Why a staging gate is non-negotiable

Artifacts land in `~/.claude/skills/` and `~/.claude/rules/` — **global, loaded into
every future Claude session on this machine.** An auto-installed bad skill silently
degrades all downstream work and is hard to trace back. One Instagram reel should never
be able to write a global rule unattended. Staging + one-tap Telegram approval costs you
seconds per item and removes the entire failure class.

---

## 2. Component decisions

| Stage | Choice | Why | Fallback |
|---|---|---|---|
| Ingest | standalone python-telegram-bot | Bridge is occupied and single-session | — |
| Video fetch | yt-dlp + cookies | Proven: mp4/mp3/WAV/caption/comments | — |
| Image fetch | gallery-dl + cookies | Handles carousels; yt-dlp cannot | — |
| Audio prep | ffmpeg `-ac 1 -ar 16000 -c:a pcm_s16le` | Already proven working | — |
| ASR (en) | Qwen3-ASR-1.7B **fp16/Q8** | Native LID; beats Whisper-large-v3 | Groq (availability only) |
| ASR (he) | Caspi-1.7B **fp16/Q8** | Same arch as above → one runtime | Groq (availability only) |
| OCR (Latin/CJK) | PP-OCRv6 mobile/small | 5.2× CPU speedup w/ OpenVINO; tiny | OCR.space E3 → Gemini rotation |
| OCR (Hebrew) | **OCR.space Engine 3** | PP-OCRv6 has no RTL support. Byte-exact on the Hebrew test carousel, ~2s/slide, and it does *not* invent text for illegible regions (§4) | **Gemini Flash rotation** (3.5→3.6→3.7→3.5-lite), which doubles as the reconciliation second opinion |
| Interpret | Claude Code | — | — |

**Simplification worth taking:** Caspi is a Qwen3-ASR fine-tune. Build **one** inference
wrapper with a swappable checkpoint path, not two integrations.

---

## 3. Language routing

Do not build a separate language-detection stage — Qwen3-ASR emits language itself.

```
audio → Qwen3-ASR-1.7B (first 60 s)
        → lang == 'he' ?  full re-run through Caspi-1.7B
        → else          keep the Qwen3 output, continue full file
```

Costs one short extra pass, removes a whole dependency, and uses the ASR model's own
judgment rather than a second opinion that can disagree with it.

---

## 4. The hidden-tool-name resolver

Your sharpest requirement, and the one most likely to produce garbage if built naively.
The pattern: creator demos a tool, never names it, says *"comment X and I'll DM the link"*.

Resolution order, cheapest first:

1. **Creator's own replies in the comments.** Usually the link is literally there. We
   already capture comments via yt-dlp — but note the anonymous run got only **14 of 256**.
   With cookies this improves; treat comment coverage as partial regardless.
2. **Caption + first-comment link.** Creators often park the link there.
3. **On-screen text.** Run OCR over sampled video frames — tool names are frequently
   visible in the UI being demoed even when never spoken.
4. **Web search** on a description synthesized from the transcript.

**Confidence gate.** The resolver must emit `resolved | uncertain | unresolved` and
never silently promote a guess to fact. `uncertain` records the candidate *and* the
reasoning; `unresolved` stores the description so you can identify it later. A
confidently-wrong tool link written into a global rule is worse than no link.

---

## 4.5 Account discovery & watchlist

**Locked 2026-08-26.** New capability: ingesting any post auto-enrolls its
creator for a capped backfill and ongoing tracking. Fully automatic, no
confirmation step — spans Phase 3 (trigger) and Phase 5 (ongoing watch), not
a phase of its own.

**Trigger — whenever a post URL is ingested** (Phase 3's bot, or the CLI
until then), after resolving the post itself:

1. **Resolve the creator's username.** Already available for free — yt-dlp's
   `uploader`/`uploader_id`, gallery-dl's `username`/`owner_id` fields, both
   already captured by the Phase 1 fetch layer.
2. **Enroll if new.** If `pages/<username>.json` doesn't exist, create it:
   ```
   {
     "username": "...",
     "first_seen": "...",
     "backfill": {"posts_cap": 50, "days_cap": 90, "done": false},
     "last_checked": null,
     "newest_known_shortcode": null
   }
   ```
   `posts_cap`/`days_cap` are placeholders — tune once real account sizes are
   known. **Whichever cap is hit first stops the backfill** (50 posts, or 90
   days back, whichever comes first).
3. **Capped backfill, throttled.** Enumerate the account's posts newest-first
   (authenticated — see open question below) and enqueue into
   `jobs/queued/` up to the cap. Uses the same delay+jitter pattern as
   `scripts/scan_carousel.py` — **a "trusted" backfill is not an exemption
   from throttling**; the automation flag on this account happened during
   exactly this kind of multi-post scan. Set `backfill.done = true` and
   `newest_known_shortcode` to the newest post found once complete — that
   shortcode becomes the cursor for step 4.
4. **Watchlisted implicitly** — existence of `pages/<username>.json` with
   `backfill.done = true` means "watched." No separate watchlist file needed.

**Ongoing watch — Phase 5, daily** (folds into the already-planned systemd
timer / weekly digest, not a separate scheduler):

- For each enrolled account, re-list only the *newest page* of posts — not a
  full history re-enumeration. Cheap, and matches the daily cadence.
- Walk newest-first until `newest_known_shortcode` is hit, enqueue everything
  newer, then stop (standard incremental-crawl early-exit).
- Update `newest_known_shortcode` and `last_checked`.
- Idempotency is already free: jobs are keyed by shortcode (§6) and
  `--download-archive` no-ops anything already fetched, so an overlap in
  listing never double-processes a post.

**Open questions — settle when Phase 3/5 actually get built, not blocking
Phase 1/2:**
- ~~Exact backfill numbers (50 posts / 90 days are placeholders).~~
  **Kept as-is 2026-08-30** — Phase 3 shipped with these defaults; no reason
  found yet to tune them (first real enrollment, `@chase.h.ai`, hit the
  50-post cap cleanly with real data). Revisit if a watched account's backfill
  turns out too small/large in practice.
- ~~Enumeration mechanism~~ **Resolved 2026-08-30.** Not the internal
  `user_feed`/`user_by_name` Python API — the plain CLI form works and is
  simpler: `gallery-dl <profile>/posts/ --post-range 1-N -j --sleep-request
  8-18`, authenticated via the same cookie jar, verified live against
  `@networkchuck` and `@chase.h.ai`. See `scripts/discover_account.py`.
- No "untrack" mechanism designed yet — add one if an account ever needs
  removing from the watchlist. Still open; `discover_account.py`'s
  `enroll_if_new` also has no retry path for a failed listing (the page file
  it wrote blocks a naive re-run) — same underlying gap.

---

## 5. Knowledge taxonomy

> **⚠️ SUPERSEDED 2026-08-30 by
> `docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md`.**
> These six types were tested against all 7 posts in `extracted/` and capture
> **1 of 7**; the most common real outcome (creator demos a tool → install it)
> has no row here at all. Phase 4 replaces the table with an extensible
> **action registry**, and a proposal became a *list* of actions rather than one
> classification. The six types survive as registry entries, and the
> "default to the weakest artifact" principle below still holds. The
> `reference` destination also changed — see the spec's "Where knowledge
> actions install". Read the spec before using this table.

| Type | Destination | Test for choosing it |
|---|---|---|
| skill | `~/.claude/skills/<name>/SKILL.md` | A repeatable *procedure* Claude should follow |
| rule | `~/.claude/rules/<topic>.md` | An always-on *constraint* (matches existing `context7.md`) |
| workflow | `.claude/workflows/<name>.md` | Multi-step orchestration across agents |
| reference | `~/.claude/projects/-home-dan/memory/<slug>.md` + MEMORY.md line | A *fact* or pointer — fits the existing memory schema |
| prompt | `~/.claude/templates/<name>.md` | Reusable text, no logic |
| discard | `logs/discarded.jsonl` w/ reason | Keep the reason — it tunes the classifier |

Default to the **weakest** artifact that captures the value. Reference notes are cheap
and harmless; global rules are expensive and intrusive. When torn, write a reference and
let it be promoted later if it proves useful.

---

## 6. State & idempotency

```
instagram-to-value/
├── jobs/{queued,running,done,failed}/<shortcode>.json
├── media/<shortcode>/          # mp4, wav, jpgs, caption, comments
├── extracted/<shortcode>.json  # transcript or OCR + lang + engine + confidence
├── staging/<shortcode>/        # proposed artifact, awaiting approval
├── pages/<username>.json       # watchlist entry: backfill state + newest_known_shortcode (§4.5)
└── logs/{pipeline,discarded}.jsonl
```

Keyed by shortcode throughout, so a re-sent URL is a no-op. `yt-dlp --download-archive`
gives the same guarantee at the fetch layer. Every stage records which engine ran and
how long it took — that log is what tells you whether local ASR is actually worth
keeping versus going Groq-first.

---

## 7. Implementation phases

### Phase 0 — Measure before building *(do this first)* — ✅ DONE 2026-08-25
The local-model premise rested on two unmeasured numbers: peak RSS and WER. Measured.
- Installed `Qwen/Qwen3-ASR-1.7B-hf` (official, transformers-native, `transformers>=5.13.0`)
  in `~/.local/venvs/qwen-asr`. Ran fp16 via `scripts/phase0_benchmark.py` against
  `out/DW1O6ZBEfDa.16k.wav` (178 s), wrapped in `systemd-run --user --scope -p MemoryMax=`
  so a runaway load gets cleanly OOM-killed in its own cgroup instead of risking the
  system OOM-killer picking an unrelated process. Result: **6.9 GB peak RSS**, load
  183 s + generate 1268 s = **7.1× realtime** (well under the 30× budget), transcript
  quality high — all named tools/products transcribed correctly (Mem Palace, AAK,
  Readwise, Obsidian, Morpheus, Multipass) except one proper noun, see below.
- **Decision gate result: fp16 primary.** RSS fits comfortably in the ~7.4 GB available
  once nothing else heavy is running, speed is 4× inside budget. Did **not** test Q8 —
  the decision gate's "expected outcome" branch already hit on the first try, and the
  only Q8 paths found are unofficial/unverified (a third-party llama.cpp patch pulling
  weights from a non-HF domain, or an obscure "crispasr" runtime) — not worth the
  trust/build cost when fp16 already clears the bar. Revisit only if operational RAM
  pressure (see below) turns out to bite often.
- **Operational finding, not in the original plan:** peak RSS (6.9 GB) is real and
  competes with whatever else is running. The first attempt hit a hard stall — not OOM,
  a *livelock* — when another AbuAliArchive process (nightly eval, 5.8 GB RSS) was
  running concurrently: this machine's swap is only ~975 MB and was already 100% full,
  so a cgroup memory throttle had nothing to reclaim into and the process wedged in
  `mem_cgroup_handle_over_high` instead of degrading gracefully. Fixed by asking Dan to
  stop the competing job. **Phase 3/5 job runner should check available RAM (≥ ~7.5 GB)
  before starting an ASR job and queue/wait otherwise, rather than assume it's free.**
- **Proper-noun accuracy check (the part that matters most):** transcript wrote
  "**Mila** Jovovich"; the actual caption spells it "**Milla** Jovovich" (double-L).
  Everything else checked out. This is exactly the class of error dual-engine
  reconciliation (§0b.3) is meant to catch — a single-engine run would have silently
  shipped the wrong spelling into a knowledge artifact.
- **Reconciliation harness built:** `scripts/reconcile.py` — difflib-based word-level
  diff between local and Groq transcripts, emits disagreement spans with context for
  Phase 4 to verify. Not yet run for real — blocked on the Groq API key (open item #2).

### Phase 1 — Fetch layer — ✅ DONE 2026-08-26
Extend the proven yt-dlp path; add gallery-dl carousel handling; auto-branch video vs
image from yt-dlp's error (verified string: `"No video formats found!"`, not the
original guess — see §9).
*Done when:* one video post and one multi-slide carousel both land complete on disk. **Met.**
- ✅ `scripts/fetch.py` built. Video track verified end-to-end against
  `DW1O6ZBEfDa` → `media/DW1O6ZBEfDa/`. **mp4 and thumbnail are off by
  default** (`--keep-mp4` / `--keep-thumbnail` to opt in) — default run
  downloads audio-only (smaller/faster, no video stream fetched at all) and
  produces 3 files: `16k.wav`, `description`, `info.json` (14/256 comments).
  With both flags: 5 files, adds `mp4` + `jpg`. Clean either way — no
  leftover DASH fragments (see fix note in §9).
- ✅ Carousel track verified end-to-end against `DLKrzPTvTh2` (6-slide post,
  found via `scripts/scan_carousel.py`) → `media/DLKrzPTvTh2/`. All 6 slides
  fetched **in order** (`DLKrzPTvTh2_01.jpg` … `_06.jpg`), each with a
  metadata sidecar, plus `description`/`info.json` from the pre-branch yt-dlp
  attempt. Auto-branch fired correctly on the verified `"No video formats
  found!"` marker.
- ⚠️ **Real gallery-dl comment coverage: 0 of 40**, worse than the video
  track's 14/256 — see fact below. Flagged, not fixed (Phase 4 concern, not
  Phase 1).

### Phase 2 — Extract layer — ✅ DONE 2026-08-30 (unblocked by a second OCR vendor)
ASR wrapper with swappable checkpoint (Qwen3 ↔ Caspi); Groq fallback; PP-OCRv6 + Gemini
routing by script.
*Done when:* an English reel, a Hebrew reel, and a Hebrew carousel all produce text.

- ✅ **English reel (`DW1O6ZBEfDa`):** `python3 scripts/extract.py DW1O6ZBEfDa` → `extracted/DW1O6ZBEfDa.json`.
  `engine_primary: qwen3-asr-1.7b`, `engine_fallback: groq-whisper-large-v3`,
  reconciliation agreement 87.1% (53 disagreement spans flagged for Phase 4).
  Full coherent transcript, matches Phase 0's known content (incl. the same
  "Mila"/"Milla" Jovovich proper-noun miss).
- ✅ **Hebrew reel (`DcblDAVNrgn`):** same command → `extracted/DcblDAVNrgn.json`.
  `asr_qwen.py`'s 60s peek correctly detected Hebrew and rerouted to
  `asr_caspi.py` (separate venv — see below); `engine_primary: caspi-1.7b`,
  reconciliation agreement 68.7% against Groq (lower than English, expected —
  Hebrew ASR is harder; Caspi's output also ran shorter than Groq's, likely
  hitting `max_new_tokens`, worth revisiting if it matters in practice).
  Fluent, correct Hebrew transcript, closely matching Groq's independent read.
- ✅ **Hebrew carousel (`DcgAqIADbs2`):** `python3 scripts/extract.py DcgAqIADbs2`
  → `extracted/DcgAqIADbs2.json`. All **5 slides in 1m40s** (PP-OCRv6 dominates
  the wall clock; the two API calls are ~2s and ~9s per slide). PP-OCRv6
  escalated on every slide as designed (mean confidence 0.58–0.80, all either
  below the 0.70 gate or caught by `looks_garbled`), each slide then read by
  **OCR.space Engine 3** (authoritative) and **gemini-3.5-flash**
  (corroborating), and the two diffed by `reconcile.py`.
  **Cross-engine agreement: 100% on slides 02/03/04, 87.5% on 01, 80.2% on 05.**
  - Every disagreement span falls in the *blurry background signage*, not the
    headline/body text — which is exactly the design intent. On slide 01
    Gemini added a distant shop sign (`"מספרה כלבו"`) that OCR.space declined
    to guess at; on slide 05 OCR.space read protest placards as the fragments
    it could actually see (`"מוקרטיה אדמ קדמיה"`) while Gemini confidently
    completed them into `"דמוקרטיה או מרד / דיקטטורה או מרד"`.
  - The single body-text disagreement in the whole carousel was one definite
    article — `"השתקפות"` vs `"ההשתקפות"` — caught by reconciliation.
  - This is §0b.3's dual-engine argument demonstrated for OCR: two independent
    engines agreeing exactly on the content that matters, and disagreeing
    precisely where the image is genuinely unreliable. Phase 4 gets those
    spans flagged instead of a smooth, confident, partly-invented transcript.
  - Unit tests: `scripts/test_extract.py`, **47 tests** (was 23).

**Blocker resolved (2026-08-30).** The original block was diagnosed as "the
Gemini key is out of quota." It was actually **one model** being out of quota —
free-tier limits are metered per model family (see §9's 2026-08-30 facts). Two
independent fixes, both live:
1. `ocr_ocrspace.py` — **OCR.space Engine 3** is now the escalation primary.
   2,500 Engine-3 requests/month free (~83/day vs this pipeline's ~5), email-only
   signup, byte-exact on the Hebrew test carousel, and it doesn't hallucinate.
2. `ocr_gemini.py` — **rotates across Gemini model families** on 429/5xx instead
   of sleeping out the window, so one exhausted model no longer stops the run.

Net: the stage now survives losing either vendor entirely, and only fails when
*both* fail. `OCRSPACE_API_KEY` joins `GROQ_API_KEY`/`GEMINI_API_KEY` in
`~/.config/instagram-to-value/secrets.env`.

**Design deviations found while building (see git log for detail):**
- **Caspi needs its own venv.** Caspi's loader (the `qwen_asr` PyPI package)
  hard-pins `transformers==4.57.6`, incompatible with the `transformers>=
  5.13.0` that `Qwen3-ASR-1.7B-hf` needs — installing it into the `qwen-asr`
  venv silently downgraded transformers and would have broken Phase 0's
  validated setup. Caspi now lives in its own `~/.local/venvs/caspi-asr` venv;
  `asr_qwen.py` (peek + full-if-non-Hebrew) and `asr_caspi.py` (Hebrew-only,
  separate process) replace the single `asr_local.py` originally planned.
- **Qwen3-ASR emits full language names** ("English", "Hebrew"), not ISO
  codes — the original `language == 'he'` routing check would never have
  fired. Now case-insensitive match on `"hebrew"`.
- **The 0.70 mean-confidence OCR gate alone isn't reliable enough.** PP-OCRv6
  forces Hebrew glyphs into its Latin/digit charset and can score confidently
  wrong (0.78-0.80 on pure noise). Added a second gate, `looks_garbled()`
  (>25% digit ratio in the recognized text) — real Latin/CJK text is nowhere
  near that digit-heavy.
- **`fetch.py`'s shortcode regex** only matched `instagram.com/p|reel/<code>`,
  not `instagram.com/<user>/reel/<code>` (the form Instagram actually links).
  Fixed.
- Added retry-with-backoff to `ocr_gemini.py` for transient 5xx/timeouts and
  429s (honoring the API's own suggested wait) — hit repeatedly in practice.

**Design locked 2026-08-26** (resolves the two ambiguities in §0/§1 vs §0b.3):

- **Reconciliation policy resolved:** §0b.3 ("transcribe twice and reconcile, cost is
  trivial") wins over the §1 Stage-3 diagram's "Groq only on failure" reading. Every
  audio job runs local ASR **and** Groq, always, and `reconcile.py` diffs them.
  **Extended to OCR 2026-08-30:** the same argument is about having two engines,
  not about audio. Every *escalated* slide now runs OCR.space **and** Gemini and
  diffs them. It only applies to escalated slides — a confident PP-OCRv6 read
  has nothing to reconcile against and shouldn't spend API quota.
- **"Script detect" (§0 Correction 1) isn't literally buildable** — you can't classify
  an image's script without OCR'ing it, which is the thing being routed. Collapsed into
  one rule instead of two branches: **always run PP-OCRv6 first; escalate to Gemini 3.7
  Flash whenever its confidence is low.** This already covers Hebrew correctly with no
  separate branch — PP-OCRv6 has no RTL model at all, so it emits nothing/garbage on
  Hebrew glyphs, which *is* low confidence, which escalates.
- **Module layout** (mirrors `fetch.py`'s conventions — argparse, JSON-to-stdout,
  fail-loud `SystemExit`, no test suite, matches repo-wide convention):
  - `scripts/extract.py` — entrypoint; given a shortcode, detects what's in
    `media/<shortcode>/` (wav vs jpgs) and runs the right pipeline; writes
    `extracted/<shortcode>.json`. Checks available RAM (≥~7.5GB, per the Phase 0
    livelock finding) before loading a local ASR model and fails loudly rather than
    risking it — real queueing is Phase 5's job, this is just a guard rail.
  - `scripts/asr_local.py` — wraps the Phase 0 load/transcribe pattern as an
    importable function, parameterized by checkpoint (`Qwen/Qwen3-ASR-1.7B-hf`
    default, Caspi via `--model-dir`); implements §3's routing (Qwen first 60s →
    if `language=='he'`, re-run full file through Caspi).
  - `scripts/asr_groq.py` — new; calls Groq Whisper Large V3, reads `GROQ_API_KEY`
    from `~/.config/instagram-to-value/secrets.env` (mode 600, outside repo, same
    pattern as the IG cookies).
  - `scripts/ocr_local.py` — PP-OCRv6 first pass (built in its own venv, not the
    single `ocr.py` originally planned).
  - `scripts/ocr_ocrspace.py` — **added 2026-08-30**; OCR.space Engine 3, the
    escalation primary. Reads `OCRSPACE_API_KEY` from the same secrets file.
  - `scripts/ocr_gemini.py` — Gemini escalation secondary *and* reconciliation
    second opinion, rotating across model families. Reads `GEMINI_API_KEY`.
  - `scripts/reconcile.py` — already built (Phase 0), reused as-is.
- **`extracted/<shortcode>.json` schema** unifies transcript/OCR under one shape:
  `text`, `language`, `engine_primary`, `engine_fallback`, `confidence`,
  `engine_seconds`, `reconciliation` (audio jobs), `slides` (per-slide OCR array, in
  order, for carousels). **Added 2026-08-30:** each escalated slide also carries
  `engine_secondary` (the corroborating OCR engine, or `null` when only one was
  reachable) and its own per-slide `reconciliation` — so OCR jobs now carry the
  same dual-engine evidence audio jobs already did.
- **Test samples sourced and fetched this session** (§9 has the fetch summaries):
  - English reel: `DW1O6ZBEfDa` (already on disk from Phase 0/1)
  - Hebrew reel: `DcblDAVNrgn` — @pnaiplus, Hebrew-language podcast interview
    ("הברזייה"), confirmed real spoken Hebrew (not just music)
  - Hebrew carousel: `DcgAqIADbs2` — @ynetgram (Hebrew news outlet), slide 1
    confirmed by eye to have Hebrew headline text baked into the image
- **`fetch.py` shortcode regex fixed:** only matched `instagram.com/p|reel/<code>`,
  not the `instagram.com/<user>/reel/<code>` form Instagram actually links to (found
  while fetching the Hebrew reel above). Now `instagram\.com/(?:[^/]+/)?(?:p|reel|reels)/...`.

### Phase 3 — Ingest bot — ✅ DONE 2026-08-30
Standalone Telegram bot, own token, allowlisted to your chat ID. URL → job file → ack.
Progress notifications on stage transitions. Also triggers account
discovery/enrollment per §4.5 — resolves the post's creator and, if new,
kicks off a capped, throttled backfill into the same job queue.
*Done when:* a URL sent from your phone produces a queued job and an ack within 2 s.

- ✅ **Built:** `scripts/jobs_lib.py` (job-state helpers), `scripts/telegram_notify.py`
  (send-only Telegram helper), `scripts/discover_account.py` (§4.5 account
  discovery + capped backfill), `scripts/telegram_bot.py` (the ingest bot),
  `scripts/worker.py` (queue drainer/orchestrator). Design spec:
  `docs/superpowers/specs/2026-08-30-phase3-ingest-bot-design.md`. Plan:
  `docs/superpowers/plans/2026-08-30-phase3-ingest-bot.md`. 21 new pure-logic
  unit tests, all passing (68 total with Phase 2's, no regressions).
- ✅ **`discover_account.py` verified live** against the real `@networkchuck`
  account (`--posts-cap 3` for a bounded test run): `pages/networkchuck.json`
  enrolled with `backfill.done: true`, 3 real posts queued into
  `jobs/queued/`. Found and fixed a real bug in the process: `now` (UTC-aware)
  minus gallery-dl's naive `post_date` strings raised `TypeError` on every
  live call — the pure functions' unit tests used naive datetimes throughout
  and never caught it.
- ✅ **`worker.py --once` verified live** draining those 3 real jobs end to
  end: fetch → extract → enroll → done, with real Telegram progress messages
  sent at each stage. All 3 succeeded (0 failed): one image post (OCR, fast),
  two audio reels (local ASR via `qwen3-asr-1.7b`, ~2-4 min CPU time each on
  this 2-core machine — confirms Phase 0's realtime-factor budget holds under
  real backfill load). Transcripts matched the real post content (e.g. the
  termshark/tshark reel transcribed correctly). Found and fixed a second real
  bug: `resolve_creator_username` preferred yt-dlp's `uploader_id`, which
  turned out to be the numeric account ID on one post, not a username —
  enrolled a bogus duplicate `pages/4440726664.json` for an already-tracked
  account. Now prefers `channel` (the real lowercase handle) and lowercases
  consistently so both fetch tracks key the same account the same way.
- ✅ **Done-criterion met live:** a real reel URL sent from Dan's phone
  (`instagram.com/reel/DbsDXkgJ_FB/?igsi=...`) produced
  `jobs/queued/DbsDXkgJ_FB.json` with the real `chat_id` and the tracking
  query param correctly stripped by `extract_shortcode`'s regex, with a
  synchronous ack — the handler's only work between receiving the message
  and replying is one idempotency check and one file write, well under the
  2s bar.

### Phase 4 — Interpret + staging gate — 🔨 DESIGN DONE 2026-08-30, plan pending
Claude Code reads extracted output, runs the tool resolver, classifies, writes a proposal
to `staging/`, and sends you a summary with an approve/reject action.
*Done when:* an approved proposal installs to the right path and a rejected one logs its reason.

**Design spec:** `docs/superpowers/specs/2026-08-30-phase4-interpret-staging-design.md`
(442 lines, committed `15f8d9f`). Read it before touching Phase 4 — it supersedes
§5 and deviates from §4 and §2 in five recorded ways. Decisions locked with Dan:
- A proposal is an **ordered list of actions**, not one classification.
- **Pluggable agent backend** — headless `claude -p` (default) and headless
  `codex exec`, switched from the bot with `/agent`. Both verified installed.
- §4's **tier 3 (frame OCR) moves last** and re-fetches the mp4 lazily, so the
  default run stays audio-only.
- Knowledge installs to a **global on-demand skill**,
  `~/.claude/skills/captured-knowledge/`; `~/.claude/rules/` stays reserved for
  always-on constraints.
- Job `origin` drives both notification style (immediate vs digest) and
  auto-discard permission (backfill only, never a hand-sent URL).
- **Exec-tier actions run on approval** (Dan's call), with the literal command
  shown in the preview and every command recorded with its exit code.
- An `unsupported` action type turns the pilot into a ranked backlog in
  `logs/unsupported_actions.jsonl`.

**Implementation is split into two plans** (per the spec's self-review):
**4a** = stage end-to-end + registry framework + the handlers the 7-post corpus
demands; **4b** = the long-tail handlers, informed by the pilot's `unsupported`
log. Neither plan is written yet.

**Why this is the next phase, concretely.** Phase 3's done-notification
(`worker.py:129`) is a status line by design — `✅ <shortcode> done — audio, 2818
chars`. It tells you a job finished, never what the post *said*, so today the only
way to learn the content is to open a session and read `extracted/<shortcode>.json`
by hand. That happened for real on 2026-08-30 (see facts below). Phase 4's "sends
you a summary" is what closes the gap; it is not a missing Phase 3 feature.

### Phase 5 — Automate
systemd user timer or `/loop` drains the queue. Weekly digest of what was created.
Daily: walk the watchlist (§4.5) — re-list each enrolled account's newest
posts, enqueue anything past `newest_known_shortcode`, update the cursor.

**Worker durability — required before the worker becomes long-lived.** Everything
below is harmless today only because `worker.py` has never run outside `--once`.
Turning on `while True` (`worker.py:155`) is what makes them real; do these in the
same change, not after:
- Wrap `note()` (`worker.py:87`) so a Telegram error degrades to a logged warning
  instead of killing the run — and revisit `telegram_notify.send()`'s deliberate
  fail-loud contract, which was written for a one-shot worker (open item #7).
- Requeue orphans on startup: scan `jobs/running/` and move anything found back to
  `queued/`, since a job there means the previous run died mid-flight (open item #8).
  Jobs are idempotent by design (§6), so a replay is safe.
- Check `free -h` available RAM before launching a job — §8 risk #1, ASR peaks at
  6.9 GB of ~9 GB. Unattended draining of 49 queued jobs is where OOM actually bites.

---

## 8. Open risks

1. **Peak RSS, not speed, is now the binding constraint.** With a 30× budget, the way
   local ASR fails is OOM (~5.6 GB free, and 9.9 GB is already in use by other processes).
   If your normal workload grows, fp16 stops fitting. Phase 0 measures headroom, not just
   whether it runs once on an idle machine.
2. **Instagram session lifetime.** Cookies die on logout or when enumeration gets flagged;
   the pipeline must fail loudly with "re-export cookies", not silently produce empty jobs.
3. **Comment coverage is partial** — the link you need may simply not be in what we fetch.
4. **Classifier drift.** Without the discard log, quality decays invisibly. Review monthly.
5. **Hebrew OCR is API-only**, so Hebrew image posts leave the machine. If that is
   unacceptable on privacy grounds, the honest answer is that no good local Hebrew OCR
   option exists today and those posts should be handled manually.
   **Updated 2026-08-30:** still API-only, but no longer *single-vendor* — the
   pipeline now runs two independent providers (OCR.space, Gemini) and degrades
   to either one alone, so a single provider's quota wall can no longer block
   the stage the way it blocked Phase 2. The local search was re-run and closed
   negative: EasyOCR has no Hebrew at all (86 languages; RTL = ar/fa/ug/ur only,
   verified by installing it), Surya v2 is now VLM-based and wants
   vllm/llama.cpp, and no local VLM fits this box anyway — Qwen3-ASR fp16
   already peaks at 6.9 GB of ~9 GB available on 2 cores. Tesseract `heb`
   (92–96% on clean modern print) remains the only real offline floor and is
   *not* installed; it is the fallback of last resort if both APIs are ever
   unacceptable, at a meaningful accuracy cost.

---

## 9. Session state — 2026-08-25

Nothing is committed anywhere: **this directory is not a git repo.** `.gitignore` is
precautionary, for whenever `git init` happens.

### Already on disk (do not re-fetch)

| Asset | Path | Note |
|---|---|---|
| Phase 0 test audio | `out/DW1O6ZBEfDa.16k.wav` | 178 s, 16 kHz mono PCM — the benchmark input |
| Reference video/audio | `out/DW1O6ZBEfDa.{mp4,mp3,jpg}` | English reel, NetworkChuck |
| Caption + metadata | `out/DW1O6ZBEfDa.{description,info.json}` | incl. 14 of 256 comments |
| Enumeration output | `out/urls.txt` (1,597), `out/urls_videos.txt` (635) | full @networkchuck history, 2017-01-12 → 2026-08-24 |
| IG session cookies | `~/.config/instagram/cookies.txt` | **outside the repo**, mode 600, dir 700 |

Installed this session: `yt-dlp` 2026.08.19 and `gallery-dl` 1.32.9 (venvs in
`~/.local/venvs`, symlinked into `~/.local/bin`), static `ffmpeg`/`ffprobe` N-126262.
Also this session: `~/.local/venvs/qwen-asr` — CPU torch, `transformers>=5.13.0`,
accelerate, librosa, soundfile, and the downloaded `Qwen/Qwen3-ASR-1.7B-hf` weights
(3.9 GB on disk at `~/.local/venvs/qwen-asr/models/Qwen3-ASR-1.7B-hf`).

### Open items

| # | Item | Owner | Note |
|---|---|---|---|
| 1 | ~~Phase 0 not started~~ | — | **Done**, see §7. fp16 primary, 6.9 GB peak RSS, 7.1× realtime. |
| 2 | Groq API key | **you** | Needed to actually run `scripts/reconcile.py` for real. Everything else in Phase 0 ran without it. |
| 3 | ~~Telegram bot token via @BotFather~~ | — | **Resolved.** `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_CHAT_ID` were already in `secrets.env` by the time Phase 3 started. |
| 4 | `discover.sh` is superseded | session | The no-auth search-index route (8 posts, 0.5% coverage). Keep as a no-cookie fallback; do not mistake it for the real enumerator. |
| 5 | `~/.local/venvs/instaloader` unused | session | 29 MB. Installed only to prove the anonymous wall; safe to delete. |
| 6 | ~~IG account flagged for automation~~ | — | **Resolved 2026-08-26.** Account cooled down, cookies re-exported (mode 600) — see facts below for what actually went into fixing this. Throttled scanning (`scripts/scan_carousel.py`) subsequently ran 26+ probes with no further auth issues. |
| 7 | A `notify()` failure kills the worker mid-job | session | `worker.py:87` `note()` is unwrapped at lines 89/100/129, and `telegram_notify.send()` raises on error. Deliberate for `--once` (its docstring argues a worker that can't notify should be noticed); wrong for Phase 5's `while True` daemon, where a transient Telegram blip takes down a run meant to last days. **Fix in Phase 5.** |
| 8 | Jobs orphaned in `jobs/running/` are never recovered | session | `drain_once` only iterates `list_queued` (`worker.py:133`); nothing ever reads `running/` back. Any interruption between `worker.py:140` and `:128` — OOM, reboot, or bug #7 — strands the job permanently. Compounds with risk #1: ASR peaks at 6.9 GB of ~9 GB, so mid-job OOM is expected, not hypothetical. **Fix in Phase 5.** |
| 9 | Bugs #7 and #8 compound | session | The `note()` at `worker.py:89` fires *before* fetch, so a notify error there orphans the job (#8). The one at `:129` fires after `move_job`, so state stays correct and only the message is lost. Same root, different severity — fix #7 and #8 together. |

### Facts established this session (do not re-derive)

- **No GPU.** i5-6200U, 2c/4t, Intel HD 520, `torch.cuda.is_available()==False`.
- **Swap is only ~975 MB and runs at or near 100% full as a baseline** on this machine —
  it is not itself a sign of trouble, but it means there is effectively zero swap
  headroom for anything memory-heavy. A cgroup/process that gets throttled expecting to
  page out will wedge (`mem_cgroup_handle_over_high`) rather than degrade. Design job
  scheduling around *available* RAM at launch time, not a fixed assumption.
- **fp16 Qwen3-ASR-1.7B-hf peaks at 6.9 GB RSS** and needs ~7.5 GB available to run
  without risk. Check `free -h` "available" before launching, in Phase 3/5's job runner.
- **Anonymous Instagram enumeration is dead** — verified across yt-dlp, gallery-dl,
  instaloader and 4 raw endpoints. Single-post access still works anonymously.
- **The Claude Telegram bridge cannot host this pipeline** — single-session bind, currently
  pointed at `AbuAliArchive`.
- **Instagram shortcode = base64 of the numeric post ID** (alphabet `A-Za-z0-9-_`).
  Validated: `3970953063832813263` → `DcbqMXFgP7P`.
- **gallery-dl rewrites the cookie jar after every run** — it is mutable state, not a
  static credential.
- `gallery-dl --print-to-file` silently emits nothing under `--simulate`.

### Facts established 2026-08-30 (Phase 3 close-out session)

- **`worker.py` exiting after a job is not a crash — it is `--once`.** The run in
  `/tmp/worker_run2.log` is 4 lines: fetch, extract, discover, enroll, then exit.
  `--once` (`worker.py:152`) drains what is queued and stops. There is no daemon
  running today and none is expected until Phase 5.
- **The 49 `chase.h.ai` backfill jobs are queued and intentionally undrained.**
  Enrolled automatically when `DbsDXkgJ_FB` resolved to a new account. Draining
  them is ~28 min of local ASR each — do not kick this off casually, and read
  Phase 5's worker-durability block first (open items #7, #8).
- **This project has its own Telegram send path, independent of the Claude
  bridge.** `scripts/telegram_notify.py` — send-only, reads `TELEGRAM_BOT_TOKEN`
  and `TELEGRAM_ALLOWED_CHAT_ID` from `secrets.env`, one fresh `Bot` per call. It
  works even when the `plugin:telegram:telegram` MCP server is down, which it was
  this session (`CONNECTION_CLOSED`). Verified live: a 1643-char report delivered
  to Dan's phone via `from telegram_notify import send`. **These are two different
  channels** — the MCP bridge failing says nothing about this one. Confirms the
  §0 Correction-3 finding from the other direction: the bridge was never the
  pipeline's notification path, `telegram_notify.py` is.
- **The ingest bot (`scripts/telegram_bot.py`) stays up across sessions** — pid
  1954425, running since 03:33. It owns the long-poll loop; `telegram_notify.py`
  deliberately does not poll, so both can coexist on the same token.
- **`jobs/failed/` not existing is not a bug.** `move_job` does
  `mkdir(parents=True, exist_ok=True)` (`jobs_lib.py:48`), so the directory is
  created on first failure. Checked because its absence looks alarming next to
  `queued/`, `running/` and `done/`.
- **`DbsDXkgJ_FB` end-to-end result, as a reference for what good output looks
  like:** `@chase.h.ai`, audio-only m4a, 2:33, 2818 chars. Local `qwen3-asr-1.7b`
  reconciled against `groq-whisper-large-v3` at **93.5% agreement** (556 vs 554
  words, 28 disagreement spans); ~28 min wall (80 s load, 470 s peek, 1122 s full).
  The reconciler earned its place here: Groq systematically heard *"cash"* for
  *"cache"* and *"cloud code"* for *"Claude Code"*, and the local model won every
  one of those spans. Two mis-hearings still survived into the merged text —
  *"Claude Det MD"* (CLAUDE.md) and *"a smaller model like Sonar or Opus"*
  (Sonnet) — both domain proper nouns, which is exactly where Phase 4's tool
  resolver (§4) will have to be tolerant of ASR noise rather than string-matching.

### Facts established 2026-08-30 (Phase 2 OCR-vendor session)

- **Gemini's free tier meters quota per model family, not per key.** This is
  the root cause of the Phase 2 block, and it was misdiagnosed as "the key is
  out of quota." Verified with one key inside a single minute:
  `gemini-3.7-flash` returned 429 while `3.6-flash`, `3.5-flash`,
  `3.5-flash-lite`, `3.1-flash-lite`, `3-flash-preview`, `2.5-flash` and
  `2.5-flash-lite` all returned 200 on the same image. The carousel was never
  out of *Gemini* quota — only out of one model's. `ocr_gemini.py` now rotates
  families on 429/5xx instead of sleeping out the window.
- **Not all Gemini vision tiers are safe for OCR, and the failure is silent.**
  Measured against the known-good slide-1 headline: `3.5-flash` and
  `3.6-flash` were exact; `3.1-flash-lite` and `2.5-flash-lite` dropped words
  (`"מה קרה בתי הספר"` for `"בבתי"`); `2.5-flash` invented an entire caption
  for blurry background signage that isn't in the image; `gemma-4-31b-it`
  leaked raw chain-of-thought instead of a transcript. `3.5-flash-lite` is
  *unstable* — exact via `:generateContent`, dropped a letter via
  `/interactions` on the same image — so it is last in the rotation, and
  everything ≤3.1 is excluded. A confidently-wrong transcript is worse than a
  429 (§4).
- **OCR.space Engine 3 reads Hebrew better than the VLMs do, for this content.**
  Byte-exact on the slide-1 headline, ~2s/slide, and it transcribes illegible
  regions literally (fragments of real protest-sign text) rather than
  smoothing them into a plausible sentence. It also preserved spellings
  `3.5-flash-lite` silently normalized away (`אווירה`→`אוירה`,
  `הנתונים האלה`→`הנתונים אלה`). Now the escalation primary.
- **Engine 3 is the only OCR.space engine that does Hebrew.** Engine 2 rejects
  it outright: `E201: Value for parameter 'language' is invalid`. Engine 3 is
  also the scarcer free quota (2,500/month vs 25,000 overall), so it runs only
  on escalation, never as a first pass.
- **Engine 3's language auto-detect is byte-identical to an explicit
  `language=heb`** on all 5 slides, so the wrapper defaults to auto-detect —
  the escalation gate fires for any low-confidence image, not only Hebrew.
- **Engine 3 wraps *some* responses in `--- OCR Start ---` / `--- OCR End ---`
  markers** — present on slides 04/05 of `DcgAqIADbs2`, absent on 01–03. Strip
  unconditionally; don't branch on it.
- **No local Hebrew OCR option exists for this machine — re-checked, still
  negative.** EasyOCR does not support Hebrew at all (86 languages; RTL is
  `ar/fa/ug/ur` only — verified by installing it, not by reading docs).
  Surya v2 is now VLM-based and wants vllm/llama.cpp. No local VLM fits
  anyway: Qwen3-ASR fp16 already peaks at 6.9 GB of ~9 GB available on 2
  cores. Tesseract `heb` (92–96% on clean modern print) is the only real
  offline floor and is not installed.
- **The 1 MB free-tier upload cap is not currently binding but is guarded
  anyway** — the largest image on disk is 422 KB and zero exceed 1 MB, but
  `ocr_ocrspace.py` downscales to 1080px wide (Instagram's own slide width)
  via the already-installed static ffmpeg rather than adding Pillow, which
  system Python doesn't have. Verified against a synthetic 1,240 KB image:
  downscaled and still byte-exact.

### Facts established 2026-08-25 (Phase 1 session)

- **yt-dlp's actual image-only-post error is `"No video formats found!"`**, not the
  original guess `"There is no video in this post"`. Verified against yt-dlp
  2026.08.19 on `https://www.instagram.com/p/B06xCNSAPsc/`.
- **The `gallery-dl` symlink was missing** from `~/.local/bin` despite the venv
  existing at `~/.local/venvs/gallery-dl` — PLAN.md's earlier claim that it was
  "symlinked into `~/.local/bin`" was wrong. Fixed this session
  (`ln -sf ~/.local/venvs/gallery-dl/bin/gallery-dl ~/.local/bin/gallery-dl`).
- **`yt-dlp -x/--extract-audio` + `--keep-video` leaves raw pre-merge DASH
  fragments on disk** (`<id>.fdash-<fmt>v.mp4` + `<id>.fdash-<fmt>a.m4a`, ~20 MB
  extra), not just "the original" — `--keep-video`'s actual scope is broader than
  its name suggests. Fixed in `scripts/fetch.py` by dropping `-x`/`--keep-video`
  entirely and deriving the WAV with a separate `ffmpeg -ac 1 -ar 16000 -c:a
  pcm_s16le` pass over the clean merged mp4 (matches §2's "Audio prep" row —
  that was always meant to be ffmpeg's job, not yt-dlp's). mp3 is no longer
  produced; it was never actually required by the §6 layout.
- **IG session cookies went dead mid-session** (see open item 6) — `sessionid`
  is absent from the Netscape jar. Root cause unclear (pre-existing vs. flagged
  by a burst of ~46 rapid requests while probing for a carousel candidate) —
  either way, confirms §8 risk 2 is real, and confirms the fetch script's
  fail-loud behavior on auth-failure markers (`"redirect to login page"` etc.)
  is necessary, not paranoid.
- **`gallery-dl -g`/`-K` require full auth even for public, already-accessible
  posts** — unlike yt-dlp, which has a working anonymous single-post path (per
  the existing "Single-post access still works anonymously" fact). Confirmed by
  testing gallery-dl with zero cookies against the known-good `DW1O6ZBEfDa` post:
  identical `"HTTP redirect to login page"` failure as with the dead cookie jar.
- **`gallery-dl` supports `-D/--directory` (exact destination, no per-extractor
  subfolders) and `-f/--filename` (format string)** as direct CLI flags — cleaner
  than the `-o extractor.instagram.*=...` config-key route originally guessed.
- **Confirmed (not just theoretical): §8 risk 2 fired for real.** The rapid
  46-URL scan triggered Instagram's automated-behavior warning on the test
  account itself, not just a dead session token. **Design gap:** the fetch
  script fails loudly on already-dead auth, but nothing throttles requests to
  avoid *causing* that in the first place — the carousel hunt bypassed
  `fetch.py` and hammered `gallery-dl -g` directly in a tight loop. Any future
  enumeration/scanning script must add real delay + jitter between requests
  (seconds, not milliseconds) and a hard cap on requests per minute — treat
  Instagram's abuse detection as adversarial, not just "handle the error
  after it happens."
- **Carousel found and verified: `p/DLKrzPTvTh2/` (6 slides).** Sequential
  scanning from the start of `out/urls_non_video.txt` (2019-era posts, ~26
  probed, throttled) turned up zero carousels — this account's older content
  is overwhelmingly single-image. The list is chronological; jumping to
  near the end (2026-era, index 900) found one on the *first* probe. Lesson:
  when sampling for a post type, sample recent content first, don't scan
  chronologically from the oldest end.
- **The cookie re-export mix-up:** the actual fix for open item 6 was mundane
  — Dan's browser-extension export landed in the project directory
  (`www.instagram.com_cookies.txt`) instead of `~/.config/instagram/`, so the
  stale yt-dlp-managed jar (still missing `sessionid`) kept getting reused
  and re-mutated on every run, masking that a good export already existed.
  Moved into place, `chmod 600`. Worth checking file *content* (does
  `sessionid` appear at all) before assuming a re-export didn't take.
- **`scripts/scan_carousel.py` built**: real delay + jitter (8-18s) between
  probes, a hard `--max-requests` cap, and stops immediately (not at batch
  end) on any auth-failure marker. Ran ~26 throttled probes total across two
  sessions with zero further auth issues — the fix for the automation
  warning (open item 6) holds. Any future multi-URL Instagram scanning should
  use this pattern, not a tight loop.
- **gallery-dl writes zero comment data for Instagram — confirmed structural,
  not a missing flag.** `--write-metadata` sidecar JSONs have no `comments`
  field. Checked the extractor source directly
  (`gallery_dl/extractor/instagram.py`): the GraphQL query it sends *asks*
  for `fetch_comment_count: 40`, but `_parse_post_graphql`/`_parse_post_rest`
  never read the comment edges out of the response into gallery-dl's own
  metadata dict — there's no config option or flag that surfaces it, because
  the feature was simply never implemented for any Instagram post type.
  Reusing gallery-dl's internal API classes to hand-parse the raw response
  ourselves is technically possible but relies on undocumented internal
  fields/query hashes that could break silently on any gallery-dl update, and
  adds another distinct request pattern against Instagram right after an
  automation flag on this account — not worth it for what Phase 1 needs.
- **yt-dlp can't get real comments for a photo/carousel post either, and this
  is structural too — tried both `--ignore-no-formats-error` and
  `--skip-download` in combination.** yt-dlp treats a carousel as a
  6-item "playlist", one per slide, each with its own internal shortcode
  (e.g. `DLKry-IPX-B`) distinct from the real post shortcode
  (`DLKrzPTvTh2`) — this is what "fetching from a specific picture" turns
  out to mean concretely. With `--ignore-no-formats-error`, each per-item
  `info.json`'s `comments` field looked promising (same "66" on every item)
  until inspected closely: it's the **stringified repr of an unconsumed
  Python generator** (`<generator object InstagramBaseIE._get_comments at
  0x...>`, which happens to be 66 characters — not 66 real comments). The
  comment generator only gets consumed by a postprocessor that runs after a
  *successful* format download; image posts have zero downloadable formats,
  so that postprocessor never runs, with or without `--skip-download`. This
  is a structural limitation of yt-dlp's Instagram extractor, not a flag we
  haven't found yet.
- **Net: carousels get 0 of N real comments via any currently-viable path**,
  worse than the video track's partial 14/256. Sharpens §8 risk 3 — for
  image/carousel posts, the resolver (§4) cannot lean on "creator's own
  replies in comments" at all and must rely more heavily on caption/OCR/web
  search. Revisit only if this turns out to matter in practice during Phase 4
  — the fix would be a small standalone script reusing gallery-dl's
  `InstagramGraphqlAPI`/`InstagramRestAPI` classes to hand-extract comment
  edges from the raw response, at the cost noted above.
