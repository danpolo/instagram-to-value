# Phase 2 — Extract Layer Implementation Plan

> **Status (2026-08-30): Tasks 1-6 executed inline this session; checkboxes below were
> never ticked mechanically. For the real final state — including two real design
> deviations from what's written here (Caspi needed its own venv, not the single
> `asr_local.py` in Task 4; the OCR gate in Task 6 needed a second `looks_garbled()`
> check, not just the confidence threshold) — see `PLAN.md` §7's "Phase 2 — Extract
> layer" section, which is the source of truth. This file is history, not a live
> checklist. 2 of 3 done-criteria samples verified (English reel, Hebrew reel); the
> Hebrew carousel is code-complete/unit-tested but blocked on a Gemini API quota.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the extract layer — local + Groq ASR (English/Hebrew, swappable checkpoint), local + Gemini OCR (Latin/CJK/Hebrew) — so an Instagram post's media on disk becomes `extracted/<shortcode>.json` text.

**Architecture:** Four small standalone CLI scripts (one per engine: `asr_local.py`, `asr_groq.py`, `ocr_local.py`, `ocr_gemini.py`), each runnable independently and printing a JSON result to stdout, mirroring `fetch.py`'s conventions. `extract.py` orchestrates by shelling out to whichever venv's Python each engine needs (torch/transformers live in the `qwen-asr` venv, paddleocr in its own new venv, the two REST wrappers need only `requests` so run under system Python) — this sidesteps dependency conflicts between torch and paddle in one interpreter and matches the existing "each heavy dependency gets its own venv" pattern (`yt-dlp`, `gallery-dl`, `qwen-asr`).

**Tech Stack:** Python 3 stdlib + `requests` (system Python, already installed) for the two REST engines; `transformers`/`torch` (existing `qwen-asr` venv) for local ASR; `paddleocr` + `onnxruntime` (new venv) for local OCR; `pytest` (system Python, already installed) for pure-logic unit tests.

**Spec:** `/home/dan/projects/instagram-to-value/PLAN.md` §0 (Correction 1), §0b.3, §1 (Stage 3), §2, §3, §6, §7 (Phase 2 design note, locked 2026-08-26).

## Global Constraints

- No GPU; CPU-only inference (PLAN.md §0 Correction 2).
- fp16 primary for local ASR, ≥7.5GB available RAM required before loading a local ASR model, or refuse loudly — never risk the livelock from Phase 0 (PLAN.md §7 Phase 0 finding).
- One local ASR model resident at a time — load, run, unload (PLAN.md §0b).
- Every audio job runs local ASR **and** Groq, always, and gets reconciled — not Groq-on-failure-only (PLAN.md §7 Phase 2 design note, locked 2026-08-26).
- OCR script-detection is collapsed into one rule: always try PP-OCRv6 first; escalate to Gemini 3.7 Flash whenever confidence is low (mean rec_score < 0.70, or no text found at all) (PLAN.md §7 Phase 2 design note).
- Fail loud, never silently produce an empty/partial extraction (PLAN.md §8 risk 2, `fetch.py`'s existing convention).
- No test framework beyond `pytest` for pure logic — no mocking of the actual models/APIs; real end-to-end verification runs against the three fetched samples instead (matches the rest of the repo: `fetch.py`, `phase0_benchmark.py`, `reconcile.py` have no test suite, verification was always a real run against real data).
- Secrets (`GROQ_API_KEY`, `GEMINI_API_KEY`) live at `~/.config/instagram-to-value/secrets.env` (mode 600, outside the repo — already created and populated this session), never as bare env vars, never committed.

## Test samples (already fetched, on disk)

| Sample | Shortcode | Path |
|---|---|---|
| English reel (audio) | `DW1O6ZBEfDa` | `media/DW1O6ZBEfDa/DW1O6ZBEfDa.16k.wav` |
| Hebrew reel (audio) | `DcblDAVNrgn` | `media/DcblDAVNrgn/DcblDAVNrgn.16k.wav` |
| Hebrew carousel (image) | `DcgAqIADbs2` | `media/DcgAqIADbs2/DcgAqIADbs2_01.jpg` … `_05.jpg` |

---

### Task 1: Git init + PP-OCRv6 environment

**Files:**
- Create: `~/.local/venvs/paddleocr/` (venv, outside repo)
- Modify: none in-repo besides the commit itself

**Interfaces:**
- Produces: `~/.local/venvs/paddleocr/bin/python` — usable for `from paddleocr import PaddleOCR`

- [ ] **Step 1: Initialize git and commit the current state**

This repo has no `.git` yet (PLAN.md §9). `.gitignore` already exists precisely for this moment.

```bash
cd /home/dan/projects/instagram-to-value
git init
git add -A
git commit -m "chore: initial commit — Phase 0/1 done, Phase 2 design locked

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

Expected: a single commit containing PLAN.md, scripts/, discover.sh, media/ metadata (jpgs/wavs are gitignored — verify none got added: `git show --stat HEAD | grep -E '\.(wav|jpg|mp4)$'` should print nothing).

- [ ] **Step 2: Create the PaddleOCR venv**

```bash
python3 -m venv ~/.local/venvs/paddleocr
~/.local/venvs/paddleocr/bin/pip install --upgrade pip
~/.local/venvs/paddleocr/bin/pip install paddleocr onnxruntime
```

Expected: installs cleanly, no GPU-only wheel errors (CPU-only `onnxruntime`, not `onnxruntime-gpu`).

- [ ] **Step 3: Smoke-test against the Hebrew carousel slide**

```bash
~/.local/venvs/paddleocr/bin/python -c "
from paddleocr import PaddleOCR
ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                 use_textline_orientation=False, engine='onnxruntime')
result = ocr.predict('media/DcgAqIADbs2/DcgAqIADbs2_01.jpg')
for res in result:
    res.print()
