#!/usr/bin/env python3
"""Provenance enrichment for install-type proposals (SESSION-6 Phase A).

A proposal used to say "install page-foundry" and nothing else -- not that the
repo has 2 stars and one author, not that the name appears nowhere in the
video and came from a comment written by the repo's own author. This module
attaches what a person would look up before approving: where the name came
from, GitHub standing, skills.sh standing, the README's opening line, and a
short list of flags derived from those facts.

It runs at interpret time and the result is persisted into proposal.json, so
/pending and Show full stay offline and the record says what was known at
approval time. Every network fetcher is injectable and returns
{status: ok|unavailable, ...}; none raises, and an unavailable fetch never
produces a flag -- flags claim only what was actually checked.

Usage:
    python3 scripts/enrich.py --pending [--staging-root DIR]
        [--extracted-root DIR] [--media-root DIR]

--pending re-enriches every pending proposal in place with no agent calls.
Actions that already carry complete evidence are left untouched, so a second
run is byte-identical and draws no API quota.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import _skill_directory
import secrets_lib
import staging_lib

COVERED_TYPES = ("skill_install", "git_repo", "package_install")
THRESHOLDS = {
    "low_adoption_stars": 100,   # fewer GitHub stars than this -> low_adoption
    "stale_days": 365,           # no push for longer than this -> stale
    # ASR mis-hears proper nouns ("impeccable" -> "Imperfectible"), so long
    # single-word names also match a near-spelling that shares their opening.
    "fuzzy_min_len": 6,
    "fuzzy_prefix": 4,
    "fuzzy_ratio": 0.65,
}
FLAG_ORDER = ("not_in_video", "self_promoted_in_comment", "low_adoption", "solo_author",
              "stale", "not_in_directory", "no_licence", "archived")
GITHUB_API = "https://api.github.com"
RAW_README = "https://raw.githubusercontent.com/{repo}/HEAD/{name}"
HTTP_TIMEOUT_S = 20
COMMENT_QUOTE_CHARS = 600
README_HEAD_CHARS = 300

OWNER_REPO = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
SKILLS_ADD_RE = re.compile(rf"\bskills\s+add\s+({OWNER_REPO})")
GITHUB_URL_RE = re.compile(r"github\.com[/:]([A-Za-z0-9][\w.-]*)/([A-Za-z0-9][\w.-]*?)(?:\.git)?/?$")
WORD_RE = re.compile(r"[A-Za-z0-9]+")


# --- pure: what to look for --------------------------------------------------------------

def repo_of(action):
    """Pure: the GitHub `owner/repo` an action points at, or None."""
    payload = action.get("payload") or {}
    if action.get("type") == "skill_install":
        return payload.get("source") or None
    if action.get("type") == "git_repo":
        m = GITHUB_URL_RE.search((payload.get("url") or "").strip())
        return f"{m.group(1)}/{m.group(2)}" if m else None
    if action.get("type") == "package_install":
        m = SKILLS_ADD_RE.search(payload.get("command") or "")
        return m.group(1) if m else None
    return None


def candidate_names(action, repo=None):
    """Pure: normalized names the post might use for this action's target."""
    payload = action.get("payload") or {}
    repo = repo or repo_of(action)
    raw = []
    if action.get("type") in ("skill_install", "git_repo"):
        raw.append(payload.get("name"))
    elif action.get("type") == "package_install" and not SKILLS_ADD_RE.search(payload.get("command") or ""):
        package = payload.get("package") or ""
        raw.append(package)
        bare = package.rsplit("/", 1)[-1]
        if len(bare) >= 4:
            raw.append(bare)
    if repo:
        raw.append(repo.split("/")[-1])
    names = []
    for name in raw:
        n = _skill_directory.normalize(name)
        if n and n not in names:
            names.append(n)
    return names


