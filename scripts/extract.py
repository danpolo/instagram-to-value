#!/usr/bin/env python3
"""Phase 2 orchestrator: media/<shortcode>/ -> extracted/<shortcode>.json.
Shells out to each engine's own venv Python -- torch/transformers (Qwen3-ASR)
in the qwen-asr venv, qwen_asr package (Caspi) in its own caspi-asr venv
(hard-pins transformers==4.57.6, incompatible with qwen-asr's transformers>=
5.13.0 -- see asr_caspi.py), paddleocr in its own venv, the two REST wrappers
under system Python. Sidesteps dependency conflicts between these, matches
the existing "each heavy dependency gets its own venv" pattern (yt-dlp,
gallery-dl, qwen-asr). See PLAN.md sec 7's Phase 2 design note for the full
schema/routing rationale.

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
CASPI_VENV_PYTHON = Path.home() / ".local" / "venvs" / "caspi-asr" / "bin" / "python"
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


GARBLED_DIGIT_RATIO_THRESHOLD = 0.25


def looks_garbled(texts, digit_ratio_threshold=GARBLED_DIGIT_RATIO_THRESHOLD):
    """Pure: catches confidently-wrong PP-OCRv6 output on Hebrew glyphs that
    the mean-score gate alone misses. Found via real Hebrew-carousel testing
    (PLAN.md sec 7's Phase 2 execution notes): PP-OCRv6 forces Hebrew shapes
    into its Latin/digit charset, and the result is often *high per-region
    confidence* despite being pure noise -- three slides scored 0.78-0.80
    (above the 0.70 threshold) while reading as e.g.
    'nn1PY,71XD 70 0171Vy 11V 17nX'. Real Latin/CJK text is never anywhere
    close to this digit-heavy (observed: 67-73% digits on the garbled slides,
    vs. a handful of percent for ordinary prose/headlines)."""
    combined = "".join(texts)
    alnum = sum(c.isalnum() for c in combined)
    if alnum == 0:
        return False
    digits = sum(c.isdigit() for c in combined)
    return (digits / alnum) > digit_ratio_threshold


def needs_gemini_escalation(texts, scores, threshold=OCR_CONFIDENCE_THRESHOLD):
    """Pure: collapses PLAN.md sec 0 Correction 1's two-branch script-detect
    diagram into one rule. Escalates when PP-OCRv6 found nothing, found
    something but isn't confident, or found something that scores confident
    but reads as garbled (see looks_garbled) -- all three are what happens on
    Hebrew glyphs, since PP-OCRv6 has no RTL model at all."""
    if not texts:
        return True
    mean_score = sum(scores) / len(scores) if scores else 0.0
    if mean_score < threshold:
        return True
    return looks_garbled(texts)


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

    qwen_result = run_json_subprocess(
        [str(QWEN_VENV_PYTHON), str(REPO_ROOT / "scripts" / "asr_qwen.py"), str(wav)]
    )
    if qwen_result.get("reroute_to") == "caspi":
        local = run_json_subprocess(
            [str(CASPI_VENV_PYTHON), str(REPO_ROOT / "scripts" / "asr_caspi.py"), str(wav)]
        )
    else:
        local = qwen_result

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