"
```

Expected: it runs without error and prints a `rec_texts`/`rec_scores` structure. Since the slide's real text is Hebrew and PP-OCRv6 has no RTL model, expect either an empty `rec_texts` or garbage text with low `rec_scores` — this is the premise Task 5/6's escalation logic is built on, not a bug. Note what you actually see in the plan's execution notes (informs the confidence threshold sanity-check in Task 6).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: create paddleocr venv (not tracked — record install steps only)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

(Nothing under `~/.local/venvs/` is repo-tracked; this commit is a no-op if nothing in-repo changed — skip it if `git status` is clean.)

---

### Task 2: Download Caspi-1.7B

**Files:**
- Create (outside repo): `~/.local/venvs/qwen-asr/models/Caspi-1.7B/`

**Interfaces:**
- Produces: a local snapshot dir usable as `AutoModelForMultimodalLM.from_pretrained("~/.local/venvs/qwen-asr/models/Caspi-1.7B")`, exactly like the existing `Qwen3-ASR-1.7B-hf` dir.

- [ ] **Step 1: Download the checkpoint**

```bash
hf download OzLabs/Caspi-1.7B --local-dir ~/.local/venvs/qwen-asr/models/Caspi-1.7B
```

Expected: ~3-4GB of fp16 weights (same architecture as Qwen3-ASR-1.7B-hf, see PLAN.md §0). No GGUF/quantized files needed — fp16 is the locked precision (PLAN.md §0b).

- [ ] **Step 2: Verify it loads**

```bash
~/.local/venvs/qwen-asr/bin/python -c "
from transformers import AutoProcessor, AutoModelForMultimodalLM
p = AutoProcessor.from_pretrained('$HOME/.local/venvs/qwen-asr/models/Caspi-1.7B')
m = AutoModelForMultimodalLM.from_pretrained('$HOME/.local/venvs/qwen-asr/models/Caspi-1.7B', dtype='float16', device_map='cpu')
print('OK', m.dtype, m.device)
"
```

Expected: prints `OK torch.float16 cpu` with no errors.

- [ ] **Step 3: Commit**

Nothing in-repo changes (model weights live outside the repo, like Qwen3-ASR-1.7B-hf). Skip the commit — note completion in the task list only.

---

### Task 3: Shared secrets reader + the two REST engines (Groq, Gemini)

**Files:**
- Create: `scripts/secrets_lib.py`
- Create: `scripts/asr_groq.py`
- Create: `scripts/ocr_gemini.py`
- Test: `scripts/test_extract.py` (started here, extended in later tasks)

**Interfaces:**
- Produces: `secrets_lib.load_secret(name, secrets_path=DEFAULT_SECRETS) -> str | None`
- Produces: `asr_groq.transcribe(audio_path, api_key, model=GROQ_MODEL) -> dict` with keys `text`, `engine`, `audio`
- Produces: `ocr_gemini.extract_text(interaction: dict) -> str` (pure), `ocr_gemini.ocr(image_path, api_key, model=GEMINI_MODEL) -> dict` with keys `image`, `engine`, `text`

- [ ] **Step 1: Write `scripts/secrets_lib.py`**

```python
#!/usr/bin/env python3
"""Shared minimal .env reader for API keys. Keys live outside the repo at
~/.config/instagram-to-value/secrets.env (mode 600, same pattern as the IG
cookies at ~/.config/instagram/cookies.txt) -- not as SDK-managed env vars.
See PLAN.md sec 7's Phase 2 design note."""
from pathlib import Path

DEFAULT_SECRETS = Path.home() / ".config" / "instagram-to-value" / "secrets.env"


