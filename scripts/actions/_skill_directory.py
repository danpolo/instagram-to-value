#!/usr/bin/env python3
"""Resolves a skill *name* to the `owner/repo` identifier `npx skills add`
needs, against the skills.sh directory. Not a handler -- a helper shared by
skill_install, the way _knowledge_common is shared by note/reference.

Four `unsupported: skill_install` rationales in the pilot said the same thing:
the post names the skill and gives no URL, "and the registry has no action that
resolves skill names through a skill directory". This is that resolver.

Why dominance rather than uniqueness: the ecosystem is thick with forks. An
exact-name search for "impeccable" matches 73 distinct owners; "last30days"
matches 74. But the original outweighs the copies by three orders of magnitude
(pbakaus/impeccable 266,695 installs vs 1,015 for the runner-up; mvanhorn's
last30days 38,758 vs 29). So a name resolves only when one source both clears
an absolute floor and dwarfs every rival. Names that are merely crowded and
small -- "Claude Video" (13/6/2 installs), "Crucible" (9/8) -- resolve to
nothing, which is correct: neither post's skill is in the directory at all, and
this action carries exec risk."""
import json
import re
import urllib.parse
import urllib.request

SEARCH_URL = "https://skills.sh/api/search"
SEARCH_TIMEOUT_S = 25
# `npx skills add` and skill_install's SCHEMA both take exactly `owner/repo`.
# skills.sh also lists aggregator hosts (smithery.ai, wai-stacks.vercel.app) in
# the same field; those are not installable and must never win the ranking.
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
NOISE_TOKENS = {"skill", "skills"}
VERSION_TOKEN_RE = re.compile(r"^v?\d+(?:[._]\d+)*$")
# Both must hold for the top candidate, measured against the best rival source.
MIN_INSTALLS = 1000
MIN_DOMINANCE_RATIO = 10
MAX_CANDIDATES = 5


def normalize(text):
    """Pure: a caption's name reduced to the term the directory indexes.

    Creators write "Taste skill 2.0"; searching that string makes skills.sh
    fall back to semantic matching and return unrelated hits, while "taste"
    fuzzy-matches leonxlnx/taste-skill. So trailing version and "skill" tokens
    go, and separators collapse to '-'."""
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", (text or "").lower()) if t]
    while tokens and (tokens[-1] in NOISE_TOKENS or VERSION_TOKEN_RE.match(tokens[-1])):
        tokens.pop()
    return "-".join(t for t in tokens if t not in NOISE_TOKENS)


def _repo_of(source):
    return (source or "").split("/")[-1]


def exact_matches(query, skills):
    """Pure: entries whose skill id, display name, or repo name IS the query.
    Substring matches are deliberately excluded -- 'crucible' must not pull in
    'crucible-navigator', which is a different skill by a different author."""
    matched = []
    for entry in skills or []:
        source = entry.get("source") or ""
        if not OWNER_REPO_RE.match(source):
            continue
        names = {normalize(entry.get("skillId")), normalize(entry.get("name")),
                  normalize(_repo_of(source))}
        if query in names:
            matched.append(entry)
    return matched


def rank_sources(matches):
    """Pure: one entry per source -- its most-installed matching skill --
    ordered by installs. A repo publishing five matching skills is still one
    candidate; the comparison that matters is between owners."""
    best = {}
    for entry in matches:
        source = entry["source"]
        if entry.get("installs", 0) > best.get(source, {}).get("installs", -1):
            best[source] = entry
    return sorted(best.values(), key=lambda e: -e.get("installs", 0))


def choose(ranked):
    """Pure: the dominance verdict over ranked candidates."""
    candidates = [f"{e['source']}@{e['skillId']} ({e.get('installs', 0)} installs)"
                   for e in ranked[:MAX_CANDIDATES]]
    if not ranked:
        return {"status": "not_found", "source": None, "skill_id": None,
                "installs": None, "candidates": [], "error": None}
    top = ranked[0]
    runner_up = ranked[1].get("installs", 0) if len(ranked) > 1 else 0
    dominant = top.get("installs", 0) >= max(MIN_INSTALLS, runner_up * MIN_DOMINANCE_RATIO)
    if not dominant:
        return {"status": "uncertain", "source": None, "skill_id": None,
                "installs": None, "candidates": candidates, "error": None}
    return {"status": "resolved", "source": top["source"], "skill_id": top["skillId"],
            "installs": top.get("installs", 0), "candidates": candidates, "error": None}


def search(query):
    """The one impure call: skills.sh's search endpoint."""
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    with urllib.request.urlopen(url, timeout=SEARCH_TIMEOUT_S) as response:
        return json.load(response)


def resolve(name, search_fn=None):
    """name -> {status, source, skill_id, installs, candidates, error}.

    status is 'resolved' (one dominant owner/repo), 'uncertain' (candidates
    exist but none dominates) or 'not_found'. A directory outage degrades to
    not_found with `error` set -- this runs inside an approval tap and must
    never raise."""
    query = normalize(name) or (name or "").strip()
    if not query:
        return {"status": "not_found", "source": None, "skill_id": None,
                "installs": None, "candidates": [], "error": "empty skill name"}
    try:
        response = (search_fn or search)(query)
    except Exception as e:
        return {"status": "not_found", "source": None, "skill_id": None,
                "installs": None, "candidates": [], "error": str(e)}
    result = choose(rank_sources(exact_matches(query, response.get("skills", []))))
    result["query"] = query
    return result
