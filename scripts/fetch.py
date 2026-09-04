#!/usr/bin/env python3
"""Phase 1 fetch layer: Instagram URL -> media/<shortcode>/ on disk.

Tries the video track first (yt-dlp). If yt-dlp reports there is no video
in the post, auto-branches to the image/carousel track (gallery-dl). Both
tracks use the same cookie jar and write into the same media/<shortcode>/
directory. See PLAN.md sec 2 (component decisions) and sec 6 (layout).

Usage:
    bin/python scripts/fetch.py <instagram-url> [--cookies PATH] [--media-root DIR]
                                 [--keep-mp4] [--keep-thumbnail]

By default only the caption/comments/metadata + 16kHz mono WAV are kept for
the video track -- the mp4 and thumbnail are downloaded-through, not saved
(pass --keep-mp4 / --keep-thumbnail to keep them). This matters: without
--keep-mp4, yt-dlp downloads audio-only (smaller/faster) rather than the
full video, since nothing else needs the video stream at this stage.

Writes a summary JSON to stdout and exits non-zero on a real failure
(never silently produces an empty/partial job -- see PLAN.md sec 8, risk 2).
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COOKIES = Path.home() / ".config" / "instagram" / "cookies.txt"
DEFAULT_MEDIA_ROOT = REPO_ROOT / "media"
DEFAULT_ARCHIVE = DEFAULT_MEDIA_ROOT / ".yt-dlp-archive.txt"

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels)/([A-Za-z0-9_-]+)")

# Substrings (checked case-insensitively) that mean "this post has no video,
# switch to the image/carousel track" rather than a real fetch failure.
# PLAN.md's original guess was "There is no video in this post"; verified
# 2026-08-25 against yt-dlp 2026.08.19 that the actual string is different.
NO_VIDEO_MARKERS = (
    "no video formats found",
    "there is no video in this post",
)

# Substrings that mean the session/cookies are the problem, not the post --
# must fail loudly per PLAN.md sec 8 risk 2, never produce a silent empty job.
AUTH_FAILURE_MARKERS = (
    "redirect to login page",
    "login required",
    "rate-limit reached",
)


def extract_shortcode(url: str) -> str:
    m = SHORTCODE_RE.search(url)
    if not m:
        raise SystemExit(f"[fetch] could not extract a shortcode from: {url}")
    return m.group(1)


def run(cmd, **kwargs):
    print(f"[fetch] $ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_auth_failure(combined_output: str):
    lower = combined_output.lower()
    for marker in AUTH_FAILURE_MARKERS:
        if marker in lower:
            raise SystemExit(
                "[fetch] FATAL: Instagram session looks dead or rate-limited "
                f"(matched {marker!r}). Re-export cookies to "
                f"{DEFAULT_COOKIES}, or wait out the rate limit. Refusing to "
                "produce a silent empty job."
            )


def try_video_track(url: str, shortcode: str, media_dir: Path, cookies: Path, archive: Path,
                     keep_mp4: bool = False, keep_thumbnail: bool = False):
    media_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--cookies", str(cookies),
        "--download-archive", str(archive),
        "--write-description",
        "--write-info-json",
        "--write-comments",
        "-o", f"{media_dir}/%(id)s.%(ext)s",
    ]
    if keep_thumbnail:
        cmd += ["--write-thumbnail"]

    if keep_mp4:
        # Default best video+audio merge -> {shortcode}.mp4. No -x here: it
        # deletes the source video unless paired with --keep-video, and
        # --keep-video turns out to preserve the raw pre-merge DASH fragments
        # (not just "the original"), leaving ~20MB of clutter per video.
        # Derive the WAV ourselves with ffmpeg afterward instead.
        pass
    else:
        # Skip the video stream entirely -- audio-only download is smaller
        # and faster, and yt-dlp's own ExtractAudio postprocessor (with
        # postprocessor-args for sample rate/channels) gets us straight to
        # the 16kHz mono WAV without ever touching an mp4.
        cmd += [
            "-f", "bestaudio/best",
            "--extract-audio", "--audio-format", "wav",  # force reencode; "best" (no format)
            # skips the postprocessor entirely when the source is already
            # audio-only, silently ignoring postprocessor-args below.
            "--postprocessor-args", "ExtractAudio:-ac 1 -ar 16000 -c:a pcm_s16le",
        ]
    cmd += [url]

    result = run(cmd)
    combined = result.stdout + result.stderr
    if result.returncode == 0:
        if keep_mp4:
            extract_wav(media_dir, shortcode)
        else:
            wav = media_dir / f"{shortcode}.wav"
            if wav.exists():
                wav.rename(media_dir / f"{shortcode}.16k.wav")
        return {"track": "video", "returncode": 0, "stderr_tail": result.stderr[-500:]}

    check_auth_failure(combined)

    lower = combined.lower()
    if any(marker in lower for marker in NO_VIDEO_MARKERS):
        return None  # signal: branch to image track

    raise SystemExit(
        f"[fetch] FATAL: yt-dlp failed for {shortcode} for an unrecognized "
        f"reason (not a known no-video marker, not a known auth marker):\n"
        f"{combined[-1500:]}"
    )


def extract_wav(media_dir: Path, shortcode: str):
    """16 kHz mono PCM WAV from the fetched mp4 -- the ASR input format
    proven in Phase 0 (see PLAN.md sec 2, 'Audio prep'). Only used when
    --keep-mp4 is set; otherwise yt-dlp produces the WAV directly from an
    audio-only download (see try_video_track)."""
    mp4 = media_dir / f"{shortcode}.mp4"
    wav = media_dir / f"{shortcode}.16k.wav"
    if not mp4.exists() or wav.exists():
        return
    cmd = ["ffmpeg", "-y", "-i", str(mp4), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    result = run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"[fetch] FATAL: ffmpeg WAV extraction failed for {shortcode}:\n{result.stderr[-1000:]}")


def try_image_track(url: str, shortcode: str, media_dir: Path, cookies: Path):
    media_dir.mkdir(parents=True, exist_ok=True)
    # No slide limit/range filter -- gallery-dl downloads every sidecar item
    # by default, and that's a hard requirement here (all carousel images
    # matter, not just a sample). Don't add --range or similar later without
    # re-checking this against a multi-slide post.
    cmd = [
        "gallery-dl",
        "--cookies", str(cookies),
        "--write-metadata",
        "-D", str(media_dir),  # exact directory, no per-extractor subfolders
        "-f", f"{shortcode}_{{num:>02}}.{{extension}}",  # preserves slide order
        url,
    ]
    result = run(cmd)
    combined = result.stdout + result.stderr
    check_auth_failure(combined)
    if result.returncode != 0:
        raise SystemExit(
            f"[fetch] FATAL: gallery-dl failed for {shortcode}:\n{combined[-1500:]}"
        )
    return {"track": "image", "returncode": 0, "stderr_tail": result.stderr[-500:]}


def summarize(media_dir: Path, shortcode: str, track_info: dict) -> dict:
    files = sorted(p.name for p in media_dir.iterdir() if p.is_file())
    summary = {
        "shortcode": shortcode,
        "media_dir": str(media_dir),
        "track": track_info["track"],
        "files": files,
        "num_files": len(files),
    }

    slides = sorted(media_dir.glob(f"{shortcode}_*.jpg")) + sorted(media_dir.glob(f"{shortcode}_*.mp4"))
    if slides:
        summary["num_slides"] = len(slides)

    info_json = media_dir / f"{shortcode}.info.json"
    if info_json.exists():
        try:
            info = json.loads(info_json.read_text())
            comments = info.get("comments") or []
            summary["comment_count_reported"] = info.get("comment_count")
            summary["comment_count_fetched"] = len(comments)
        except (json.JSONDecodeError, OSError):
            pass

    meta_jsons = sorted(media_dir.glob(f"{shortcode}_*.json"))
    if meta_jsons:
        try:
            first_meta = json.loads(meta_jsons[0].read_text())
            comments = first_meta.get("comments")
            if comments is not None:
                summary["comment_count_fetched"] = len(comments)
            if "comment_count" in first_meta:
                summary["comment_count_reported"] = first_meta["comment_count"]
        except (json.JSONDecodeError, OSError):
            pass

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--cookies", default=str(DEFAULT_COOKIES))
    ap.add_argument("--media-root", default=str(DEFAULT_MEDIA_ROOT))
    ap.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    ap.add_argument("--keep-mp4", action="store_true",
                     help="Keep the downloaded video file (off by default -- only the 16kHz WAV is needed for ASR)")
    ap.add_argument("--keep-thumbnail", action="store_true",
                     help="Keep the video's cover-frame thumbnail (off by default)")
    args = ap.parse_args()

    cookies = Path(args.cookies)
    media_root = Path(args.media_root)
    archive = Path(args.archive)
    if not cookies.exists():
        raise SystemExit(f"[fetch] FATAL: cookie jar not found at {cookies}")

    shortcode = extract_shortcode(args.url)
    media_dir = media_root / shortcode

    track_info = try_video_track(args.url, shortcode, media_dir, cookies, archive,
                                  keep_mp4=args.keep_mp4, keep_thumbnail=args.keep_thumbnail)
    if track_info is None:
        print(f"[fetch] {shortcode}: no video in post, switching to image/gallery-dl track", file=sys.stderr)
        track_info = try_image_track(args.url, shortcode, media_dir, cookies)

    summary = summarize(media_dir, shortcode, track_info)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
