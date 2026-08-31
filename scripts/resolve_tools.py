#!/usr/bin/env python3
"""The §4 hidden-tool-name resolver (design spec Component 3). Tiers 1-2 are
pure Python URL extraction; tiers 3 (frame OCR) and 4 (web search) each wrap
one impure call and are exercised by interpret.py's orchestration, not run
standalone. See PLAN.md sec 4 and the design spec's escalation order (tier 3
moved last, deliberate deviation #3: the default fetch stays audio-only, only
a post that survives tiers 1/2/4 unresolved pays for a video download)."""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
URL_RE = re.compile(r"https?://[^\s)\]]+")


def _dedup(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def extract_urls(text):
    """Pure: every http(s) URL in text, order preserved, deduped, trailing
    punctuation stripped."""
    return _dedup(url.rstrip(".,;:!?)") for url in URL_RE.findall(text or ""))


def is_self_referential(url):
    """Pure: an instagram.com link back to the post itself/the platform is
    never the answer to "what tool did they demo"."""
    return "instagram.com" in url.lower()


def tier1_creator_replies(comments, channel):
    """Pure: URLs from comments authored by the post's own channel
    (case-insensitive), minus self-referential links."""
    channel_lower = (channel or "").lower()
    if not channel_lower:
        return []
    urls = []
    for comment in comments or []:
        if (comment.get("author") or "").lower() == channel_lower:
            urls.extend(u for u in extract_urls(comment.get("text") or "") if not is_self_referential(u))
    return _dedup(urls)


def tier2_caption_and_comments(description, comments):
    """Pure: URLs from the caption + every comment (not just the creator's),
    minus self-referential links -- the fallback when tier 1 finds nothing."""
    urls = list(extract_urls(description or ""))
    for comment in comments or []:
        urls.extend(extract_urls(comment.get("text") or ""))
    return _dedup(u for u in urls if not is_self_referential(u))


def resolve_pure_tiers(description, comments, channel):
    """Runs tier 1 then tier 2. Returns {status, tier, candidates} where
    status is 'resolved' (one candidate), 'uncertain' (several), or
    'not_found' (neither tier found anything -- not a final resolver status,
    just "the Python tiers have nothing"; interpret.py's agent call or tier 4
    may still resolve it from context/search)."""
    tier1 = tier1_creator_replies(comments, channel)
    if tier1:
        return {"status": "resolved" if len(tier1) == 1 else "uncertain", "tier": 1, "candidates": tier1}
    tier2 = tier2_caption_and_comments(description, comments)
    if tier2:
        return {"status": "resolved" if len(tier2) == 1 else "uncertain", "tier": 2, "candidates": tier2}
    return {"status": "not_found", "tier": None, "candidates": []}


def sample_frame_timestamps(duration_s, interval_s=5, max_frames=8):
    """Pure: evenly-spaced sample points for tier 3's frame OCR, capped so a
    long reel doesn't spend unbounded OCR calls. Always at least one frame."""
    if duration_s <= 0:
        return [0.0]
    count = min(max_frames, max(1, int(duration_s // interval_s) + 1))
    step = duration_s / count
    return [round(i * step, 2) for i in range(count)]


def resolve_via_frame_ocr(shortcode, media_root, duration_s):
    """Tier 3, last resort: re-fetch the mp4 (--keep-mp4), sample frames with
    ffmpeg, OCR each with the existing PP-OCRv6 pass (no escalation ladder --
    on-screen UI text is Latin-script screenshare, not the Hebrew-carousel
    case that ladder was built for), collect the recognized text as evidence
    for interpret.py's redraft call. Never raises -- a failed refetch/OCR
    degrades to no extra evidence rather than failing the whole run."""
    media_dir = Path(media_root) / shortcode
    mp4_path = media_dir / f"{shortcode}.mp4"
    if not mp4_path.exists():
        fetch_result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "fetch.py"),
             f"https://www.instagram.com/p/{shortcode}/", "--media-root", str(media_root), "--keep-mp4"],
            capture_output=True, text=True, timeout=900,
        )
        if fetch_result.returncode != 0 or not mp4_path.exists():
            return {"status": "unavailable", "texts": [], "error": fetch_result.stderr[-500:]}

    texts = []
    for ts in sample_frame_timestamps(duration_s):
        frame_path = media_dir / f"{shortcode}.frame_{ts:.2f}.jpg"
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", str(mp4_path), "-frames:v", "1", str(frame_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 or not frame_path.exists():
            continue
        ocr_result = subprocess.run(
            [str(Path.home() / ".local" / "venvs" / "paddleocr" / "bin" / "python"),
             str(REPO_ROOT / "scripts" / "ocr_local.py"), str(frame_path)],
            capture_output=True, text=True, timeout=120,
        )
        if ocr_result.returncode == 0:
            try:
                data = json.loads(ocr_result.stdout)
                if data.get("text"):
                    texts.append(data["text"])
            except json.JSONDecodeError:
                pass
    return {"status": "sampled" if texts else "no_text_found", "texts": texts, "error": None}


def resolve_via_web_search(search_agent_text, extract_json_fn):
    """Tier 4: parse the search agent's JSON response into the resolver
    contract. extract_json_fn is agents_lib.extract_json, passed in rather
    than imported to keep this module's dependency direction pointing away
    from agents_lib (resolve_tools doesn't need to know how the agent was
    invoked, only how to read its answer)."""
    try:
        data = extract_json_fn(search_agent_text)
    except ValueError:
        return {"status": "unresolved", "tool_name": None, "url": None, "sources": []}
    confidence = (data.get("confidence") or "low").lower()
    if not data.get("tool_name"):
        return {"status": "unresolved", "tool_name": None, "url": None, "sources": data.get("sources", [])}
    status = "resolved" if confidence == "high" else "uncertain"
    return {"status": status, "tool_name": data.get("tool_name"), "url": data.get("url"),
             "sources": data.get("sources", [])}
