#!/usr/bin/env python3
"""Phase 2 Hebrew/escalation OCR *primary*: OCR.space Engine 3.

Replaces Gemini as the first escalation target for images PP-OCRv6 can't read
(PLAN.md sec 0 Correction 1). Chosen after a head-to-head on the DcgAqIADbs2
Hebrew carousel: Engine 3 reproduced the known-good slide-1 headline
byte-for-byte, read all 5 slides cleanly in ~2s each, and -- unlike several
Gemini vision tiers -- did not invent text for the blurry background signage
(gemini-2.5-flash hallucinated a whole sign caption there). Not hallucinating
matters directly for sec 4's "never silently promote a guess to fact" rule.

Two constraints are load-bearing, both verified against the live API:
- **Engine 3 is mandatory.** Engine 2 rejects Hebrew outright with
  "E201: Value for parameter 'language' is invalid". Only Engine 3 carries the
  200+ language models. Engine 3 is also the scarcer quota (2,500/month on the
  free tier, vs 25,000 overall) -- so this runs only on escalation, never as
  the primary pass.
- **Language auto-detect is the default.** Byte-identical to an explicit
  `language=heb` on all 5 slides, and the escalation gate fires for *any*
  low-confidence image, not only Hebrew ones. `--language` forces it.

Usage:
    python3 scripts/ocr_ocrspace.py <image.jpg> [--secrets PATH] [--language heb]

Writes JSON to stdout: {"image": ..., "engine": "ocrspace-engine3", "text": ...}.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret

OCRSPACE_URL = "https://api.ocr.space/parse/image"
OCRSPACE_ENGINE = 3
ENGINE_NAME = "ocrspace-engine3"

# Free-tier hard cap on upload size. Every Instagram slide on disk is well under
# this (largest observed: 422 KB of 1 MB), but a higher-resolution post would
# silently fail without a guard, so downscale rather than trust the pattern.
MAX_UPLOAD_BYTES = 1024 * 1024
DOWNSCALE_WIDTH = 1080  # Instagram's own native slide width

# Engine 3 wraps some -- not all -- responses in these markers. Observed on
# slides 04 and 05 of DcgAqIADbs2 but not 01-03, so strip unconditionally
# rather than branching on it.
ENGINE_MARKER_RE = re.compile(r"^\s*---\s*OCR (?:Start|End)\s*---\s*$", re.MULTILINE)

# Free keys are throttled per IP; the API states the wait in the error body.
RATE_LIMIT_RE = re.compile(r"within (\d+) seconds", re.IGNORECASE)


def strip_engine_markers(text):
    """Pure: removes Engine 3's '--- OCR Start/End ---' wrapper lines and the
    blank lines they leave behind, without touching interior blank lines that
    are real paragraph breaks in the transcribed text."""
    cleaned = ENGINE_MARKER_RE.sub("", text)
    return cleaned.strip("\n").strip()


def parse_rate_limit_seconds(error_text):
    """Pure: extracts the wait from a throttle body like 'You may only perform
    this action upto maximum 10 number of times within 600 seconds'."""
    m = RATE_LIMIT_RE.search(error_text)
    return float(m.group(1)) if m else None


def normalize_error(error_message):
    """Pure: OCR.space reports ErrorMessage as either a string or a list of
    strings depending on the failure. Collapse to one string."""
    if isinstance(error_message, list):
        return " ".join(str(e) for e in error_message)
    return str(error_message or "")


def parse_ocrspace_response(payload):
    """Pure: (text, error) from a parsed OCR.space JSON body. error is None on
    success. OCRExitCode 1 = success, 2 = partial success (still usable text),
    3/4 = failed/fatal."""
    if payload.get("IsErroredOnProcessing"):
        return None, normalize_error(payload.get("ErrorMessage")) or "unknown processing error"

    exit_code = payload.get("OCRExitCode")
    if exit_code not in (1, 2):
        return None, normalize_error(payload.get("ErrorMessage")) or f"OCRExitCode={exit_code}"

    results = payload.get("ParsedResults") or []
    if not results:
        return None, "no ParsedResults in response"

    text = "\n".join(strip_engine_markers(r.get("ParsedText") or "") for r in results)
    return text.strip(), None


