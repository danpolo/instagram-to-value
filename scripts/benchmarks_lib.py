#!/usr/bin/env python3
"""Registry for the benchmark gate (PLAN.md sec 9 #13, SESSION-6 handoff
"Phase B"). One record per installed skill/repo, keyed like /pending's
duplicate merge (staging_lib.action_key) so impeccable cloned by three posts
and added once via `npx skills add` is one artefact with four installs.

States: not_benchmarked -> drafted -> running -> passing | failing ->
verdict_sent -> uninstalled | kept_despite_failure. `running -> drafted` is
the retry edge: an agent error is not a verdict. Nothing here blocks use of a
skill -- the registry only records what the benchmark found and what Dan
decided.

state/benchmarks.json is shared by two processes (itv-bot registers installs
and records Remove/Keep, itv-worker runs benchmarks), so every change goes
through update(): lock, re-read, change, atomic write -- never a load held
across a multi-minute benchmark."""
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import staging_lib

BENCHMARKABLE_TYPES = ("skill_install", "skill", "git_repo", "package_install")
TRANSITIONS = {
    "not_benchmarked": {"drafted"},
    "drafted": {"running"},
    "running": {"passing", "failing", "drafted"},
    "failing": {"verdict_sent"},
    "verdict_sent": {"uninstalled", "kept_despite_failure"},
    "passing": set(),
    "uninstalled": set(),
    "kept_despite_failure": set(),
}
UNBENCHMARKED = ("not_benchmarked", "drafted", "running")
FAILING = ("failing", "verdict_sent")
# An agent error (quota, timeout, unparseable reply) is retried, but not on
# every worker poll.
DEFAULT_RETRY_AFTER_S = 6 * 3600


class InvalidTransition(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def empty():
    return {"records": {}}


def path_for(staging_root):
    """state/ sits beside staging/ -- so a test's tmp staging root never
    touches the live registry."""
    return Path(staging_root).resolve().parent / "state" / "benchmarks.json"


def bench_root_for(staging_root):
    return Path(staging_root).resolve().parent / "benchmarks"


def record_id(key):
    """Short and stable: it travels in Telegram callback data (64-byte limit)."""
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def transition(record, to, now, **fields):
    """Pure: a copy of record in state `to`, with fields set and the step
    appended to history. Raises InvalidTransition off the state machine."""
    current = record.get("state")
    if to not in TRANSITIONS.get(current, ()):
        raise InvalidTransition(f"{current} -> {to}")
    new = dict(record, **fields)
    new["state"] = to
    new["history"] = list(record.get("history") or []) + [{"state": to, "at": now}]
    return new


def set_state(registry, rid, to, now, **fields):
    registry["records"][rid] = transition(registry["records"][rid], to, now, **fields)
    return registry["records"][rid]


def artefact_key(action):
    key = staging_lib.action_key(action)
    if key is None and action.get("type") == "skill":
        key = f"skill:{((action.get('payload') or {}).get('name') or '').lower()}"
    return key


def _name(action, key):
    payload = action.get("payload") or {}
    if action.get("type") != "package_install" and payload.get("name"):
        return payload["name"]
    return re.split(r"[/:]", key)[-1]


def _unique_name(records, name, rid):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or rid
    if any(r["name"] == name for r in records.values()):
        return f"{name}-{rid[:4]}"
    return name


def register_install(registry, shortcode, action, result, now):
    """B1: record a successful install. A new artefact starts not_benchmarked;
    the same artefact from another post only gains an install entry. Returns
    the record, or None for types that are never benchmarked (rules, notes)."""
    if action.get("type") not in BENCHMARKABLE_TYPES:
        return None
    key = artefact_key(action)
    if not key:
        return None
    rid = record_id(key)
    records = registry.setdefault("records", {})
    record = records.get(rid)
    if record is None:
        payload = action.get("payload") or {}
        record = {"id": rid, "key": key, "name": _unique_name(records, _name(action, key), rid),
                  "type": action["type"],
                  "source": key.split(":", 1)[1] if key.startswith("repo:") else
                  (payload.get("url") or payload.get("source") or payload.get("command")),
                  "shortcode": shortcode, "installed_at": now, "state": "not_benchmarked",
                  "installs": [], "benchmark_path": None, "last_run": None, "last_error": None,
                  "attempted_at": None, "message_id": None, "history": []}
        records[rid] = record
    if not any(i["shortcode"] == shortcode and i["action_id"] == action["id"] for i in record["installs"]):
        result = result or {}
        record["installs"].append({"shortcode": shortcode, "action_id": action["id"], "type": action["type"],
                                   "path": result.get("path"), "command": result.get("command")})
    return record


def backfill(registry, staging_root):
    """Registers every already-installed artefact in staging/, oldest decision
    first. Idempotent. Returns the number of new records."""
    before = len(registry.get("records", {}))
    installed = [(a.get("decided_at") or "", p.get("shortcode"), a)
                 for p in staging_lib._all_proposals(staging_root)
                 for a in p.get("actions", []) if a.get("status") == "installed"]
    for decided_at, shortcode, action in sorted(installed, key=lambda t: t[0]):
        register_install(registry, shortcode, action, action.get("result"), decided_at or now_iso())
    return len(registry.get("records", {})) - before


def due(registry, now, retry_after_s=DEFAULT_RETRY_AFTER_S):
    """Records still to benchmark, skipping ones whose last attempt errored
    too recently. Only first installs are benchmarked -- a settled record is
    never re-run."""
    now_dt = _parse_ts(now)
    out = []
    for record in registry.get("records", {}).values():
        if record.get("state") not in UNBENCHMARKED:
            continue
        attempted = record.get("attempted_at")
        if attempted and (now_dt - _parse_ts(attempted)).total_seconds() < retry_after_s:
            continue
        out.append(record)
    return out


def attention(registry):
    records = list(registry.get("records", {}).values())
    return {"not_benchmarked": [r for r in records if r.get("state") in UNBENCHMARKED],
            "failing": [r for r in records if r.get("state") in FAILING]}


def load(path):
    path = Path(path)
    if not path.exists():
        return empty()
    return json.loads(path.read_text())


def save(path, registry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".benchmarks-")
    with os.fdopen(fd, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@contextmanager
def _locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def update(path, fn):
    """Lock, load, fn(registry) mutates it, save. Returns fn's result."""
    with _locked(path):
        registry = load(path)
        out = fn(registry)
        save(path, registry)
        return out
