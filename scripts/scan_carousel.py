#!/usr/bin/env python3
"""Find one multi-slide carousel URL from a candidate list, WITHOUT hammering
Instagram. Real delay + jitter between requests, hard request cap, and stops
immediately (not after finishing the batch) on any sign of an auth/rate
problem. See PLAN.md sec 9 -- a tight loop of ~46 requests in ~90s previously
triggered Instagram's automated-behavior warning on the account.

Usage:
    bin/python scripts/scan_carousel.py [--list PATH] [--start-index N]
        [--max-requests N] [--min-delay S] [--max-delay S] [--cookies PATH]

Prints the first URL found with >1 media item and exits 0. Exits 1 if the
whole capped batch turns up nothing (raise --max-requests and re-run with a
later --start-index to keep looking -- in a separate invocation, not a
bigger loop).
"""
import argparse
import random
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIST = REPO_ROOT / "out" / "urls_non_video.txt"
DEFAULT_COOKIES = Path.home() / ".config" / "instagram" / "cookies.txt"

AUTH_FAILURE_MARKERS = ("redirect to login page", "login required", "rate-limit reached")


def probe(url: str, cookies: Path) -> list:
    cmd = ["gallery-dl", "--cookies", str(cookies), "-g", url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    combined = (result.stdout + result.stderr).lower()
    for marker in AUTH_FAILURE_MARKERS:
        if marker in combined:
            raise SystemExit(
                f"[scan] FATAL: hit {marker!r} on {url} -- stopping immediately, "
                "not continuing the batch. Cool down before trying again."
            )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=str(DEFAULT_LIST))
    ap.add_argument("--start-index", type=int, default=0)
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--min-delay", type=float, default=8.0)
    ap.add_argument("--max-delay", type=float, default=20.0)
    ap.add_argument("--cookies", default=str(DEFAULT_COOKIES))
    args = ap.parse_args()

    urls = [line.strip() for line in Path(args.list).read_text().splitlines() if line.strip()]
    cookies = Path(args.cookies)
    batch = urls[args.start_index:args.start_index + args.max_requests]

    for i, url in enumerate(batch):
        if i > 0:
            delay = random.uniform(args.min_delay, args.max_delay)
            print(f"[scan] sleeping {delay:.1f}s before next request", file=sys.stderr)
            time.sleep(delay)

        idx = args.start_index + i
        print(f"[scan] [{idx}] probing {url}", file=sys.stderr)
        items = probe(url, cookies)
        print(f"[scan] [{idx}] {len(items)} item(s)", file=sys.stderr)

        if len(items) > 1:
            print(f"[scan] FOUND carousel ({len(items)} slides) at index {idx}: {url}", file=sys.stderr)
            print(url)
            return

    print(f"[scan] no carousel found in indices [{args.start_index}, {args.start_index + len(batch)}) "
          f"-- re-run with --start-index {args.start_index + len(batch)}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
