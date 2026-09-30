#!/usr/bin/env python3
"""The benchmark gate: draft -> run -> judge -> verdict (PLAN.md sec 9 #13,
SESSION-6 handoff "Phase B", steps B2-B4).

For one registered artefact (benchmarks_lib):
  B2  one agent call reads its SKILL.md/README and writes 2-4 tasks with
      explicit pass criteria -> benchmarks/<name>/bench.md (+ bench.json).
  B3  each task runs in a throwaway directory -- the agent gets the skill's
      documentation, can write files there, and has no shell or network --
      then one judge call scores every task against its criteria. Each run is
      archived under benchmarks/<name>/runs/<timestamp>/.
  B4  pass: the record is updated, nothing is sent. Fail: one Telegram
      message naming the failing task, with Remove / Keep.

The quota is chosen per benchmark by quota_router (most ahead of weekly
pace). An agent error is never a verdict: the record falls back and is
retried later. Nothing here removes anything -- only a Remove tap does.

Usage:
    python3 scripts/benchmark.py --backfill
    python3 scripts/benchmark.py --list
    python3 scripts/benchmark.py --next
    python3 scripts/benchmark.py --record impeccable [--stub-verdict fail]
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))

import _skills_root
import agents_lib
import benchmarks_lib
import quota_router

DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"
MIN_TASKS, MAX_TASKS = 2, 4
DOC_MAX_CHARS = 30000
REPLY_MAX_CHARS = 12000
FILE_MAX_CHARS = 4000
FILES_MAX_CHARS = 16000
DRAFT_TIMEOUT_S = JUDGE_TIMEOUT_S = 600
RUN_TIMEOUT_S = 900

TASKS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tasks"],
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "title", "prompt", "pass_criteria"],
        "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "prompt": {"type": "string"},
                       "pass_criteria": {"type": "array", "items": {"type": "string"}}}}}},
}
VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tasks"],
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "passed", "reason"],
        "properties": {"id": {"type": "string"}, "passed": {"type": "boolean"}, "reason": {"type": "string"}}}}},
}


# --- pure pieces -----------------------------------------------------------------------

def find_skill_doc(path):
    """The file that says what an install does: a SKILL.md (the path itself,
    at the root, or a repo's own .claude/skills/<name>/SKILL.md), else README.md."""
    if not path:
        return None
    path = Path(path)
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    nested = sorted(path.glob(".claude/skills/*/SKILL.md"), key=lambda p: (p.parent.name != path.name, str(p)))
    for candidate in [path / "SKILL.md", *nested, path / "README.md"]:
        if candidate.is_file():
            return candidate
    return None


def _items(text, required):
    """{"tasks": [...]} items from an agent reply; when the wrapper is broken
    (Claude drops closing braces -- explain._parse_items), every complete
    object carrying the required keys."""
    try:
        data = agents_lib.extract_json(text or "")
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            return [i for i in data["tasks"] if isinstance(i, dict)]
    except ValueError:
        pass
    decoder, items, i = json.JSONDecoder(), [], (text or "").find("{", 1)
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict) and set(required) <= obj.keys():
                items.append(obj)
                i = text.find("{", end)
                continue
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return items


def parse_tasks(text):
    tasks, seen = [], set()
    for item in _items(text, ("id", "prompt", "pass_criteria")):
        criteria = item.get("pass_criteria")
        criteria = [criteria] if isinstance(criteria, str) else criteria
        prompt = str(item.get("prompt") or "").strip()
        if not (isinstance(criteria, list) and criteria and prompt):
            continue
        tid = str(item.get("id") or f"t{len(tasks) + 1}")
        if tid in seen:
            tid = f"t{len(tasks) + 1}"
        seen.add(tid)
        tasks.append({"id": tid, "title": str(item.get("title") or tid), "prompt": prompt,
                      "pass_criteria": [str(c) for c in criteria]})
    if len(tasks) < MIN_TASKS:
        raise ValueError(f"draft has {len(tasks)} usable tasks, need {MIN_TASKS}-{MAX_TASKS}: {(text or '')[:200]!r}")
    return tasks[:MAX_TASKS]


