#!/usr/bin/env python3
"""Phase 2 local ASR, Caspi (Hebrew) side. Runs in its OWN venv
(~/.local/venvs/caspi-asr) because Caspi's loader -- the `qwen_asr` package --
hard-pins transformers==4.57.6, which conflicts with the transformers>=5.13.0
that Qwen3-ASR-1.7B-hf (scripts/asr_qwen.py) needs. See PLAN.md sec 7's Phase 2
design note. Only invoked when asr_qwen.py's 60s peek detects Hebrew.

Usage:
    ~/.local/venvs/caspi-asr/bin/python scripts/asr_caspi.py <audio.wav>
        [--dtype float16] [--max-new-tokens N] [--model PATH]

Writes JSON to stdout: {"engine": "caspi-1.7b", "text", "language", "seconds"}.
"""
import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_MODEL = str(Path.home() / ".local" / "venvs" / "qwen-asr" / "models" / "Caspi-1.7B")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    if not Path(args.audio).exists():
        raise SystemExit(f"[asr_caspi] FATAL: audio file not found: {args.audio}")

    import torch
    from qwen_asr import Qwen3ASRModel

    t0 = time.monotonic()
    print(f"[asr_caspi] loading {args.model} dtype={args.dtype} device=cpu ...", file=sys.stderr)
    torch_dtype = getattr(torch, args.dtype)
    model = Qwen3ASRModel.from_pretrained(
        args.model,
        dtype=torch_dtype,
        device_map="cpu",
        max_inference_batch_size=1,
        max_new_tokens=args.max_new_tokens,
    )
    load_seconds = round(time.monotonic() - t0, 2)
    print(f"[asr_caspi] loaded in {load_seconds:.1f}s", file=sys.stderr)

    t1 = time.monotonic()
    results = model.transcribe(audio=args.audio, language=None)
    generate_seconds = round(time.monotonic() - t1, 2)
    result0 = results[0]

    output = {
        "audio": args.audio,
        "engine": "caspi-1.7b",
        "language": result0.language,
        "text": result0.text,
        "seconds": {"load": load_seconds, "generate": generate_seconds},
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