def _find_name(name, text):
    """Pure: the span of `text` naming `name`, or None. Multi-word names match
    as a contiguous word run; long single words also match an ASR near-miss."""
    words = list(WORD_RE.finditer(text))
    lowered = [w.group(0).lower() for w in words]
    parts = name.split("-")
    for i in range(len(lowered) - len(parts) + 1):
        if lowered[i:i + len(parts)] == parts:
            return text[words[i].start():words[i + len(parts) - 1].end()]
    joined = "".join(parts)
    for w, low in zip(words, lowered):
        if low == joined:
            return w.group(0)
        # Fuzzy for single-word names only: "@deepseek-ai/dsh" must not count
        # as named in a video that merely says "DeepSeek".
        if (len(parts) == 1 and len(joined) >= THRESHOLDS["fuzzy_min_len"]
                and len(low) >= THRESHOLDS["fuzzy_min_len"]
                and low[:THRESHOLDS["fuzzy_prefix"]] == joined[:THRESHOLDS["fuzzy_prefix"]]
                and SequenceMatcher(None, low, joined).ratio() >= THRESHOLDS["fuzzy_ratio"]):
            return w.group(0)
    return None


def _mention(names, repo, text):
    text = text or ""
    if repo and repo.lower() in text.lower():
        return repo
    for name in names:
        found = _find_name(name, text)
        if found:
            return found
    return None


def classify_provenance(names, repo, transcript, caption, comments):
    """Pure (A1): where the post names this target -- 'video' (transcript/OCR),
    'caption', 'comment', or 'agent_inferred' when it appears nowhere. For a
    comment, records its author and whether that comment itself carries the
    repo it recommends. It claims nothing about identity."""
    result = {"source": "agent_inferred", "matched": None, "comment_author": None,
              "comment_text": None, "comment_links_repo": False}
    for source, text in (("video", transcript), ("caption", caption)):
        found = _mention(names, repo, text)
        if found:
            return dict(result, source=source, matched=found)
    for comment in comments or []:
        text = (comment or {}).get("text") or ""
        found = _mention(names, repo, text)
        if found:
            return dict(result, source="comment", matched=found,
                        comment_author=comment.get("author"),
                        comment_text=text[:COMMENT_QUOTE_CHARS],
                        comment_links_repo=bool(repo) and repo.lower() in text.lower())
    return result


def derive_flags(action_type, provenance, directory, github, now):
    """Pure (A3): flags from checked facts only; an unavailable fetch adds none."""
    flags = set()
    source = (provenance or {}).get("source")
    if source in ("comment", "agent_inferred"):
        flags.add("not_in_video")
    if source == "comment" and provenance.get("comment_links_repo"):
        flags.add("self_promoted_in_comment")
    if github and github.get("status") == "ok":
        if github.get("stars") is not None and github["stars"] < THRESHOLDS["low_adoption_stars"]:
            flags.add("low_adoption")
        if github.get("contributors") == 1:
            flags.add("solo_author")
        if github.get("pushed_at"):
            pushed = datetime.fromisoformat(github["pushed_at"]).replace(tzinfo=timezone.utc)
            if (now - pushed).days > THRESHOLDS["stale_days"]:
                flags.add("stale")
        if github.get("licence") is None:
            flags.add("no_licence")
        if github.get("archived"):
            flags.add("archived")
    if (action_type == "skill_install" and directory and directory.get("status") == "ok"
            and directory.get("listed") is False):
        flags.add("not_in_directory")
    return [f for f in FLAG_ORDER if f in flags]


def readme_head(text):
    """Pure: title, tagline and first prose line of a README, markup stripped."""
    pieces, in_fence = [], False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[\s*\]\([^)]*\)", "", line)
        line = line.strip().lstrip("#").strip()
        if line:
            pieces.append(line)
        if len(pieces) == 3:
            break
    return " — ".join(pieces)[:README_HEAD_CHARS]


# --- impure: fetchers (A2) -- injectable, never raise ------------------------------------

def _unavailable(error):
    out = {"status": "unavailable", "error": str(error)}
    if getattr(error, "code", None) == 404:
        out["not_found"] = True   # a permanent answer, not worth retrying
    return out