def parse_verdicts(text, task_ids):
    verdicts = {}
    for item in _items(text, ("id", "passed")):
        tid = str(item.get("id"))
        if tid in task_ids and isinstance(item.get("passed"), bool):
            verdicts[tid] = {"passed": item["passed"], "reason": str(item.get("reason") or "").strip()}
    missing = [t for t in task_ids if t not in verdicts]
    if missing:
        raise ValueError(f"judge gave no verdict for {missing}: {(text or '')[:200]!r}")
    return verdicts


def build_draft_prompt(name, doc_name, doc_text):
    return f"""BENCHMARK DRAFT
You are writing a small acceptance benchmark for "{name}", an agent skill or
tool that was just installed. Its documentation ({doc_name}) is below.

Write {MIN_TASKS} to {MAX_TASKS} concrete tasks that exercise what the documentation says it
does. Each task is given to a fresh AI agent together with the same
documentation, in an EMPTY working directory, with no shell, no network and
no access to the tool's code: the agent can only read the documentation and
write files into that directory. So every task must be completable that way
(produce a file, a plan, a review, a rewrite, a config...) and must carry any
input it needs inline.

For each task:
- "id": "t1", "t2", ...
- "title": a few plain words
- "prompt": the full instruction to the agent, inputs inline
- "pass_criteria": 1-4 explicit statements a judge can check from the agent's
  reply and files alone, based on what the documentation promises -- not on
  generic quality.

Documentation:
<<<
{doc_text[:DOC_MAX_CHARS]}
>>>

Respond with ONLY JSON: {{"tasks": [{{"id": "t1", "title": "...", "prompt": "...", "pass_criteria": ["..."]}}]}}"""


def build_run_prompt(name, doc_text, task, workdir):
    return f"""You have the "{name}" skill. Its instructions are below; follow them for this task.

<skill>
{doc_text[:DOC_MAX_CHARS]}
</skill>

Task: {task['prompt']}

Work only inside {workdir}: write every file the task asks for there, using
absolute paths under it. You have no shell and no network. When done, reply
with a short summary of what you produced."""


def build_judge_prompt(name, tasks, outputs):
    cases = [{"id": t["id"], "title": t["title"], "prompt": t["prompt"], "pass_criteria": t["pass_criteria"],
              "agent_reply": outputs[t["id"]]["reply"], "files": outputs[t["id"]]["files"]} for t in tasks]
    return f"""BENCHMARK JUDGE
You are judging whether an agent using the "{name}" skill passed each task
below. Judge ONLY against each task's pass_criteria, strictly: a task passes
only if every criterion is clearly met by the agent_reply and files shown.
Output that is missing or empty fails.

{json.dumps(cases, ensure_ascii=False, indent=1)}

Respond with ONLY JSON: {{"tasks": [{{"id": "t1", "passed": true, "reason": "one sentence naming the criterion that decided it"}}]}}"""


def collect_workdir(workdir):
    files, budget = [], FILES_MAX_CHARS
    for path in sorted(Path(workdir).rglob("*")):
        if not path.is_file() or budget <= 0:
            continue
        try:
            content = path.read_text()[:min(FILE_MAX_CHARS, budget)]
        except (UnicodeDecodeError, OSError):
            content = "(binary file)"
        budget -= len(content)
        files.append({"path": str(path.relative_to(workdir)), "content": content})
    return files


def render_bench_md(name, doc_path, tasks, drafted_by, now):
    lines = [f"# Benchmark: {name}", "", f"Drafted {now} by {drafted_by} from `{doc_path}`.", ""]
    for task in tasks:
        lines += [f"## {task['id']} — {task['title']}", "", task["prompt"], "", "**Pass criteria**", ""]
        lines += [f"- {c}" for c in task["pass_criteria"]] + [""]
    return "\n".join(lines)


