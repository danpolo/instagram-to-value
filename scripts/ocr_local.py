#!/usr/bin/env python3
"""Phase 2 local OCR: PP-OCRv6 via the onnxruntime inference engine (lighter than
the full PaddlePaddle framework install -- fine for a CPU-only machine at this
volume, PLAN.md sec 2). Handles Latin/CJK text directly; has no Hebrew/RTL model
at all, so Hebrew images come back empty or low-confidence -- extract.py's
needs_escalation() is what routes those to the API engines, OCR.space Engine 3
first and Gemini second (PLAN.md sec 0 Correction 1, sec 7's Phase 2 design
note).

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