def _open(url):
    headers = {"User-Agent": "instagram-to-value-enrich"}
    if url.startswith(GITHUB_API):
        headers["Accept"] = "application/vnd.github+json"
        token = secrets_lib.load_secret("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_S) as r:
        return r.read()


def http_get_json(url):
    body = _open(url)
    return json.loads(body) if body.strip() else []


def http_get_text(url):
    return _open(url).decode("utf-8", "replace")


def fetch_directory(name, source=None, search_fn=None):
    """skills.sh standing, via the same resolver skill_install uses on approval."""
    try:
        captured = {}

        def capture(query):
            captured["response"] = (search_fn or _skill_directory.search)(query)
            return captured["response"]

        resolved = _skill_directory.resolve(name, search_fn=capture)
        if resolved.get("error"):
            return _unavailable(resolved["error"])
        if source:
            entries = [e for e in (captured.get("response") or {}).get("skills", [])
                       if (e.get("source") or "").lower() == source.lower()]
            return {"status": "ok", "listed": bool(entries), "source": source,
                    "installs": max(e.get("installs", 0) for e in entries) if entries else None,
                    "dominant": resolved["status"] == "resolved"
                                and (resolved["source"] or "").lower() == source.lower()}
        return {"status": "ok", "listed": resolved["status"] in ("resolved", "uncertain"),
                "source": resolved["source"], "installs": resolved["installs"],
                "dominant": resolved["status"] == "resolved"}
    except Exception as e:
        return _unavailable(e)


def fetch_github(repo, get_json=None):
    """GitHub /repos + /contributors. A contributors failure keeps the repo facts."""
    get_json = get_json or http_get_json
    try:
        data = get_json(f"{GITHUB_API}/repos/{repo}")
    except Exception as e:
        return _unavailable(e)
    try:
        contributors = get_json(f"{GITHUB_API}/repos/{repo}/contributors?per_page=100")
        contributors = len(contributors) if isinstance(contributors, list) else None
    except Exception:
        contributors = None
    try:
        spdx = ((data.get("license") or {}).get("spdx_id")) if data.get("license") else None
        return {"status": "ok", "full_name": data.get("full_name"), "stars": data.get("stargazers_count"),
                "forks": data.get("forks_count"), "created_at": (data.get("created_at") or "")[:10] or None,
                "pushed_at": (data.get("pushed_at") or "")[:10] or None,
                # NOASSERTION: a licence file GitHub could not classify -- present, so not "no licence".
                "licence": "other" if spdx == "NOASSERTION" else spdx,
                "archived": bool(data.get("archived")), "contributors": contributors,
                "description": data.get("description")}
    except Exception as e:
        return _unavailable(e)


def fetch_readme(repo, get_text=None):
    """README head from raw.githubusercontent.com (not drawn from the REST quota)."""
    get_text = get_text or http_get_text
    error = None
    for name in ("README.md", "readme.md"):
        try:
            return {"status": "ok", "head": readme_head(get_text(RAW_README.format(repo=repo, name=name)))}
        except Exception as e:
            error = e
    return _unavailable(error)


LIVE_FETCHERS = {"directory": fetch_directory, "github": fetch_github, "readme": fetch_readme}


def _safe(fn, *args):
    try:
        result = fn(*args)
        return result if isinstance(result, dict) and "status" in result else _unavailable("bad fetcher result")
    except Exception as e:
        return _unavailable(e)


# --- orchestration (A4 core, A6) ---------------------------------------------------------

def needs_enrichment(action):
    """Pending covered actions with no evidence, or with a retryable failed fetch."""
    if action.get("type") not in COVERED_TYPES or action.get("status") != "pending":
        return False
    evidence = action.get("evidence")
    if not evidence:
        return True
    return any(part.get("status") == "unavailable" and not part.get("not_found")
               for part in (evidence.get("directory"), evidence.get("github"), evidence.get("readme")) if part)


def enrich_action(action, context, fetchers, now):
    action_type, payload = action["type"], action.get("payload") or {}
    repo = repo_of(action)
    directory = None
    if action_type == "skill_install" or (action_type == "package_install" and repo):
        directory = _safe(fetchers["directory"], payload.get("name") or repo.split("/")[-1], repo)
        if not repo and directory.get("status") == "ok":
            repo = directory.get("source")
    provenance = classify_provenance(candidate_names(action, repo), repo, context.get("transcript"),
                                     context.get("caption"), context.get("comments"))
    github = _safe(fetchers["github"], repo) if repo else None
    readme = _safe(fetchers["readme"], repo) if repo else None
    evidence = {"provenance": provenance, "repo": repo, "directory": directory, "github": github,
                "readme": readme, "checked_at": now.date().isoformat()}
    return evidence, derive_flags(action_type, provenance, directory, github, now)


def enrich_actions(actions, context, fetchers=None, now=None):
    """Adds `evidence` and `flags` to each action that needs them, in place.
    Never raises: interpret_post calls this right before writing a proposal."""
    fetchers = fetchers or LIVE_FETCHERS
    now = now or datetime.now(timezone.utc)
    for action in actions:
        try:
            if needs_enrichment(action):
                action["evidence"], action["flags"] = enrich_action(action, context, fetchers, now)
        except Exception as e:
            print(f"[enrich] WARNING: {action.get('id')} not enriched: {e}", file=sys.stderr)
    return actions


def load_context(shortcode, extracted_root, media_root):
    """File IO: transcript, caption and comments. Unlike interpret's loader it
    tolerates a missing extracted/ file -- a caption and comments still count."""
    context = {"transcript": "", "caption": "", "comments": []}
    extracted = Path(extracted_root) / f"{shortcode}.json"
    if extracted.exists():
        context["transcript"] = json.loads(extracted.read_text()).get("text", "")
    media_dir = Path(media_root) / shortcode
    info = media_dir / f"{shortcode}.info.json"
    if info.exists():
        data = json.loads(info.read_text())
        context["caption"], context["comments"] = data.get("description") or "", data.get("comments") or []
    elif (media_dir / f"{shortcode}.description").exists():
        context["caption"] = (media_dir / f"{shortcode}.description").read_text()
    else:
        sidecars = sorted(media_dir.glob(f"{shortcode}_*.json"))
        if sidecars:
            context["caption"] = json.loads(sidecars[0].read_text()).get("description", "")
    return context


def _serialize(proposal):
    return json.dumps(proposal, indent=2, ensure_ascii=False)


def enrich_pending(staging_root, extracted_root, media_root, fetchers=None, now=None):
    """A6: in-place re-enrichment of every pending proposal; no agent calls.
    Writes a proposal only when its content changed."""
    result = {"proposals": 0, "changed": [], "with_evidence": []}
    for shortcode in staging_lib.list_pending(staging_root):
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        result["proposals"] += 1
        before = _serialize(proposal)
        if any(needs_enrichment(a) for a in proposal["actions"]):
            enrich_actions(proposal["actions"], load_context(shortcode, extracted_root, media_root),
                           fetchers=fetchers, now=now)
        if _serialize(proposal) != before:
            staging_lib.write_proposal(shortcode, staging_root, proposal)
            result["changed"].append(shortcode)
        if any("evidence" in a for a in proposal["actions"]):
            result["with_evidence"].append(shortcode)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", action="store_true", required=True)
    ap.add_argument("--staging-root", default=str(REPO_ROOT / "staging"))
    ap.add_argument("--extracted-root", default=str(REPO_ROOT / "extracted"))
    ap.add_argument("--media-root", default=str(REPO_ROOT / "media"))
    args = ap.parse_args()
    result = enrich_pending(args.staging_root, args.extracted_root, args.media_root)
    print(json.dumps({"pending_proposals": result["proposals"], "changed": result["changed"],
                      "with_evidence": len(result["with_evidence"])}, indent=2))


if __name__ == "__main__":
    main()
