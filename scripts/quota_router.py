#!/usr/bin/env python3
"""Which subscription pays for a benchmark (Dan's cost rule, 2026-09-15).

Four quotas: Claude, Codex, Antigravity's Gemini pool and its third-party
pool. Each benchmark goes to the one most ahead of its weekly pace -- the
largest `pace_delta` (week elapsed % minus weekly used %) reported by the
QuotaPulse bridge (~/projects/QuotaPulse/bridge/bridge.py, GET /usage).
Each quota has a cheaper model for drafting and running tasks and a stronger
one for judging.

Usage:
    python3 scripts/quota_router.py     # prints the live pick
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = os.environ.get("QUOTAPULSE_URL", "http://100.123.127.134:8787/usage")
DEFAULT_TOKEN_PATH = Path.home() / ".quotapulse" / ".bridge_token"
# A quota whose 5-hour window is this spent can't finish a 4-6 call benchmark.
FIVE_HOUR_MAX_PCT = 90

PROFILES = {
    "claude": {"work": {"backend": "claude", "model": "sonnet", "effort": "medium"},
               "judge": {"backend": "claude", "model": "opus", "effort": "medium"}},
    "codex": {"work": {"backend": "codex", "model": "gpt-5.6-terra", "effort": "medium"},
              "judge": {"backend": "codex", "model": "gpt-5.6-sol", "effort": "medium"}},
    # Gemini's thinking level is part of the model id; agy's --effort is not used.
    "agy-gemini": {"work": {"backend": "agy", "model": "gemini-3.8-flash-low", "effort": None},
                   "judge": {"backend": "agy", "model": "gemini-3.8-flash-high", "effort": None}},
    "agy-3p": {"work": {"backend": "agy", "model": "claude-sonnet-4-6", "effort": None},
               "judge": {"backend": "agy", "model": "claude-opus-4-6-thinking", "effort": None}},
}


def windows(usage):
    """Pure: the bridge payload as {quota: {weekly, five_hour}}."""
    usage = usage or {}
    pools = (usage.get("antigravity") or {}).get("model_pools") or {}
    return {"claude": usage.get("claude"), "codex": usage.get("codex"),
            "agy-gemini": pools.get("gemini"), "agy-3p": pools.get("3p")}


def _already_reset(resets_at, now):
    if not resets_at:
        return False
    try:
        return datetime.fromisoformat(resets_at.replace("Z", "+00:00")) <= \
            datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False


def pick_quota(usage, now):
    """Pure: the quota with the largest weekly pace_delta, skipping any whose
    week is used up, whose pace is unknown, or whose current 5-hour window is
    nearly spent. None when nothing qualifies (or the bridge was unreachable)."""
    best = None
    for name, window in windows(usage).items():
        if not isinstance(window, dict):
            continue
        weekly = window.get("weekly") or {}
        pace, used = weekly.get("pace_delta"), weekly.get("pct")
        if pace is None or (used is not None and used >= 100):
            continue
        five = window.get("five_hour") or {}
        if (five.get("pct") or 0) >= FIVE_HOUR_MAX_PCT and not _already_reset(five.get("resets_at"), now):
            continue
        if best is None or pace > best[1]:
            best = (name, pace)
    return best[0] if best else None


def fetch_usage(url=DEFAULT_URL, token_path=DEFAULT_TOKEN_PATH, timeout=20):
    """The bridge's /usage JSON, or None. Never raises: no usage data means
    the benchmark waits, not that the worker dies."""
    try:
        token = Path(token_path).read_text().strip()
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"[quota_router] WARNING: no usage from {url}: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    usage = fetch_usage()
    now = datetime.now(timezone.utc).isoformat()
    for name, window in windows(usage).items():
        weekly = (window or {}).get("weekly") or {}
        print(f"{name:11} used {weekly.get('pct')}%  pace_delta {weekly.get('pace_delta')}")
    print("pick:", pick_quota(usage, now))
