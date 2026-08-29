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
import re
import sys
import time
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


RETRY_AFTER_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def parse_retry_after_seconds(error_text):
    """Pure: extracts the suggested wait from a 429 body like 'Please retry in
    49.88284644s.' Returns None if not present."""
    m = RETRY_AFTER_RE.search(error_text)
    return float(m.group(1)) if m else None


def ocr(image_path, api_key, model=GEMINI_MODEL, max_attempts=4):
    """Retries transient failures -- observed repeatedly in practice: 5xx
    ('gemini-3.7-flash is currently experiencing high demand'), read timeouts,
    and 429 (free-tier quota: 20 req/window) all recovered on retry. A 429
    honors the API's own suggested wait time instead of guessing; other
    retryable failures use short exponential backoff. Any other 4xx (bad key,
    bad request) fails immediately -- retrying those would just waste time."""
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
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    last_error = None
    for attempt in range(1, max_attempts + 1):
        wait_seconds = 2 ** attempt  # default backoff: 2s, 4s, 8s
        try:
            response = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=120)
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            print(f"[ocr_gemini] attempt {attempt}/{max_attempts} network error: {e}", file=sys.stderr)
        else:
            if response.status_code == 200:
                text = extract_text(response.json())
                return {"image": str(image_path), "engine": "gemini-3.7-flash", "text": text}
            last_error = f"{response.status_code}: {response.text[:500]}"
            if response.status_code == 429:
                retry_after = parse_retry_after_seconds(response.text)
                wait_seconds = retry_after + 1 if retry_after is not None else 60
            elif response.status_code < 500:
                raise SystemExit(f"[ocr_gemini] FATAL: Gemini API returned {last_error}")
            print(f"[ocr_gemini] attempt {attempt}/{max_attempts} got {last_error}", file=sys.stderr)
        if attempt < max_attempts:
            print(f"[ocr_gemini] waiting {wait_seconds:.0f}s before retry", file=sys.stderr)
            time.sleep(wait_seconds)

    raise SystemExit(f"[ocr_gemini] FATAL: Gemini API failed after {max_attempts} attempts: {last_error}")


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
