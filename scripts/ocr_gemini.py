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