def _short(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_failure_message(record, task, reason):
    """Plain words only (Dan's /pending preference): what failed and why."""
    return (f"❌ {record['name']} failed its benchmark\n\n"
            f"Task: {task['title']} — {_short(task['prompt'], 200)}\n"
            f"Why it failed: {_short(reason, 300)}\n\n"
            "It is still installed and usable. Remove it?")


def failure_buttons(rid):
    return [[("🗑 Remove", f"bench_rm:{rid}"), ("👍 Keep", f"bench_keep:{rid}")]]


# --- impure ------------------------------------------------------------------------------

def _doc_for(record):
    for install in record.get("installs", []):
        doc = find_skill_doc(install.get("path"))
        if doc:
            return doc
    return find_skill_doc(_skills_root.SKILLS_ROOT / record["name"])


def _agent_kw(spec, timeout, schema=None):
    kw = {"backend": spec["backend"], "model": spec["model"], "effort": spec.get("effort"), "timeout": timeout}
    if schema is not None:
        kw["schema"] = schema
    return kw


def _change(registry_path, rid, fn):
    def apply(registry):
        registry["records"][rid] = fn(registry["records"][rid])
        return registry["records"][rid]
    return benchmarks_lib.update(registry_path, apply)


def _telegram(text, buttons):
    import telegram_notify
    return telegram_notify.send_with_buttons(text, buttons)


def benchmark_one(rid, *, registry_path, bench_root, usage_fn=quota_router.fetch_usage,
                  run_agent_fn=agents_lib.run_agent, notify_fn=None, stub_verdict=None,
                  now_fn=benchmarks_lib.now_iso):
    """Benchmarks one record end to end. Returns {name, verdict: pass | fail |
    error | deferred, quota, agent_calls, wall_s, run_dir, error}."""
    started, now = time.monotonic(), now_fn()
    record = benchmarks_lib.load(registry_path)["records"][rid]
    summary = {"name": record["name"], "verdict": None, "quota": None, "agent_calls": 0,
               "wall_s": 0.0, "run_dir": None, "error": None}

    def finish(verdict, error=None):
        summary.update(verdict=verdict, error=error, wall_s=round(time.monotonic() - started, 1))
        return summary

    def fail_attempt(error):
        def fn(rec):
            if rec["state"] == "running":
                rec = benchmarks_lib.transition(rec, "drafted", now)
            return dict(rec, last_error=error, attempted_at=now)
        _change(registry_path, rid, fn)
        return finish("error", error)

    quota = quota_router.pick_quota(usage_fn(), now)
    if quota is None:
        return finish("deferred", "no quota has headroom (or QuotaPulse is unreachable)")
    profile, summary["quota"] = quota_router.PROFILES[quota], quota
    if record["state"] == "running":  # a previous run died mid-way
        record = _change(registry_path, rid, lambda rec: benchmarks_lib.transition(rec, "drafted", now))
    if record["state"] not in ("not_benchmarked", "drafted"):
        return finish("skipped", f"already {record['state']}")

    doc = _doc_for(record)
    if doc is None:
        return fail_attempt("no SKILL.md or README.md found at any install path")
    doc_text = doc.read_text(errors="replace")
    bench_dir = Path(bench_root) / record["name"]

    # B2: draft once; a retry after a run error reuses the tasks.
    if record["state"] == "not_benchmarked" or not (bench_dir / "bench.json").exists():
        result = run_agent_fn(build_draft_prompt(record["name"], doc.name, doc_text),
                              **_agent_kw(profile["work"], DRAFT_TIMEOUT_S, TASKS_SCHEMA))
        summary["agent_calls"] += 1
        if not result.get("ok"):
            return fail_attempt(f"draft: {result.get('error')}")
        try:
            tasks = parse_tasks(result.get("text"))
        except ValueError as e:
            return fail_attempt(f"draft: {e}")
        bench_dir.mkdir(parents=True, exist_ok=True)
        drafted_by = f"{quota} ({profile['work']['model']})"
        (bench_dir / "bench.md").write_text(render_bench_md(record["name"], doc, tasks, drafted_by, now))
        (bench_dir / "bench.json").write_text(json.dumps({"doc": str(doc), "tasks": tasks}, indent=2,
                                                         ensure_ascii=False))
        if record["state"] == "not_benchmarked":
            _change(registry_path, rid, lambda rec: benchmarks_lib.transition(
                rec, "drafted", now, benchmark_path=str(bench_dir / "bench.md")))
    else:
        tasks = json.loads((bench_dir / "bench.json").read_text())["tasks"]

    # B3: run each task in a throwaway directory, archive what it produced.
    _change(registry_path, rid, lambda rec: benchmarks_lib.transition(rec, "running", now, attempted_at=now))
    run_dir = bench_dir / "runs" / now.replace(":", "").replace("+0000", "Z")
    summary["run_dir"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for task in tasks:
        workdir = Path(tempfile.mkdtemp(prefix=f"bench-{record['name']}-"))
        try:
            result = run_agent_fn(build_run_prompt(record["name"], doc_text, task, workdir),
                                  workdir=str(workdir), **_agent_kw(profile["work"], RUN_TIMEOUT_S))
            summary["agent_calls"] += 1
            files = collect_workdir(workdir)
            shutil.copytree(workdir, run_dir / task["id"], dirs_exist_ok=True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        outputs[task["id"]] = {"ok": bool(result.get("ok")), "error": result.get("error"),
                               "reply": (result.get("text") or "")[:REPLY_MAX_CHARS], "files": files}

    def archive(verdicts, verdict, error=None):
        (run_dir / "run.json").write_text(json.dumps({
            "record": rid, "name": record["name"], "quota": quota, "profile": profile, "started_at": now,
            "doc": str(doc), "agent_calls": summary["agent_calls"],
            "wall_s": round(time.monotonic() - started, 1), "tasks": tasks, "outputs": outputs,
            "verdicts": verdicts, "verdict": verdict, "error": error}, indent=2, ensure_ascii=False))

    broken = [tid for tid, out in outputs.items() if not out["ok"]]
    if broken:  # the agent never got to try -- that says nothing about the skill
        error = f"run {broken[0]}: {outputs[broken[0]]['error']}"
        archive(None, "error", error)
        return fail_attempt(error)

    if stub_verdict:
        verdicts = {t["id"]: {"passed": stub_verdict == "pass",
                              "reason": f"forced {stub_verdict} (stub judge, no judge call)"} for t in tasks}
    else:
        result = run_agent_fn(build_judge_prompt(record["name"], tasks, outputs),
                              **_agent_kw(profile["judge"], JUDGE_TIMEOUT_S, VERDICT_SCHEMA))
        summary["agent_calls"] += 1
        try:
            if not result.get("ok"):
                raise ValueError(result.get("error"))
            verdicts = parse_verdicts(result.get("text"), [t["id"] for t in tasks])
        except ValueError as e:
            archive(None, "error", f"judge: {e}")
            return fail_attempt(f"judge: {e}")

    # B4: verdict.
    failed = [t for t in tasks if not verdicts[t["id"]]["passed"]]
    archive(verdicts, "fail" if failed else "pass")
    if not failed:
        _change(registry_path, rid, lambda rec: benchmarks_lib.transition(
            rec, "passing", now, last_run=str(run_dir), last_error=None))
        return finish("pass")
    record = _change(registry_path, rid, lambda rec: benchmarks_lib.transition(
        rec, "failing", now, last_run=str(run_dir), last_error=None))
    try:
        message_id = (notify_fn or _telegram)(format_failure_message(record, failed[0], verdicts[failed[0]["id"]]["reason"]),
                                              failure_buttons(rid))
    except Exception as e:  # stays `failing`; /benchmarks still lists it
        _change(registry_path, rid, lambda rec: dict(rec, last_error=f"telegram: {e}"))
        return finish("fail", f"telegram: {e}")
    _change(registry_path, rid, lambda rec: benchmarks_lib.transition(rec, "verdict_sent", now, message_id=message_id))
    return finish("fail")


def run_next(*, registry_path, bench_root, usage_fn=quota_router.fetch_usage, run_agent_fn=agents_lib.run_agent,
             notify_fn=None, now_fn=benchmarks_lib.now_iso):
    """Benchmarks the first due record, or returns None when nothing is due."""
    due = benchmarks_lib.due(benchmarks_lib.load(registry_path), now_fn())
    if not due:
        return None
    return benchmark_one(due[0]["id"], registry_path=registry_path, bench_root=bench_root, usage_fn=usage_fn,
                         run_agent_fn=run_agent_fn, notify_fn=notify_fn, now_fn=now_fn)


def remove_installs(record, allowed_roots=None, sync_fn=None):
    """Deletes what a record installed -- only on Dan's Remove tap. Only paths
    strictly inside an install root are touched; a package-manager install
    has no path and is reported for manual removal. Returns plain notes."""
    import install_artifact
    roots = install_artifact.ALLOWED_ROOTS if allowed_roots is None else allowed_roots
    resolved_roots = {Path(r).resolve() for r in roots}
    notes, touched_skills = [], False
    for install in record.get("installs", []):
        if not install.get("path"):
            notes.append(f"not removed automatically: `{install.get('command') or install.get('type')}` "
                         "— uninstall that one by hand")
            continue
        target = Path(install["path"])
        if install.get("type") == "skill" or target.name == "SKILL.md":
            target = target.parent
        try:
            resolved = install_artifact.validate_path(target, allowed_roots=roots, allowed_files=[])
        except install_artifact.PathNotAllowedError:
            notes.append(f"left {target} alone — outside the install folders")
            continue
        if resolved in resolved_roots:
            notes.append(f"left {target} alone — that is an install folder itself")
            continue
        if not resolved.exists():
            notes.append(f"{target} was already gone")
            continue
        shutil.rmtree(resolved) if resolved.is_dir() else resolved.unlink()
        notes.append(f"deleted {target}")
        touched_skills = touched_skills or resolved.is_relative_to(_skills_root.SKILLS_ROOT)
    if touched_skills:
        sync_note = (sync_fn or _skills_root.sync)()
        if sync_note:
            notes.append(sync_note)
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--backfill", action="store_true", help="register already-installed artefacts")
    group.add_argument("--list", action="store_true")
    group.add_argument("--next", action="store_true", help="benchmark the first due record")
    group.add_argument("--record", help="benchmark this record (name or id) now")
    ap.add_argument("--stub-verdict", choices=("pass", "fail"), help="skip the judge call and force a verdict")
    args = ap.parse_args()

    registry_path = benchmarks_lib.path_for(args.staging_root)
    bench_root = benchmarks_lib.bench_root_for(args.staging_root)
    if args.backfill:
        added = benchmarks_lib.update(registry_path, lambda reg: benchmarks_lib.backfill(reg, args.staging_root))
        print(f"added {added} records")
        args.list = True
    if args.list:
        records = benchmarks_lib.load(registry_path)["records"].values()
        for record in records:
            print(f"{record['state']:22} {record['id']}  {record['name']}  ({len(record['installs'])} installs)")
        return
    if args.next:
        summary = run_next(registry_path=registry_path, bench_root=bench_root)
    else:
        records = benchmarks_lib.load(registry_path)["records"]
        match = records.get(args.record) or next((r for r in records.values() if r["name"] == args.record), None)
        if match is None:
            raise SystemExit(f"no record {args.record!r}")
        summary = benchmark_one(match["id"], registry_path=registry_path, bench_root=bench_root,
                                stub_verdict=args.stub_verdict)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