def needs_downscale(size_bytes, cap=MAX_UPLOAD_BYTES):
    """Pure: True if the file is too big for the free tier's upload cap."""
    return size_bytes > cap


def downscale(image_path, dest_path, width=DOWNSCALE_WIDTH):
    """Shell out to the static ffmpeg already installed for the Phase 1 audio
    pass (PLAN.md sec 9) rather than adding Pillow -- system Python has no PIL,
    and this needs no new dependency."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(image_path),
        "-vf", f"scale='min({width},iw)':-2",
        "-q:v", "3",
        str(dest_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"[ocr_ocrspace] FATAL: ffmpeg downscale failed:\n{result.stderr[-1000:]}")
    return dest_path


def ocr(image_path, api_key, language=None, max_attempts=4):
    """Retries transient failures (network errors, 5xx, per-IP throttling --
    the throttle body states its own wait, so honor that instead of guessing).
    An API-reported parse error (bad key, unsupported language) fails
    immediately: retrying those just burns quota. Mirrors ocr_gemini.py's
    backoff shape so both escalation engines behave the same way."""
    image_path = Path(image_path)

    with tempfile.TemporaryDirectory() as tmp:
        upload_path = image_path
        if needs_downscale(image_path.stat().st_size):
            print(f"[ocr_ocrspace] {image_path.name} is "
                  f"{image_path.stat().st_size / 1024:.0f}KB (> 1MB free-tier cap) "
                  f"-- downscaling to {DOWNSCALE_WIDTH}px wide", file=sys.stderr)
            upload_path = downscale(image_path, Path(tmp) / f"{image_path.stem}.small.jpg")

        data = {"apikey": api_key, "OCREngine": OCRSPACE_ENGINE}
        if language:
            data["language"] = language

        last_error = None
        for attempt in range(1, max_attempts + 1):
            wait_seconds = 2 ** attempt  # default backoff: 2s, 4s, 8s
            try:
                with open(upload_path, "rb") as fh:
                    response = requests.post(
                        OCRSPACE_URL, data=data, files={"file": fh}, timeout=120
                    )
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                print(f"[ocr_ocrspace] attempt {attempt}/{max_attempts} network error: {e}",
                      file=sys.stderr)
            else:
                if response.status_code == 200:
                    text, error = parse_ocrspace_response(response.json())
                    if error is None:
                        return {"image": str(image_path), "engine": ENGINE_NAME, "text": text}
                    last_error = error
                    raise SystemExit(f"[ocr_ocrspace] FATAL: OCR.space returned: {last_error}")

                last_error = f"{response.status_code}: {response.text[:500]}"
                if response.status_code in (403, 429):
                    retry_after = parse_rate_limit_seconds(response.text)
                    wait_seconds = retry_after + 1 if retry_after is not None else 60
                elif response.status_code < 500:
                    raise SystemExit(f"[ocr_ocrspace] FATAL: OCR.space returned {last_error}")
                print(f"[ocr_ocrspace] attempt {attempt}/{max_attempts} got {last_error}",
                      file=sys.stderr)

            if attempt < max_attempts:
                print(f"[ocr_ocrspace] waiting {wait_seconds:.0f}s before retry", file=sys.stderr)
                time.sleep(wait_seconds)

    raise SystemExit(
        f"[ocr_ocrspace] FATAL: OCR.space failed after {max_attempts} attempts: {last_error}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    ap.add_argument("--language", default=None,
                    help="force a language code (e.g. heb); default is Engine 3 auto-detect")
    args = ap.parse_args()

    if not Path(args.image).exists():
        raise SystemExit(f"[ocr_ocrspace] FATAL: image not found: {args.image}")

    api_key = load_secret("OCRSPACE_API_KEY", Path(args.secrets))
    if not api_key:
        raise SystemExit(
            f"[ocr_ocrspace] FATAL: OCRSPACE_API_KEY not set in {args.secrets}. "
            "Register a free key (email only, no card) at https://ocr.space/ocrapi/freekey"
        )

    result = ocr(args.image, api_key, language=args.language)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
