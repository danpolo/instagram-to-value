# Phase 5a: Worker Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This plan carries its own approval.** Per
> `docs/superpowers/handoffs/SESSION-1-worker-durability.md`, plan and
> implementation happen in one session with no review checkpoint — every
> decision below is already settled by `PLAN.md`.

**Goal:** Make `worker.py` safe to run unattended for the 23-hour, 49-job
backfill drain: a Telegram hiccup no longer kills the run, a mid-flight
crash no longer strands a job forever, and a low-RAM moment defers a job
instead of risking an OOM/livelock. Then launch the drain.

**Architecture:** Three independent, additive changes to `scripts/worker.py`,
all landing together because #7 and #8 share a root cause (PLAN.md §9 open
item #9). No new files, no schema changes — `jobs/{queued,running,done,failed}/`
and the job JSON shape are untouched.

**Tech Stack:** Python 3, pytest (`tmp_path`, `capsys` fixtures — matches
`test_ingest.py`'s existing style, no mocking library, no monkeypatch).

**Spec:** `PLAN.md` §7 Phase 5 "Worker durability" block, §9 open items #7/#8/#9,
§8 risk #1, §6 (idempotency). No separate design-spec doc for this phase — the
handoff file above **is** the spec; this plan operationalizes it.

## Global Constraints

- RAM gate threshold: **7.5 GB available** (PLAN.md §9 fact: "fp16
  Qwen3-ASR-1.7B-hf peaks at 6.9 GB RSS and needs ~7.5 GB available"). Reuse
  `extract.py`'s existing `MIN_RAM_GB_FOR_ASR = 7.5` constant and its
  `get_available_ram_gb()` / `check_ram_guard()` — do not reimplement.
- `telegram_notify.send()` keeps its fail-loud contract unchanged. Only
  `worker.py`'s use of it gets wrapped.
- Do **not** turn on `while True` (`worker.py:155`) or touch
  `scripts/telegram_bot.py` — out of scope for this session.
- All 68 existing tests stay green throughout.

---

### Task 1: RAM gate — defer a job instead of launching it into an OOM

**Files:**
- Modify: `scripts/worker.py` (import line ~24-26, `drain_once` at line 132)
- Test: `scripts/test_ingest.py`

**Interfaces:**
- Consumes: `extract.get_available_ram_gb() -> float` (GB, reads
  `/proc/meminfo` `MemAvailable`), `extract.check_ram_guard(available_gb,
  minimum_gb=MIN_RAM_GB_FOR_ASR) -> bool`, `extract.MIN_RAM_GB_FOR_ASR = 7.5`
  (all already defined, `scripts/extract.py:32-50`).
- Produces: `worker.drain_once(jobs_root, media_root, extracted_root,
  pages_root, available_ram_gb_fn=get_available_ram_gb,
  process_job_fn=process_job)` — two new optional kwargs, both defaulted so
  `main()` and Phase 3's existing call sites need no changes.

- [x] **Step 1: Write the failing tests**

```python
def test_drain_once_defers_job_when_ram_low(tmp_path, capsys):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    calls = []
    worker.drain_once(tmp_path, tmp_path / "media", tmp_path / "extracted", tmp_path / "pages",
                       available_ram_gb_fn=lambda: 2.0, process_job_fn=lambda *a: calls.append(a))
    assert calls == []
    state, _ = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "queued"
    assert "deferring" in capsys.readouterr().err


def test_drain_once_proceeds_job_when_ram_ok(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    calls = []
    worker.drain_once(tmp_path, tmp_path / "media", tmp_path / "extracted", tmp_path / "pages",
                       available_ram_gb_fn=lambda: 10.0, process_job_fn=lambda *a: calls.append(a))
    assert len(calls) == 1
    assert calls[0][0] == "ABC123"
    state, _ = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "running"
```

Add both to `scripts/test_ingest.py`, after `test_list_queued_oldest_first`.

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest scripts/test_ingest.py -k drain_once -v`
Expected: FAIL — `drain_once() got an unexpected keyword argument 'available_ram_gb_fn'`

- [x] **Step 3: Implement**

In `scripts/worker.py`, add to the import block (after the existing
`from discover_account import enroll_if_new` line):

```python
from extract import get_available_ram_gb, check_ram_guard, MIN_RAM_GB_FOR_ASR
```

Replace `drain_once` (currently `worker.py:132-142`) with:

```python
def drain_once(jobs_root, media_root, extracted_root, pages_root,
                available_ram_gb_fn=get_available_ram_gb, process_job_fn=process_job):
    for shortcode in list_queued(jobs_root):
        job_path = Path(jobs_root) / "queued" / f"{shortcode}.json"
        try:
            job = json.loads(job_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[worker] WARNING: unreadable job {job_path}: {e}", file=sys.stderr)
            continue
        available_gb = available_ram_gb_fn()
        if not check_ram_guard(available_gb):
            print(f"[worker] deferring {shortcode}: only {available_gb:.1f}GB available, "
                  f"need >={MIN_RAM_GB_FOR_ASR}GB (PLAN.md sec 8 risk 1) -- will retry next drain",
                  file=sys.stderr)
            continue
        move_job(shortcode, jobs_root, "queued", "running")
        process_job_fn(shortcode, job.get("url") or f"https://www.instagram.com/p/{shortcode}/",
                        job.get("chat_id"), jobs_root, media_root, extracted_root, pages_root)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest scripts/test_ingest.py -k drain_once -v`
Expected: PASS (2 tests)

- [x] **Step 5: Commit**

```bash
git add scripts/worker.py scripts/test_ingest.py
git commit -m "worker: gate job launch on available RAM (PLAN.md sec 8 risk 1)"
```

---

### Task 2: Requeue orphaned jobs on startup

**Files:**
- Modify: `scripts/worker.py` (`main()` at line 145, add new function above `drain_once`)
- Test: `scripts/test_ingest.py`

**Interfaces:**
- Consumes: `jobs_lib.move_job(shortcode, jobs_root, from_state, to_state,
  **extra_fields) -> Path` (raises `FileNotFoundError` if source missing,
  raises `json.JSONDecodeError`/`OSError` if the source file is unreadable —
  already the case per `jobs_lib.py:38-52`).
- Produces: `worker.requeue_orphans(jobs_root)` — no return value, called
  once from `main()` before the drain loop.

- [x] **Step 1: Write the failing tests**

```python
def test_requeue_orphans_moves_running_to_queued(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "running", url="https://x/p/ABC123/")
    worker.requeue_orphans(tmp_path)
    state, _ = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "queued"


def test_requeue_orphans_noop_when_running_missing(tmp_path):
    worker.requeue_orphans(tmp_path)  # no running/ dir at all -- must not raise
    assert jobs_lib.list_queued(tmp_path) == []


def test_requeue_orphans_skips_unreadable_file(tmp_path, capsys):
    running_dir = tmp_path / "running"
    running_dir.mkdir()
    (running_dir / "BROKEN.json").write_text("{not valid json")
    worker.requeue_orphans(tmp_path)
    assert (running_dir / "BROKEN.json").exists()  # left alone, not silently dropped
    assert "WARNING" in capsys.readouterr().err
```

Add all three to `scripts/test_ingest.py`, after Task 1's tests.

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest scripts/test_ingest.py -k requeue_orphans -v`
Expected: FAIL — `module 'worker' has no attribute 'requeue_orphans'`

- [x] **Step 3: Implement**

Add above `drain_once` in `scripts/worker.py`:

```python
def requeue_orphans(jobs_root):
    """Scan jobs/running/ and move anything found back to queued/ -- a job
    sitting there means the previous run died mid-flight (OOM, reboot, or a
    notify error before bug #7's fix landed -- PLAN.md sec 9 open item #8).
    Jobs are idempotent by design (PLAN.md sec 6), so a replay is safe.
    Called once at worker startup, before the drain loop."""
    jobs_root = Path(jobs_root)
    running_dir = jobs_root / "running"
    if not running_dir.exists():
        return
    for job_path in sorted(running_dir.glob("*.json")):
        shortcode = job_path.stem
        try:
            move_job(shortcode, jobs_root, "running", "queued")
            print(f"[worker] requeued orphaned job {shortcode}", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[worker] WARNING: could not requeue orphaned job {job_path}: {e}", file=sys.stderr)
```

In `main()` (`worker.py:145-159`), add the call right after `args = ap.parse_args()`
and before the `while True:` loop:

```python
    requeue_orphans(args.jobs_root)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest scripts/test_ingest.py -k requeue_orphans -v`
Expected: PASS (3 tests)

- [x] **Step 5: Commit**

```bash
git add scripts/worker.py scripts/test_ingest.py
git commit -m "worker: requeue orphaned jobs.running/ on startup (PLAN.md open item #8)"
```

---

### Task 3: Wrap `note()` so a Telegram failure degrades to a warning

**Files:**
- Modify: `scripts/worker.py` (`process_job` at line 85-87)
- Modify: `scripts/telegram_notify.py` (docstring of `send()`, lines 30-33 — comment only, no behavior change)
- Test: `scripts/test_ingest.py`

**Interfaces:**
- Consumes: `telegram_notify.send(text, chat_id=None, secrets_path=DEFAULT_SECRETS)`
  (imported as `notify` in `worker.py:26`) — contract unchanged, still raises
  `SystemExit` on missing secrets and propagates any network/API exception.
- Produces: `worker.safe_notify(text, chat_id, notify_fn=notify)` — swallows
  any exception from `notify_fn`, logs a warning, never raises. `process_job`'s
  `note()` closure now delegates to it.

- [x] **Step 1: Write the failing tests**

```python
def test_safe_notify_calls_through_on_success():
    calls = []
    def fake(text, chat_id=None):
        calls.append((text, chat_id))
    worker.safe_notify("hi", "123", notify_fn=fake)
    assert calls == [("hi", "123")]


def test_safe_notify_swallows_raising_notifier(capsys):
    def boom(text, chat_id=None):
        raise RuntimeError("network down")
    worker.safe_notify("hello", "123", notify_fn=boom)
    assert "WARNING" in capsys.readouterr().err
```

Add both to `scripts/test_ingest.py`, after Task 2's tests.

- [x] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest scripts/test_ingest.py -k safe_notify -v`
Expected: FAIL — `module 'worker' has no attribute 'safe_notify'`

- [x] **Step 3: Implement**

Add above `process_job` in `scripts/worker.py`:

```python
def safe_notify(text, chat_id, notify_fn=notify):
    """Send a progress notification, degrading to a logged warning instead of
    raising (PLAN.md sec 9 open item #7). telegram_notify.send() itself keeps
    its fail-loud contract -- that split is deliberate: send() stays honest
    for anyone calling it directly, this wrapper is what the *worker*
    specifically needs, because a transient Telegram blip must not kill a
    23-hour unattended drain. Open item #9: the call before fetch is exactly
    what orphaned jobs before this fix + the requeue-on-startup fix landed."""
    try:
        notify_fn(text, chat_id=chat_id)
    except Exception as e:
        print(f"[worker] WARNING: notify failed, continuing: {e}", file=sys.stderr)
```

Change `process_job`'s `note()` (currently `worker.py:86-87`) from:

```python
    def note(text):
        notify(text, chat_id=chat_id)
```

to:

```python
    def note(text):
        safe_notify(text, chat_id)
```

In `scripts/telegram_notify.py`, update `send()`'s docstring (lines 30-33) to
record the split with the worker:

```python
def send(text, chat_id=None, secrets_path=DEFAULT_SECRETS):
    """Fire-and-wait send of one Telegram message from synchronous code.
    Fails loudly rather than silently dropping a progress notification --
    this function's own contract is unchanged. worker.py's safe_notify()
    wrapper is what degrades a failure to a warning for its own unattended
    drain (PLAN.md sec 9 open item #7); a caller that wants fail-loud still
    gets it by calling send() directly."""
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest scripts/test_ingest.py -k safe_notify -v`
Expected: PASS (2 tests)

- [x] **Step 5: Full suite + commit**

Run: `python3 -m pytest scripts/ -v`
Expected: 75 passed (68 + 2 + 3 + 2)

```bash
git add scripts/worker.py scripts/telegram_notify.py scripts/test_ingest.py
git commit -m "worker: swallow note() failures instead of killing the job (PLAN.md open item #7)"
```

---

### Task 4: PLAN.md cleanup — close stale open items #2, #4, #5

**Files:**
- Modify: `PLAN.md` (§9 open items table, lines ~696-703)

No tests — documentation only.

- [x] **Step 1: Close item #2** (Groq API key). Change the row to record it
  resolved, citing the two real reconciliation runs already in §7: Phase 2's
  87.1% (English reel `DW1O6ZBEfDa`) and Phase 3's 93.5%
  (`DbsDXkgJ_FB`, §9 facts).

- [x] **Step 2: Close item #5** (unused instaloader venv). Delete it, then
  mark resolved:

```bash
rm -rf ~/.local/venvs/instaloader
```

- [x] **Step 3: Verify item #4** (`discover.sh`). Read `discover.sh` and
  confirm it is still the search-index anonymous-fallback route described in
  the open-items table (queries `site:instagram.com`/`lite.duckduckgo.com`,
  no auth). If the description still matches, leave the row's text as-is —
  it already reads correctly, this is a verify-only step per the handoff.

- [x] **Step 4: Commit**

```bash
git add PLAN.md
git commit -m "docs: close stale open items #2 and #5, verify #4 still accurate"
```

---

### Task 5: Live durability checks

No code changes — this is the handoff's required live verification before
launching the drain. All three must pass.

- [x] **Step 1: Orphan recovery under a real kill.** Queue a throwaway test
  job, start `python3 scripts/worker.py --once`, kill it (`SIGKILL`) after it
  has moved the job to `running/` but before it finishes. Restart
  `--once`. Confirm the log shows `requeued orphaned job <shortcode>` and the
  job reaches `done/` or `failed/` (not stuck in `running/`).

- [x] **Step 2: Telegram happy path still works.** Confirm a real progress
  message arrives on a normal job run — the `safe_notify` wrapper must not
  have silenced anything on success.

- [x] **Step 3: RAM gate reads a plausible number.** Run
  `python3 -c "import sys; sys.path.insert(0,'scripts'); from extract import get_available_ram_gb; print(get_available_ram_gb())"`
  and sanity-check the value against `free -h`.

---

### Task 6: Launch the 49-job backfill drain

**Only after all three Task 5 checks pass.**

- [x] **Step 1: Start detached, logging outside the repo.**

```bash
nohup python3 scripts/worker.py --once > /tmp/worker_drain.log 2>&1 &
disown
```

Record the PID (`echo $!` or `pgrep -f "worker.py --once"`).

- [x] **Step 2: Watch the first 2-3 jobs complete.** Tail
  `/tmp/worker_drain.log` and confirm at least 2-3 jobs reach `done/` (or a
  legitimate `failed/` with a real error, not a crash) before stepping away.

- [x] **Step 3: Report to Dan** — test count before/after, which of the three
  Task 5 checks passed, the drain's PID and logfile path, how many jobs
  completed while watching, the estimated finish time (~28 min/job × jobs
  remaining), and confirmation that Session 2 can start immediately.

**What actually happened, deviating from the literal steps above:**
- Launched with the harness's own `Bash(run_in_background: true)` instead of
  a manual `nohup ... & disown`, after the first `nohup` launch (with its log
  in plain `/tmp/`) died along with the whole real host reboot at
  `2026-08-30 22:03:34` — the box rebooted mid-drain, not a sandbox/tool
  artifact. That first run still got real value: it drained 5 real jobs
  (`DcNELNupJRh`, `DcOzCuYJXX4`, `DcFeZa0JgXi`, plus 2 more) and, when killed
  mid-ASR by a deliberate `SIGKILL` test *before* the reboot, proved
  `requeue_orphans()` live for the first time.
- The reboot itself produced a **second, unplanned** proof of the same fix:
  it orphaned `Dcez-FApTe7` in `jobs/running/`, and the relaunch requeued it
  automatically with no code change needed.
- Process-tree check at close (`ps -o pid,ppid,pgid,sid,tty`) confirmed the
  relaunched drain is its own session leader with no controlling tty (`SID`
  distinct from the interactive shell's, `TT ?`) — a POSIX session boundary
  a terminal/session-close `SIGHUP` does not cross. The residual risk is
  only a real host reboot/crash, which is recoverable exactly like the one
  above: `requeue_orphans()` + a fresh `--once` picks it back up.

---

## Self-review

**Spec coverage:** RAM gate (Task 1) ✓, orphan requeue (Task 2) ✓, `note()`
wrap + `send()` docstring split (Task 3) ✓, stale item #2/#4/#5 cleanup
(Task 4) ✓, three live checks (Task 5) ✓, drain launch + report (Task 6) ✓.
`while True` and `telegram_bot.py` correctly left untouched (out of scope).

**Placeholder scan:** none found — every step has literal code or literal
shell commands.

**Type consistency:** `drain_once`'s new kwargs (`available_ram_gb_fn`,
`process_job_fn`) match their use in Task 1's tests exactly.
`requeue_orphans(jobs_root)` and `safe_notify(text, chat_id, notify_fn=notify)`
signatures match between definition (Tasks 2/3 Step 3) and test calls (Tasks
2/3 Step 1).
