#!/usr/bin/env python3
"""Phase 2 local ASR, Qwen3 side: loads Qwen3-ASR-1.7B-hf once, transcribes the
first 60s to detect language (PLAN.md sec 3). If that's Hebrew, exits early and
signals the caller to reroute to Caspi (scripts/asr_caspi.py, a SEPARATE venv --
Caspi needs the `qwen_asr` package, which hard-pins transformers==4.57.6,
incompatible with the transformers>=5.13.0 this checkpoint needs -- discovered
while building this task, see PLAN.md sec 7's Phase 2 design note). Otherwise
continues generating on the SAME already-loaded model for the full file -- no
redundant reload.

Usage:
    ~/.local/venvs/qwen-asr/bin/python scripts/asr_qwen.py <audio.wav>
        [--dtype float16] [--max-new-tokens N] [--model Qwen/Qwen3-ASR-1.7B-hf]

Writes JSON to stdout: either a full result ({"engine": "qwen3-asr-1.7b", "text",
"language", ...}) or a reroute signal ({"reroute_to": "caspi", "peek_language": "he"}).
"""
import argparse
import json
import sys
import time
from pathlib import Path


def should_reroute_to_caspi(detected_language: str) -> bool:
    """Pure routing decision from PLAN.md sec 3: only Hebrew reroutes. Qwen3-ASR
    emits full language names (verified: 'English', not 'en' -- PLAN.md's 'he'
    assumption was wrong), so match case-insensitively on the name, not a code."""
    return (detected_language or "").strip().lower() == "hebrew"


def _clip_wav(audio_path, seconds):
    """Write a temp copy of audio_path clipped to the first `seconds` seconds,
    for the cheap 60s language-detection peek (PLAN.md sec 3)."""
    import soundfile as sf
    data, samplerate = sf.read(audio_path)
    clipped = data[: int(seconds * samplerate)]
    tmp_path = str(Path(audio_path).with_suffix(f".peek{seconds}s.wav"))
    sf.write(tmp_path, clipped, samplerate)
    return tmp_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    ap.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B-hf")
    args = ap.parse_args()

    if not Path(args.audio).exists():
        raise SystemExit(f"[asr_qwen] FATAL: audio file not found: {args.audio}")

    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    t0 = time.monotonic()
    print(f"[asr_qwen] loading {args.model} dtype={args.dtype} device=cpu ...", file=sys.stderr)
    torch_dtype = getattr(torch, args.dtype)
    processor = AutoProcessor.from_pretrained(args.model)
    model = AutoModelForMultimodalLM.from_pretrained(args.model, dtype=torch_dtype, device_map="cpu")
    model.eval()
    load_seconds = round(time.monotonic() - t0, 2)
    print(f"[asr_qwen] loaded in {load_seconds:.1f}s", file=sys.stderr)

    def generate(audio_path, max_new_tokens):
        t = time.monotonic()
        inputs = processor.apply_transcription_request(audio=audio_path).to(model.device, model.dtype)
        with torch.inference_mode():
            output_ids = model.generate(**inputs, max_new_tokens=max_new_tokens)
        generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
        parsed = processor.decode(generated_ids, return_format="parsed")[0]
        return {
            "language": parsed.get("language"),
            "transcription": parsed.get("transcription"),
            "generate_seconds": round(time.monotonic() - t, 2),
        }

    peek_wav = _clip_wav(args.audio, 60)
    try:
        peek = generate(peek_wav, args.max_new_tokens)
    finally:
        Path(peek_wav).unlink(missing_ok=True)
    print(f"[asr_qwen] 60s peek language={peek['language']!r}", file=sys.stderr)

    if should_reroute_to_caspi(peek["language"]):
        print("[asr_qwen] Hebrew detected -> signaling reroute to Caspi (separate venv)", file=sys.stderr)
        print(json.dumps({"reroute_to": "caspi", "peek_language": peek["language"]}, ensure_ascii=False))
        return

    print("[asr_qwen] non-Hebrew -> continuing full file on already-loaded model", file=sys.stderr)
    full = generate(args.audio, args.max_new_tokens)
    result = {
        "audio": args.audio,
        "engine": "qwen3-asr-1.7b",
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