def load_secret(name, secrets_path=DEFAULT_SECRETS):
    secrets_path = Path(secrets_path)
    if not secrets_path.exists():
        return None
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None
```

- [ ] **Step 2: Write `scripts/asr_groq.py`**

```python
#!/usr/bin/env python3
"""Phase 2 Groq fallback/reconciliation ASR: calls Groq's Whisper Large V3 API.
Per PLAN.md sec 0b.3 / sec 7's Phase 2 design note, this now runs on EVERY audio
job (not just on local failure) so reconcile.py has something to diff against.
Groq is an *availability* fallback, not an accuracy upgrade -- Qwen3-ASR-1.7B
outperforms it (PLAN.md sec 0b.2).

Usage:
    python3 scripts/asr_groq.py <audio.wav> [--secrets PATH]

Writes JSON to stdout in the {"text": ...} shape reconcile.py already expects.
"""
import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"


def transcribe(audio_path, api_key, model=GROQ_MODEL):
    with open(audio_path, "rb") as f:
        response = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, "audio/wav")},
            data={"model": model, "response_format": "json"},
            timeout=300,
        )
    if response.status_code != 200:
        raise SystemExit(
            f"[asr_groq] FATAL: Groq API returned {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    return {"text": data.get("text", ""), "engine": "groq-whisper-large-v3", "audio": str(audio_path)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()

    if not Path(args.audio).exists():
        raise SystemExit(f"[asr_groq] FATAL: audio file not found: {args.audio}")

    api_key = load_secret("GROQ_API_KEY", Path(args.secrets))
    if not api_key:
        raise SystemExit(f"[asr_groq] FATAL: GROQ_API_KEY not set in {args.secrets}")

    result = transcribe(args.audio, api_key)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `scripts/ocr_gemini.py`**

```python
#!/usr/bin/env python3
"""Phase 2 Hebrew/escalation OCR: Gemini 3.7 Flash Vision, for images PP-OCRv6
can't read (no RTL model at all) or reads with low confidence. See PLAN.md sec 0
Correction 1 and sec 7's Phase 2 design note (collapses "script detect" into one
confidence-gated escalation rule -- the gate itself lives in extract.py).

Usage:
    python3 scripts/ocr_gemini.py <image.jpg> [--secrets PATH]

Writes JSON to stdout: {"image": ..., "engine": "gemini-3.7-flash", "text": ...}.
"""
import argparse
import base64
import json
import mimetypes
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MODEL = "gemini-3.7-flash"
OCR_PROMPT = (
    "Transcribe every piece of text visible in this image exactly as written, "
    "top to bottom. This may include Hebrew (right-to-left) text. Output only "
    "the transcribed text, no commentary, no translation."
)


def extract_text(interaction: dict) -> str:
    """Pull the model's text out of an /interactions response -- walks steps for
    the model_output step's text content."""
    for step in interaction.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for item in step.get("content", []):
            if item.get("type") == "text":
                return item["text"]
    return ""


def ocr(image_path, api_key, model=GEMINI_MODEL):
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    image_bytes = Path(image_path).read_bytes()
    payload = {
        "model": model,
        "input": [
            {"type": "text", "text": OCR_PROMPT},
            {
                "type": "image",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
                "mime_type": mime_type,
            },
        ],
    }
    response = requests.post(
        GEMINI_URL,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"[ocr_gemini] FATAL: Gemini API returned {response.status_code}: {response.text[:500]}"
        )
    text = extract_text(response.json())
    return {"image": str(image_path), "engine": "gemini-3.7-flash", "text": text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()

    if not Path(args.image).exists():
        raise SystemExit(f"[ocr_gemini] FATAL: image not found: {args.image}")

    api_key = load_secret("GEMINI_API_KEY", Path(args.secrets))
    if not api_key:
        raise SystemExit(f"[ocr_gemini] FATAL: GEMINI_API_KEY not set in {args.secrets}")

    result = ocr(args.image, api_key)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write the first slice of `scripts/test_extract.py`**

```python
"""Pure-logic unit tests for the Phase 2 extract layer. Run with:
    python3 -m pytest scripts/test_extract.py -v
No local model / live API calls -- every heavy or networked call lives inside
functions these tests don't invoke."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secrets_lib
import ocr_gemini


def test_load_secret_reads_key(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("# comment\nGROQ_API_KEY=abc123\nGEMINI_API_KEY=xyz789\n")
    assert secrets_lib.load_secret("GROQ_API_KEY", secrets) == "abc123"
    assert secrets_lib.load_secret("GEMINI_API_KEY", secrets) == "xyz789"


def test_load_secret_missing_key_returns_none(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GROQ_API_KEY=\n")
    assert secrets_lib.load_secret("GROQ_API_KEY", secrets) is None


def test_load_secret_missing_file_returns_none(tmp_path):
    assert secrets_lib.load_secret("GROQ_API_KEY", tmp_path / "nope.env") is None


def test_extract_text_from_gemini_response():
    response = {
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "prompt"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "recognized text"}]},
        ]
    }
    assert ocr_gemini.extract_text(response) == "recognized text"


def test_extract_text_missing_model_output():
    assert ocr_gemini.extract_text({"steps": []}) == ""
```

- [ ] **Step 5: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_extract.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Run Groq against the Hebrew reel, verify real output**

```bash
python3 scripts/asr_groq.py media/DcblDAVNrgn/DcblDAVNrgn.16k.wav | tee /tmp/groq_DcblDAVNrgn.json
```

Expected: JSON with a non-empty `text` field containing Hebrew characters roughly matching the known caption content (חן טל, "הברזייה").

- [ ] **Step 7: Run Gemini OCR against the Hebrew carousel slide, verify real output**

```bash
python3 scripts/ocr_gemini.py media/DcgAqIADbs2/DcgAqIADbs2_01.jpg | tee /tmp/gemini_DcgAqIADbs2_01.json
```

Expected: JSON with a `text` field containing the Hebrew headline visible on the slide ("הנוער הגאה חוזר לארון..." or a close transcription of it).

- [ ] **Step 8: Commit**

```bash
git add scripts/secrets_lib.py scripts/asr_groq.py scripts/ocr_gemini.py scripts/test_extract.py
git commit -m "feat: add Groq ASR and Gemini OCR REST wrappers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Local ASR wrapper (`asr_local.py`)

**Files:**
- Create: `scripts/asr_local.py`
- Test: `scripts/test_extract.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone)
- Produces: `asr_local.should_reroute_to_caspi(detected_language: str) -> bool` (pure); CLI prints `{"audio", "engine", "language", "text", "peek_language", "seconds"}` to stdout

- [ ] **Step 1: Write `scripts/asr_local.py`**

```python
#!/usr/bin/env python3
"""Phase 2 local ASR wrapper: swappable-checkpoint transcription for Qwen3-ASR-1.7B
(default/multilingual) and Caspi-1.7B (Hebrew fine-tune of the same architecture --
PLAN.md sec 0). Implements PLAN.md sec 3's language routing exactly: transcribe the
first 60s with Qwen3; if it detects Hebrew, unload Qwen3 and re-run the full file
through Caspi; otherwise keep generating on the *same already-loaded* Qwen3 model
for the full file (no redundant reload). One inference wrapper, swappable checkpoint
path -- PLAN.md sec 2's "simplification worth taking".

Usage:
    ~/.local/venvs/qwen-asr/bin/python scripts/asr_local.py <audio.wav>
        [--dtype float16] [--max-new-tokens N]
        [--qwen-model Qwen/Qwen3-ASR-1.7B-hf] [--caspi-model PATH]
        [--no-caspi-reroute]

Writes JSON result to stdout. Fails loudly (SystemExit) on load/generate errors.
"""
import argparse
import gc
import json
import sys
import time
from pathlib import Path

DEFAULT_CASPI_MODEL = str(Path.home() / ".local" / "venvs" / "qwen-asr" / "models" / "Caspi-1.7B")


def should_reroute_to_caspi(detected_language: str) -> bool:
    """Pure routing decision from PLAN.md sec 3: only Hebrew reroutes."""
    return detected_language == "he"


def _clip_wav(audio_path, seconds):
    """Write a temp copy of audio_path clipped to the first `seconds` seconds,
    for the cheap 60s language-detection peek (PLAN.md sec 3)."""
    import soundfile as sf
    data, samplerate = sf.read(audio_path)
    clipped = data[: int(seconds * samplerate)]
    tmp_path = str(Path(audio_path).with_suffix(f".peek{seconds}s.wav"))
    sf.write(tmp_path, clipped, samplerate)
    return tmp_path


class LoadedModel:
    """Holds one loaded checkpoint. Caller controls its lifetime explicitly (del +
    gc.collect()) to honor PLAN.md sec 0b's 'one model resident at a time'."""

    def __init__(self, model_id, dtype):
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        self.model_id = model_id
        t0 = time.monotonic()
        print(f"[asr_local] loading {model_id} dtype={dtype} device=cpu ...", file=sys.stderr)
        torch_dtype = getattr(torch, dtype)
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(model_id, dtype=torch_dtype, device_map="cpu")
        self.model.eval()
        self.load_seconds = round(time.monotonic() - t0, 2)
        print(f"[asr_local] loaded in {self.load_seconds:.1f}s", file=sys.stderr)

    def generate(self, audio_path, max_new_tokens):
        import torch

        t0 = time.monotonic()
        inputs = self.processor.apply_transcription_request(audio=audio_path).to(self.model.device, self.model.dtype)
        with torch.inference_mode():
            output_ids = self.model.generate(**inputs, max_new_tokens=max_new_tokens)
        generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
        parsed = self.processor.decode(generated_ids, return_format="parsed")[0]
        return {
            "language": parsed.get("language"),
            "transcription": parsed.get("transcription"),
            "generate_seconds": round(time.monotonic() - t0, 2),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    ap.add_argument("--qwen-model", default="Qwen/Qwen3-ASR-1.7B-hf")
    ap.add_argument("--caspi-model", default=DEFAULT_CASPI_MODEL)
    ap.add_argument("--no-caspi-reroute", action="store_true",
                     help="Skip the Hebrew reroute even if Qwen3 detects 'he' (debugging only)")
    args = ap.parse_args()

    if not Path(args.audio).exists():
        raise SystemExit(f"[asr_local] FATAL: audio file not found: {args.audio}")

    qwen = LoadedModel(args.qwen_model, args.dtype)
    peek_wav = _clip_wav(args.audio, 60)
    try:
        peek = qwen.generate(peek_wav, args.max_new_tokens)
    finally:
        Path(peek_wav).unlink(missing_ok=True)
    print(f"[asr_local] 60s peek language={peek['language']!r}", file=sys.stderr)

    if not args.no_caspi_reroute and should_reroute_to_caspi(peek["language"]):
        print(f"[asr_local] Hebrew detected -> unload Qwen3, load {args.caspi_model}", file=sys.stderr)
        del qwen
        gc.collect()
        caspi = LoadedModel(args.caspi_model, args.dtype)
        full = caspi.generate(args.audio, args.max_new_tokens)
        engine, load_seconds = "caspi-1.7b", caspi.load_seconds
    else:
        print("[asr_local] non-Hebrew -> continuing full file on already-loaded Qwen3", file=sys.stderr)
        full = qwen.generate(args.audio, args.max_new_tokens)
        engine, load_seconds = "qwen3-asr-1.7b", qwen.load_seconds

    result = {
        "audio": args.audio,
        "engine": engine,
        "language": full["language"],
        "text": full["transcription"],
        "peek_language": peek["language"],
        "seconds": {
            "load": load_seconds,
            "peek_generate": peek["generate_seconds"],
            "full_generate": full["generate_seconds"],
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Extend `scripts/test_extract.py`**

Add to the top imports: `import asr_local`. Add:

```python
def test_should_reroute_to_caspi_on_hebrew():
    assert asr_local.should_reroute_to_caspi("he") is True


def test_should_not_reroute_on_english():
    assert asr_local.should_reroute_to_caspi("en") is False
    assert asr_local.should_reroute_to_caspi(None) is False
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_extract.py -v
```

Expected: all pass, including the 3 new ones (importing `asr_local` must not require torch — verify by running this under plain system `python3`, not the qwen-asr venv, which proves the heavy imports are correctly function-scoped).

- [ ] **Step 4: Run against the English reel — verify no Caspi reroute**

```bash
~/.local/venvs/qwen-asr/bin/python scripts/asr_local.py media/DW1O6ZBEfDa/DW1O6ZBEfDa.16k.wav 2>&1 | tee /tmp/asr_local_DW1O6ZBEfDa.log
```

Expected: stderr shows `peek language='en'` (or similar non-`he` code) and "non-Hebrew -> continuing ... on already-loaded Qwen3"; final JSON has `"engine": "qwen3-asr-1.7b"` and a `"text"` matching the known content (Mem Palace, AAK, Readwise, Obsidian, Morpheus, Multipass per PLAN.md §7 Phase 0).

- [ ] **Step 5: Run against the Hebrew reel — verify the Caspi reroute fires**

```bash
~/.local/venvs/qwen-asr/bin/python scripts/asr_local.py media/DcblDAVNrgn/DcblDAVNrgn.16k.wav 2>&1 | tee /tmp/asr_local_DcblDAVNrgn.log
```

Expected: stderr shows `peek language='he'` and "Hebrew detected -> unload Qwen3, load .../Caspi-1.7B"; final JSON has `"engine": "caspi-1.7b"` and Hebrew `"text"` roughly matching the known caption ("אין שום ספר חוקים...", חן טל, הברזייה).

- [ ] **Step 6: Commit**

```bash
git add scripts/asr_local.py scripts/test_extract.py
git commit -m "feat: add local ASR wrapper with Qwen3/Caspi language routing

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Local OCR wrapper (`ocr_local.py`)

**Files:**
- Create: `scripts/ocr_local.py`

**Interfaces:**
- Produces: CLI prints `{"image", "engine", "texts", "scores", "text", "mean_score"}` to stdout

- [ ] **Step 1: Write `scripts/ocr_local.py`**

```python
#!/usr/bin/env python3
"""Phase 2 local OCR: PP-OCRv6 via the onnxruntime inference engine (lighter than
the full PaddlePaddle framework install -- fine for a CPU-only machine at this
volume, PLAN.md sec 2). Handles Latin/CJK text directly; has no Hebrew/RTL model
at all, so Hebrew images come back empty or low-confidence -- extract.py's
needs_gemini_escalation() is what routes those to Gemini (PLAN.md sec 0
Correction 1, sec 7's Phase 2 design note).

Usage:
    ~/.local/venvs/paddleocr/bin/python scripts/ocr_local.py <image.jpg>

Writes JSON to stdout.
"""
import argparse
import json
import tempfile
from pathlib import Path


def run_ocr(image_path):
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine="onnxruntime",
    )
    results = ocr.predict(str(image_path))

    texts, scores = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i, res in enumerate(results):
            out_path = Path(tmp) / f"res_{i}.json"
            res.save_to_json(str(out_path))
            saved = json.loads(out_path.read_text())
            payload = saved.get("res", saved)
            texts.extend(payload.get("rec_texts") or [])
            scores.extend(payload.get("rec_scores") or [])
    return texts, scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    args = ap.parse_args()

    if not Path(args.image).exists():
        raise SystemExit(f"[ocr_local] FATAL: image not found: {args.image}")

    texts, scores = run_ocr(args.image)
    mean_score = round(sum(scores) / len(scores), 4) if scores else 0.0

    result = {
        "image": args.image,
        "engine": "pp-ocrv6",
        "texts": texts,
        "scores": scores,
        "text": "\n".join(texts),
        "mean_score": mean_score,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run against the Hebrew carousel slide — confirm low confidence**

```bash
~/.local/venvs/paddleocr/bin/python scripts/ocr_local.py media/DcgAqIADbs2/DcgAqIADbs2_01.jpg | tee /tmp/ocr_local_DcgAqIADbs2_01.json
```

Expected: `mean_score` well under 0.70 (or `texts` empty / `mean_score` 0.0), confirming the premise from Task 1 Step 3. Record the actual `mean_score` seen — if it's surprisingly high (PP-OCRv6 partially reads the Hebrew glyphs as Latin-like garbage with false confidence), note it and revisit the 0.70 threshold in Task 6 rather than assuming the plan's default is right.

- [ ] **Step 3: Commit**

```bash
git add scripts/ocr_local.py
git commit -m "feat: add local PP-OCRv6 wrapper

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Orchestrator (`extract.py`) + end-to-end verification

**Files:**
- Create: `scripts/extract.py`
- Test: `scripts/test_extract.py` (extend)
- Modify: `PLAN.md` (mark Phase 2 done)

**Interfaces:**
- Consumes: `asr_local.py`, `asr_groq.py`, `ocr_local.py`, `ocr_gemini.py`, `reconcile.py` as subprocesses (JSON-on-stdout contract established in Tasks 3-5)
- Produces: `extracted/<shortcode>.json` on disk; pure functions `check_ram_guard`, `needs_gemini_escalation`, `detect_media_type`, `build_extracted_json`

- [ ] **Step 1: Write `scripts/extract.py`**

```python
#!/usr/bin/env python3
"""Phase 2 orchestrator: media/<shortcode>/ -> extracted/<shortcode>.json.
Shells out to each engine's own venv Python (torch/transformers in qwen-asr,
paddleocr in its own venv, the two REST wrappers under system Python) --
sidesteps dependency conflicts between torch and paddle in one interpreter,
matches the existing "each heavy dependency gets its own venv" pattern.
See PLAN.md sec 7's Phase 2 design note for the full schema/routing rationale.

Usage:
    python3 scripts/extract.py <shortcode> [--media-root DIR] [--extracted-root DIR]

Writes extracted/<shortcode>.json and prints it to stdout. Fails loudly on any
subprocess error or insufficient RAM -- never produces a silent partial result.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QWEN_VENV_PYTHON = Path.home() / ".local" / "venvs" / "qwen-asr" / "bin" / "python"
PADDLEOCR_VENV_PYTHON = Path.home() / ".local" / "venvs" / "paddleocr" / "bin" / "python"
DEFAULT_MEDIA_ROOT = REPO_ROOT / "media"
DEFAULT_EXTRACTED_ROOT = REPO_ROOT / "extracted"

MIN_RAM_GB_FOR_ASR = 7.5
OCR_CONFIDENCE_THRESHOLD = 0.70


def get_available_ram_gb():
    """MemAvailable, not just 'free' -- the metric PLAN.md sec 7 Phase 0's
    livelock finding says actually matters."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            kb = int(line.split()[1])
            return kb / (1024 * 1024)
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def check_ram_guard(available_gb, minimum_gb=MIN_RAM_GB_FOR_ASR):
    """Pure: True if there's enough headroom to load a local ASR model safely.
    Swap is only ~975MB (PLAN.md sec 7 Phase 0) -- a close call wedges instead
    of degrading, so refuse rather than risk it."""
    return available_gb >= minimum_gb


def needs_gemini_escalation(texts, scores, threshold=OCR_CONFIDENCE_THRESHOLD):
    """Pure: collapses PLAN.md sec 0 Correction 1's two-branch script-detect
    diagram into one rule. Escalates when PP-OCRv6 found nothing, or found
    something but isn't confident -- which is exactly what happens on Hebrew
    glyphs, since PP-OCRv6 has no RTL model at all."""
    if not texts:
        return True
    mean_score = sum(scores) / len(scores) if scores else 0.0
    return mean_score < threshold


def detect_media_type(media_dir: Path, shortcode: str):
    """'audio' if the 16kHz wav exists, 'image' if numbered slide jpgs exist,
    else None. Matches fetch.py's media/<shortcode>/ layout (PLAN.md sec 6)."""
    if (media_dir / f"{shortcode}.16k.wav").exists():
        return "audio"
    if sorted(media_dir.glob(f"{shortcode}_*.jpg")):
        return "image"
    return None


def build_extracted_json(shortcode, media_type, **fields):
    """Assembles the extracted/<shortcode>.json schema (PLAN.md sec 7's Phase 2
    design note). Drops unset fields instead of null-padding."""
    result = {"shortcode": shortcode, "media_type": media_type}
    result.update({k: v for k, v in fields.items() if v is not None})
    return result


def run_json_subprocess(cmd):
    """Run a scripts/*.py CLI that prints a JSON result to stdout; return the
    parsed dict. Fails loudly with the subprocess's own stderr on nonzero exit
    -- matches fetch.py's fail-loud convention (PLAN.md sec 8 risk 2)."""
    print(f"[extract] $ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"[extract] FATAL: {cmd[1]} failed:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout)


def extract_audio(shortcode, media_dir):
    wav = media_dir / f"{shortcode}.16k.wav"
    available_gb = get_available_ram_gb()
    if not check_ram_guard(available_gb):
        raise SystemExit(
            f"[extract] FATAL: only {available_gb:.1f}GB available, need "
            f">={MIN_RAM_GB_FOR_ASR}GB to load a local ASR model safely "
            "(PLAN.md sec 7 Phase 0 livelock finding). Free memory and retry."
        )

    local = run_json_subprocess([str(QWEN_VENV_PYTHON), str(REPO_ROOT / "scripts" / "asr_local.py"), str(wav)])
    groq = run_json_subprocess([sys.executable, str(REPO_ROOT / "scripts" / "asr_groq.py"), str(wav)])

    local_path = media_dir / f"{shortcode}.local.json"
    groq_path = media_dir / f"{shortcode}.groq.json"
    local_path.write_text(json.dumps(local, ensure_ascii=False))
    groq_path.write_text(json.dumps(groq, ensure_ascii=False))
    reconciliation = run_json_subprocess(
        [sys.executable, str(REPO_ROOT / "scripts" / "reconcile.py"), str(local_path), str(groq_path)]
    )

    return build_extracted_json(
        shortcode, "audio",
        text=local["text"], language=local["language"],
        engine_primary=local["engine"], engine_fallback=groq["engine"],
        engine_seconds=local["seconds"], reconciliation=reconciliation,
    )


def extract_image(shortcode, media_dir):
    slides = sorted(media_dir.glob(f"{shortcode}_*.jpg"))
    if not slides:
        raise SystemExit(f"[extract] FATAL: no slides found for {shortcode} in {media_dir}")

    results = []
    for slide in slides:
        local = run_json_subprocess(
            [str(PADDLEOCR_VENV_PYTHON), str(REPO_ROOT / "scripts" / "ocr_local.py"), str(slide)]
        )
        if needs_gemini_escalation(local["texts"], local["scores"]):
            gemini = run_json_subprocess([sys.executable, str(REPO_ROOT / "scripts" / "ocr_gemini.py"), str(slide)])
            results.append({
                "slide": slide.name, "text": gemini["text"],
                "engine_primary": local["engine"], "engine_fallback": gemini["engine"],
                "confidence": local["mean_score"],
            })
        else:
            results.append({
                "slide": slide.name, "text": local["text"],
                "engine_primary": local["engine"], "engine_fallback": None,
                "confidence": local["mean_score"],
            })

    combined_text = "\n\n".join(r["text"] for r in results)
    return build_extracted_json(shortcode, "image", text=combined_text, slides=results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("shortcode")
    ap.add_argument("--media-root", default=str(DEFAULT_MEDIA_ROOT))
    ap.add_argument("--extracted-root", default=str(DEFAULT_EXTRACTED_ROOT))
    args = ap.parse_args()

    media_dir = Path(args.media_root) / args.shortcode
    if not media_dir.exists():
        raise SystemExit(f"[extract] FATAL: no media directory for {args.shortcode} at {media_dir}")

    media_type = detect_media_type(media_dir, args.shortcode)
    if media_type is None:
        raise SystemExit(f"[extract] FATAL: no recognizable media (wav or slides) in {media_dir}")

    result = extract_audio(args.shortcode, media_dir) if media_type == "audio" else extract_image(args.shortcode, media_dir)

    extracted_root = Path(args.extracted_root)
    extracted_root.mkdir(parents=True, exist_ok=True)
    out_path = extracted_root / f"{args.shortcode}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[extract] wrote {out_path}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Extend `scripts/test_extract.py`**

Add `import extract` to the imports. Add:

```python
def test_check_ram_guard_above_threshold():
    assert extract.check_ram_guard(8.0) is True


def test_check_ram_guard_below_threshold():
    assert extract.check_ram_guard(5.6) is False


def test_check_ram_guard_at_exact_threshold():
    assert extract.check_ram_guard(7.5, minimum_gb=7.5) is True


def test_needs_escalation_when_no_text_found():
    assert extract.needs_gemini_escalation([], []) is True


def test_needs_escalation_when_low_confidence():
    assert extract.needs_gemini_escalation(["x"], [0.3]) is True


def test_no_escalation_when_confident():
    assert extract.needs_gemini_escalation(["hello"], [0.95]) is False


def test_detect_media_type_audio(tmp_path):
    (tmp_path / "ABC123.16k.wav").write_bytes(b"")
    assert extract.detect_media_type(tmp_path, "ABC123") == "audio"


def test_detect_media_type_image(tmp_path):
    (tmp_path / "ABC123_01.jpg").write_bytes(b"")
    assert extract.detect_media_type(tmp_path, "ABC123") == "image"


def test_detect_media_type_none(tmp_path):
    assert extract.detect_media_type(tmp_path, "ABC123") is None


def test_build_extracted_json_drops_none_fields():
    result = extract.build_extracted_json("ABC123", "audio", text="hi", reconciliation=None)
    assert result == {"shortcode": "ABC123", "media_type": "audio", "text": "hi"}
```

- [ ] **Step 3: Run the tests, verify they pass**

```bash
python3 -m pytest scripts/test_extract.py -v
```

Expected: all pass (should be ~17 tests total across Tasks 3/4/6).

- [ ] **Step 4: Run end-to-end against all three samples**

```bash
python3 scripts/extract.py DW1O6ZBEfDa
python3 scripts/extract.py DcblDAVNrgn
python3 scripts/extract.py DcgAqIADbs2
```

Expected, per PLAN.md's Phase 2 done-criteria ("an English reel, a Hebrew reel, and a Hebrew carousel all produce text"):
- `extracted/DW1O6ZBEfDa.json`: `media_type: "audio"`, `language` ~= `"en"`, non-empty `text`, `reconciliation.agreement_ratio` present.
- `extracted/DcblDAVNrgn.json`: `media_type: "audio"`, `language: "he"`, `engine_primary: "caspi-1.7b"`, non-empty Hebrew `text`.
- `extracted/DcgAqIADbs2.json`: `media_type: "image"`, 5 entries in `slides`, at least the slide with the confirmed Hebrew headline shows `engine_fallback: "gemini-3.7-flash"` and Hebrew text.

If any sample fails this bar, treat it as a real bug to fix (not a plan deviation) before moving on — this is the phase's actual acceptance test.

- [ ] **Step 5: Update `PLAN.md`'s Phase 2 section to done**

Add a `— ✅ DONE 2026-08-26` marker to the `### Phase 2 — Extract layer` heading (matching Phase 0/1's style) and a short results bullet list under it: RAM available at run time, the three `extracted/*.json` files' key fields (media_type/language/engine used), and the actual PP-OCRv6 confidence score observed on the Hebrew slide (from Task 5 Step 2) versus the 0.70 threshold — confirm or correct it based on what was actually seen.

- [ ] **Step 6: Commit**

```bash
git add scripts/extract.py scripts/test_extract.py PLAN.md
git commit -m "feat: add extract.py orchestrator, close out Phase 2

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** §0 Correction 1 (script-detect collapse) → Task 6 `needs_gemini_escalation`. §0b.3 (dual-transcribe always) → Task 6 `extract_audio`. §2 (component choices, one wrapper w/ swappable checkpoint) → Task 4. §3 (language routing) → Task 4. §6 (schema: transcript/OCR + lang + engine + confidence) → Task 6 `build_extracted_json`. §7 Phase 2 done-criteria → Task 6 Step 4. §8 risk 2 (fail loud) → every script's `SystemExit` pattern. §7 Phase 0's RAM/livelock finding → Task 6 `check_ram_guard`.
- **No placeholders:** every step has real, complete code; no "TBD"/"similar to above".
- **Type/name consistency checked:** `asr_local.py` emits `text`/`language`/`engine`/`seconds` — `extract.py`'s `extract_audio` reads exactly those keys. `ocr_local.py` emits `texts`/`scores`/`text`/`mean_score`/`engine` — `extract.py`'s `extract_image` reads exactly those keys. `secrets_lib.load_secret` signature matches both callers. `reconcile.py`'s existing `load_text()` (`data.get("transcription") or data.get("text")`) is satisfied by both `asr_local.py` and `asr_groq.py` writing a `"text"` field.
