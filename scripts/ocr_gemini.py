#!/usr/bin/env python3
"""Phase 2 Hebrew/escalation OCR *secondary*: Gemini Flash Vision, rotating
across model families.

Runs after ocr_ocrspace.py (the primary since 2026-08-30) -- both as a
fallback when OCR.space is unavailable, and as the independent second read
that reconcile.py diffs for sec 0b.3-style dual-engine agreement.

**Why a rotation and not one model:** the free tier meters quota
*per model family*, not per key. Verified 2026-08-30 with one key in a single
minute: gemini-3.7-flash returned 429 while gemini-3.6-flash, 3.5-flash,
3.5-flash-lite, 3.1-flash-lite, 3-flash-preview, 2.5-flash and 2.5-flash-lite
all returned 200 on the same image. The Phase 2 carousel was never actually
out of Gemini quota -- only out of one model's. So a 429 rotates to the next
family immediately instead of sleeping out the window. A 5xx rotates too:
"currently experiencing high demand" is per-model backend load, and the
original code slept through it for no reason.

**Why this order.** Measured on the DcgAqIADbs2 Hebrew carousel against the
known-good slide-1 headline:
  - gemini-3.5-flash       exact on every run, both API surfaces  -> first
  - gemini-3.6-flash       exact, but ~20s vs ~9s
  - gemini-3.7-flash       the original Phase 2 choice
  - gemini-3.5-flash-lite  fastest (~1.5s) but *unstable* -- dropped a letter
                           ("מה קרה בתי" for "בבתי") on one surface while
                           reading it correctly on the other -> last resort
Everything at 3.1 and below is deliberately excluded: 3.1-flash-lite and
2.5-flash-lite dropped words, and 2.5-flash invented a caption for blurry
background signage. A confidently-wrong transcript is worse than a 429 here
(sec 4's confidence gate).

Usage:
    python3 scripts/ocr_gemini.py <image.jpg> [--secrets PATH] [--model M]

Writes JSON to stdout: {"image": ..., "engine": <model that answered>, "text": ...}.
"""
import argparse
import base64
import json
import mimetypes
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

# Ordered best-accuracy-first; see the module docstring for the measurements.
# Each entry is an independent free-tier quota pool.
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
]

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


RETRY_AFTER_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def parse_retry_after_seconds(error_text):
    """Pure: extracts the suggested wait from a 429 body like 'Please retry in
    49.88284644s.' Returns None if not present."""
    m = RETRY_AFTER_RE.search(error_text)
    return float(m.group(1)) if m else None


def plan_round_wait(suggested_waits, attempt):
    """Pure: how long to sleep after *every* model in one rotation failed.
    Honors the longest wait the API itself suggested; falls back to exponential
    backoff when none of them said. Only reached once the whole rotation is
    exhausted -- a single model's 429 rotates instead of sleeping."""
    stated = [w for w in suggested_waits if w is not None]
    if stated:
        return max(stated) + 1
    return 2 ** attempt


def build_payload(image_path, model):
    """Pure-ish: the /interactions request body for one model."""
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    image_bytes = Path(image_path).read_bytes()
    return {
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


def ocr(image_path, api_key, models=GEMINI_MODELS, max_attempts=3):
    """Rotate through the model families on 429/5xx/network failure; sleep only
    once a whole rotation is exhausted. Any other 4xx (bad key, bad request)
    fails immediately -- that's a real error, not a quota wall, and retrying it
    on four models would just produce the same failure four times."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    last_error = None

    for attempt in range(1, max_attempts + 1):
        suggested_waits = []
        for model in models:
            try:
                response = requests.post(
                    GEMINI_URL, headers=headers, json=build_payload(image_path, model), timeout=120
                )
            except requests.exceptions.RequestException as e:
                last_error = f"{model}: {e}"
                print(f"[ocr_gemini] {model} network error: {e}", file=sys.stderr)
                continue

            if response.status_code == 200:
                text = extract_text(response.json())
                return {"image": str(image_path), "engine": model, "text": text}

            last_error = f"{model} -> {response.status_code}: {response.text[:300]}"
            if response.status_code == 429:
                suggested_waits.append(parse_retry_after_seconds(response.text))
                print(f"[ocr_gemini] {model} quota exhausted -- rotating", file=sys.stderr)
                continue
            if response.status_code >= 500:
                print(f"[ocr_gemini] {model} backend error {response.status_code} "
                      "-- rotating", file=sys.stderr)
                continue
            raise SystemExit(f"[ocr_gemini] FATAL: Gemini API returned {last_error}")

        if attempt < max_attempts:
            wait_seconds = plan_round_wait(suggested_waits, attempt)
            print(f"[ocr_gemini] all {len(models)} models unavailable; "
                  f"waiting {wait_seconds:.0f}s before retry", file=sys.stderr)
            time.sleep(wait_seconds)

    raise SystemExit(
        f"[ocr_gemini] FATAL: all {len(models)} Gemini models failed after "
        f"{max_attempts} rotations: {last_error}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    ap.add_argument("--model", default=None,
                    help="pin one model instead of rotating the whole list")
    args = ap.parse_args()

    if not Path(args.image).exists():
        raise SystemExit(f"[ocr_gemini] FATAL: image not found: {args.image}")

    api_key = load_secret("GEMINI_API_KEY", Path(args.secrets))
    if not api_key:
        raise SystemExit(f"[ocr_gemini] FATAL: GEMINI_API_KEY not set in {args.secrets}")

    models = [args.model] if args.model else GEMINI_MODELS
    result = ocr(args.image, api_key, models=models)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
