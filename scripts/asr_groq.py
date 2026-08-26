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
