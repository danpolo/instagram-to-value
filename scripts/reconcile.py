#!/usr/bin/env python3
"""Phase 0 reconciliation harness: diff two independent reads of the same media
and flag disagreement spans for Claude (Phase 4) to verify.
See PLAN.md sec 0b.3 / sec 7.

Built for ASR (local vs Groq) and reused unchanged for OCR (OCR.space vs
Gemini) once Hebrew images gained a second independent engine on 2026-08-30 --
the sec 0b.3 argument ("two engines disagreeing is the best signal that a token
is unreliable") is about having two engines, not about audio.

Usage:
    bin/python scripts/reconcile.py <a.json> <b.json> [--label-a X --label-b Y]

Where each input is a phase0_benchmark.py-style JSON with a "transcription" or
"text" field, or a plain .txt file. Writes disagreement spans to stdout as JSON.
Labels default to local/groq so existing audio callers are unaffected.
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path


def load_text(path):
    p = Path(path)
    if p.suffix == ".json":
        data = json.loads(p.read_text())
        return data.get("transcription") or data.get("text") or ""
    return p.read_text()


def tokenize(text):
    # Keep words + trailing punctuation attached, drop pure whitespace tokens.
    return re.findall(r"\S+", text)


def reconcile(local_text, groq_text, context=3, label_a="local", label_b="groq"):
    a = tokenize(local_text)
    b = tokenize(groq_text)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    spans = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        spans.append({
            "op": op,
            label_a: " ".join(a[i1:i2]),
            label_b: " ".join(b[j1:j2]),
            "context_before": " ".join(a[max(0, i1 - context):i1]),
            "context_after": " ".join(a[i2:i2 + context]),
        })

    total_tokens = max(len(a), len(b), 1)
    disagreement_tokens = sum(len(s[label_a].split()) + len(s[label_b].split()) for s in spans)
    agreement_ratio = 1 - (disagreement_tokens / (2 * total_tokens))

    return {
        "agreement_ratio": round(agreement_ratio, 4),
        f"{label_a}_word_count": len(a),
        f"{label_b}_word_count": len(b),
        "disagreement_spans": spans,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("local")
    ap.add_argument("groq")
    ap.add_argument("--out", default=None)
    ap.add_argument("--label-a", default="local")
    ap.add_argument("--label-b", default="groq")
    args = ap.parse_args()

    local_text = load_text(args.local)
    groq_text = load_text(args.groq)

    if not groq_text.strip():
        print(json.dumps({"error": f"{args.label_b} text is empty — API key not set, or that engine returned nothing"}), file=sys.stderr)
        sys.exit(1)

    result = reconcile(local_text, groq_text, label_a=args.label_a, label_b=args.label_b)
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out)
    print(out)
    print(f"[reconcile] agreement={result['agreement_ratio']:.1%} "
          f"spans={len(result['disagreement_spans'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
