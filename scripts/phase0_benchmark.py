#!/usr/bin/env python3
"""Phase 0 benchmark: load Qwen3-ASR-1.7B-hf on CPU, transcribe the reference
WAV, and record wall-clock + peak RSS. See PLAN.md sec 7 (Phase 0).

Usage:
    bin/python scripts/phase0_benchmark.py <audio.wav> [--dtype float16|bfloat16] [--max-new-tokens N]

Writes JSON result to out/phase0_<dtype>.json and prints a summary.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

def track_peak_rss(stop_event, peak_holder, interval=0.5):
    """Poll our own /proc/self/status VmRSS in a background thread."""
    path = Path("/proc/self/status")
    peak_kb = 0
    while not stop_event.is_set():
        try:
            for line in path.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    peak_kb = max(peak_kb, kb)
                    break
        except Exception:
            pass
        stop_event.wait(interval)
    peak_holder["peak_kb"] = peak_kb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--dtype", default="float16", choices=["float16", "bfloat16", "float32"])
    ap.add_argument("--max-new-tokens", type=int, default=1200)
    ap.add_argument("--model-id", default="Qwen/Qwen3-ASR-1.7B-hf")
    ap.add_argument("--model-dir", default=None, help="local snapshot dir, overrides model-id")
    args = ap.parse_args()

    peak_holder = {"peak_kb": 0}
    stop_event = threading.Event()
    tracker = threading.Thread(target=track_peak_rss, args=(stop_event, peak_holder), daemon=True)
    tracker.start()

    t0 = time.monotonic()
    import torch
    from transformers import AutoProcessor, AutoModelForMultimodalLM

    dtype = getattr(torch, args.dtype)
    model_id = args.model_dir or args.model_id

    print(f"[phase0] loading {model_id} dtype={args.dtype} device=cpu ...", file=sys.stderr)
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForMultimodalLM.from_pretrained(model_id, dtype=dtype, device_map="cpu")
    model.eval()
    t_loaded = time.monotonic()
    load_s = t_loaded - t0
    print(f"[phase0] loaded in {load_s:.1f}s, device={model.device} dtype={model.dtype}", file=sys.stderr)

    inputs = processor.apply_transcription_request(audio=args.audio).to(model.device, model.dtype)
    t_prep = time.monotonic()

    with torch.inference_mode():
        output_ids = model.generate(**inputs, max_new_tokens=args.max_new_tokens)
    t_gen = time.monotonic()
    gen_s = t_gen - t_prep

    generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]
    parsed = processor.decode(generated_ids, return_format="parsed")[0]

    stop_event.set()
    tracker.join(timeout=2)

    result = {
        "model_id": model_id,
        "dtype": args.dtype,
        "audio": args.audio,
        "load_seconds": round(load_s, 2),
        "generate_seconds": round(gen_s, 2),
        "total_seconds": round(t_gen - t0, 2),
        "peak_rss_kb": peak_holder["peak_kb"],
        "peak_rss_gb": round(peak_holder["peak_kb"] / (1024 * 1024), 3),
        "language": parsed.get("language"),
        "transcription": parsed.get("transcription"),
    }

    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"phase0_{args.dtype}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"[phase0] load={load_s:.1f}s generate={gen_s:.1f}s peak_rss={result['peak_rss_gb']}GB", file=sys.stderr)
    print(f"[phase0] language={parsed.get('language')}", file=sys.stderr)
    print(f"[phase0] wrote {out_path}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
