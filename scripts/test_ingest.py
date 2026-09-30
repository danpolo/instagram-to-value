"""Pure-logic unit tests for the Phase 3 ingest layer. Run with:
    python3 -m pytest scripts/test_ingest.py -v
No live Telegram/network calls -- those live inside functions these tests
don't invoke (matches test_extract.py's convention)."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs_lib
import discover_account
import telegram_bot
import worker


def test_find_job_untracked_returns_none(tmp_path):
    assert jobs_lib.find_job("ABC123", tmp_path) == (None, None)


def test_write_job_then_find_job(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    state, path = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "queued"
    data = json.loads(path.read_text())
    assert data == {"shortcode": "ABC123", "url": "https://x/p/ABC123/"}


def test_write_job_raises_if_already_exists(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued")
    try:
        jobs_lib.write_job("ABC123", tmp_path, "queued")
        assert False, "expected FileExistsError"
    except FileExistsError:
        pass


def test_move_job_merges_fields_and_removes_source(tmp_path):
    jobs_lib.write_job("ABC123", tmp_path, "queued", url="https://x/p/ABC123/")
    dst = jobs_lib.move_job("ABC123", tmp_path, "queued", "running")
    assert not (tmp_path / "queued" / "ABC123.json").exists()
    assert dst == tmp_path / "running" / "ABC123.json"
    data = json.loads(dst.read_text())
    assert data["url"] == "https://x/p/ABC123/"

    dst2 = jobs_lib.move_job("ABC123", tmp_path, "running", "failed", error="boom")
    assert not dst.exists()
    data2 = json.loads(dst2.read_text())
    assert data2["error"] == "boom"
    assert data2["url"] == "https://x/p/ABC123/"


def test_move_job_missing_source_raises(tmp_path):
    try:
        jobs_lib.move_job("NOPE", tmp_path, "queued", "running")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_list_queued_empty_when_dir_missing(tmp_path):
    assert jobs_lib.list_queued(tmp_path) == []


def test_list_queued_oldest_first(tmp_path):
    import time
    jobs_lib.write_job("FIRST", tmp_path, "queued")
    time.sleep(0.01)
    jobs_lib.write_job("SECOND", tmp_path, "queued")
    assert jobs_lib.list_queued(tmp_path) == ["FIRST", "SECOND"]


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
                       available_ram_gb_fn=lambda: 10.0, process_job_fn=lambda *a, **kw: calls.append((a, kw)))
    assert len(calls) == 1
    assert calls[0][0][0] == "ABC123"
    state, _ = jobs_lib.find_job("ABC123", tmp_path)
    assert state == "running"


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


# Trimmed from a real `gallery-dl https://www.instagram.com/networkchuck/posts/
# --post-range 1-5 -j` run against @networkchuck during Phase 3 design
# (2026-08-30) -- shortcodes/dates/owner_id are the actual values returned.
SAMPLE_GALLERY_DL_JSON = json.dumps([
    [2, {"post_shortcode": "DcmLKiSF75h", "post_date": "2026-08-28 20:02:57",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "ytdl:https://www.instagram.com/p/DcmLKiSF75h/1.mp4"],
    [2, {"post_shortcode": "DchEg-1PNFF", "post_date": "2026-08-26 20:28:27",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "https://instagram.fsdv5-1.fna.fbcdn.net/v/t51.82787-15/example.jpg"],
    [2, {"post_shortcode": "Dce_LrgCrFA", "post_date": "2026-08-26 01:03:30",
         "owner_id": "4440726664", "username": "networkchuck"}],
    [3, "ytdl:https://www.instagram.com/p/Dce_LrgCrFA/1.mp4"],
])


def test_should_stop_backfill_posts_cap():
    now = datetime(2026, 9, 1)
    assert discover_account.should_stop_backfill(2, now, now, posts_cap=2, days_cap=90) is True
    assert discover_account.should_stop_backfill(1, now, now, posts_cap=2, days_cap=90) is False


def test_should_stop_backfill_days_cap():
    first_seen = datetime(2026, 9, 1)
    old_post = datetime(2026, 5, 1)  # ~123 days before first_seen
    assert discover_account.should_stop_backfill(0, old_post, first_seen, posts_cap=50, days_cap=90) is True
    recent_post = datetime(2026, 8, 20)
    assert discover_account.should_stop_backfill(0, recent_post, first_seen, posts_cap=50, days_cap=90) is False


def test_parse_gallery_dl_posts_skips_non_post_entries():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    assert [p["shortcode"] for p in posts] == ["DcmLKiSF75h", "DchEg-1PNFF", "Dce_LrgCrFA"]
    assert posts[0]["username"] == "networkchuck"
    assert posts[0]["owner_id"] == "4440726664"


def test_select_backfill_shortcodes_respects_posts_cap():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    first_seen = datetime(2026, 9, 1)
    kept = discover_account.select_backfill_shortcodes(posts, first_seen, posts_cap=2, days_cap=90)
    assert [p["shortcode"] for p in kept] == ["DcmLKiSF75h", "DchEg-1PNFF"]


def test_select_backfill_shortcodes_respects_days_cap():
    posts = discover_account.parse_gallery_dl_posts(SAMPLE_GALLERY_DL_JSON)
    first_seen = datetime(2026, 9, 1)  # post ages (timedelta.days, truncated): 3, 5, 5
    kept = discover_account.select_backfill_shortcodes(posts, first_seen, posts_cap=50, days_cap=4)
    assert [p["shortcode"] for p in kept] == ["DcmLKiSF75h"]


def test_build_page_record_shape():
    record = discover_account.build_page_record("networkchuck", "2026-08-30T00:00:00+00:00")
    assert record == {
        "username": "networkchuck",
        "first_seen": "2026-08-30T00:00:00+00:00",
        "backfill": {"posts_cap": 50, "days_cap": 90, "done": False},
        "last_checked": None,
        "newest_known_shortcode": None,
    }


def test_enroll_if_new_noop_when_already_watched(tmp_path):
    pages_root = tmp_path / "pages"
    pages_root.mkdir()
    (pages_root / "networkchuck.json").write_text("{}")
    assert discover_account.enroll_if_new("networkchuck", pages_root=pages_root,
                                           jobs_root=tmp_path / "jobs") is None


def test_is_allowed_chat_matches():
    assert telegram_bot.is_allowed_chat(12345, "12345") is True
    assert telegram_bot.is_allowed_chat("12345", "12345") is True


def test_is_allowed_chat_rejects_other():
    assert telegram_bot.is_allowed_chat(999, "12345") is False


def test_status_reply_text():
    assert telegram_bot.status_reply("done", "ABC123") == "Already tracked: ABC123 is in done/."


def test_resolve_creator_username_from_info_json(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    (media_dir / "ABC123.info.json").write_text(json.dumps({"uploader_id": "networkchuck"}))
    assert worker.resolve_creator_username(media_dir, "ABC123") == "networkchuck"


def test_resolve_creator_username_prefers_channel_over_numeric_uploader_id(tmp_path):
    # Real shape found live 2026-08-30 (post DcmLKiSF75h): uploader_id is the
    # numeric account ID, uploader is a display-name-cased string, channel is
    # the actual lowercase handle matching the image track's "username".
    # Picking uploader_id first enrolled a bogus duplicate account.
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    (media_dir / "ABC123.info.json").write_text(json.dumps({
        "uploader": "NetworkChuck", "uploader_id": "4440726664", "channel": "networkchuck",
    }))
    assert worker.resolve_creator_username(media_dir, "ABC123") == "networkchuck"


def test_resolve_creator_username_from_gallery_dl_sidecar(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    (media_dir / "ABC123_01.json").write_text(json.dumps({"username": "ynetgram", "owner_id": "999"}))
    assert worker.resolve_creator_username(media_dir, "ABC123") == "ynetgram"


def test_resolve_creator_username_none_when_absent(tmp_path):
    media_dir = tmp_path / "ABC123"
    media_dir.mkdir()
    assert worker.resolve_creator_username(media_dir, "ABC123") is None


def test_build_proposal_buttons_none_when_nothing_pending():
    actions = [{"status": "installed"}, {"status": "skipped"}]
    assert worker.build_proposal_buttons(actions) is None


def test_build_proposal_buttons_present_when_pending_exists():
    actions = [{"status": "pending"}]
    buttons = worker.build_proposal_buttons(actions)
    assert buttons is not None
    assert len(buttons) == 2


# --- /pending must deliver tappable proposals, not a plain list ------------
# Session 3 pilot found backfill-origin proposals unreachable: they are never
# pushed with buttons (backfill only emits a digest), and /pending rendered a
# text list, so 36 staged proposals had no route to approval.

class _FakeMessage:
    def __init__(self, recorder, text=None, reply_to=None):
        self._recorder = recorder
        self.message_id = 1000 + len(recorder)
        self.text = text
        self.reply_to_message = reply_to

    async def reply_text(self, text, reply_markup=None):
        sent = _FakeMessage(self._recorder)
        self._recorder.append({"text": text, "reply_markup": reply_markup,
                               "message_id": sent.message_id})
        return sent


class _FakeUpdate:
    def __init__(self, recorder, chat_id=42, text=None, reply_to=None):
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.message = _FakeMessage(recorder, text=text, reply_to=reply_to)


class _FakeContext:
    def __init__(self, staging_root, args=None, chat_id=42, jobs_root=None):
        self.args = args
        self.bot_data = {"staging_root": staging_root, "allowed_chat_id": chat_id,
                         "jobs_root": jobs_root or staging_root}
        self.user_data = {}


def _stage(tmp_path, shortcode, summary="a summary"):
    import staging_lib
    staging_lib.write_proposal(shortcode, tmp_path, {
        "shortcode": shortcode, "origin": "backfill", "summary": summary,
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "pending", "message_id": None,
        "resolver": {"status": "not_found", "tool_name": None, "url": None,
                     "tier": None, "evidence": []},
        "actions": [{"id": "a1", "type": "reference", "risk": "inert",
                     "confidence": 0.9, "status": "pending",
                     "payload": {"title": "T", "content": "C"},
                     "decided_at": None, "result": None}],
        "agent_calls": 1,
    })


def test_cmd_pending_sends_one_card_per_action_not_per_post(tmp_path):
    import asyncio
    _stage(tmp_path, "AAA111")
    _stage(tmp_path, "BBB222")
    sent = []
    asyncio.run(telegram_bot.cmd_pending(_FakeUpdate(sent), _FakeContext(tmp_path)))
    # header + one message per pending action, across posts
    assert len(sent) == 3
    assert sent[0]["reply_markup"] is None          # header carries no buttons
    cards = sent[1:]
    buttons = [b for row in cards[0]["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["✅ Approve", "❌ Skip"]
    # the post stays linked internally (callback data), not in the text
    assert [b.callback_data for b in buttons] == ["act_ok:AAA111:a1", "act_skip:AAA111:a1"]
    assert "AAA111" not in cards[0]["text"]
    assert "Why: a summary" in cards[0]["text"]


def test_cmd_pending_batches_actions(tmp_path):
    import asyncio
    for i in range(telegram_bot.DEFAULT_PENDING_BATCH + 2):
        _stage(tmp_path, f"SC{i:04d}")
    sent = []
    asyncio.run(telegram_bot.cmd_pending(_FakeUpdate(sent), _FakeContext(tmp_path)))
    total = telegram_bot.DEFAULT_PENDING_BATCH + 2
    assert len(sent) == 1 + telegram_bot.DEFAULT_PENDING_BATCH
    assert f"/pending {total}" in sent[0]["text"]


class _FakeQuery:
    def __init__(self, recorder, data, chat_id=42):
        self.data = data
        self.message = _FakeMessage(recorder)
        self.message.chat_id = chat_id
        self.markup_cleared = False

    async def answer(self, *a, **kw):
        pass

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


def test_skip_callback_marks_only_that_action(tmp_path):
    import asyncio
    import staging_lib
    _stage(tmp_path, "AAA111")
    sent = []
    query = _FakeQuery(sent, "act_skip:AAA111:a1")
    update = type("U", (), {"callback_query": query})()
    asyncio.run(telegram_bot.handle_callback(update, _FakeContext(tmp_path)))
    assert staging_lib.read_proposal("AAA111", tmp_path)["actions"][0]["status"] == "skipped"
    assert query.markup_cleared
    assert sent and "Skipped" in sent[0]["text"]


def test_callback_on_already_decided_action_does_nothing(tmp_path):
    import asyncio
    import staging_lib
    _stage(tmp_path, "AAA111")
    staging_lib.update_action_status("AAA111", tmp_path, "a1", "installed", {"ok": True})
    sent = []
    query = _FakeQuery(sent, "act_ok:AAA111:a1")
    update = type("U", (), {"callback_query": query})()
    asyncio.run(telegram_bot.handle_callback(update, _FakeContext(tmp_path)))
    assert staging_lib.read_proposal("AAA111", tmp_path)["actions"][0]["status"] == "installed"
    assert "already" in sent[0]["text"]


def test_list_pending_preserves_creation_order_after_message_id_update(tmp_path):
    import asyncio
    import staging_lib
    _stage(tmp_path, "FIRST01")
    _stage(tmp_path, "SECOND2")
    proposal = staging_lib.read_proposal("FIRST01", tmp_path)
    proposal["created_at"] = "2026-01-01T00:00:00+00:00"
    staging_lib.write_proposal("FIRST01", tmp_path, proposal)
    proposal = staging_lib.read_proposal("SECOND2", tmp_path)
    proposal["created_at"] = "2026-01-01T00:01:00+00:00"
    staging_lib.write_proposal("SECOND2", tmp_path, proposal)

    sent = []
    asyncio.run(telegram_bot.cmd_pending(_FakeUpdate(sent), _FakeContext(tmp_path, args=["1"])))

    assert staging_lib.list_pending(tmp_path) == ["FIRST01", "SECOND2"]


def test_cmd_pending_explicit_count_overrides_batch(tmp_path):
    import asyncio
    for i in range(7):
        _stage(tmp_path, f"SC{i:04d}")
    sent = []
    asyncio.run(telegram_bot.cmd_pending(_FakeUpdate(sent), _FakeContext(tmp_path, args=["7"])))
    assert len(sent) == 8  # header + all 7


def test_cmd_pending_rejects_foreign_chat(tmp_path):
    import asyncio
    _stage(tmp_path, "AAA111")
    sent = []
    update = _FakeUpdate(sent, chat_id=999)
    asyncio.run(telegram_bot.cmd_pending(update, _FakeContext(tmp_path, chat_id=42)))
    assert sent == []


# --- Two ❌ Discard taps in a row must not cross their reason prompts ------
# Found live in the Session 4 pending sweep: both prompts said only "why?" and
# shared ONE user_data slot, so the second tap overwrote the first shortcode.
# The first answer was filed against the SECOND proposal, and the second answer
# found no slot at all and fell through to the URL parser as a new post.

class _FakeCallbackQuery:
    def __init__(self, recorder, data, chat_id=42):
        self.data = data
        self.message = _FakeMessage(recorder)
        self.message.chat_id = chat_id
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


class _FakeCallbackUpdate:
    def __init__(self, recorder, data, chat_id=42):
        self.callback_query = _FakeCallbackQuery(recorder, data, chat_id)


def _reply_to(message_id):
    return type("M", (), {"message_id": message_id})()


def _discard_log(staging_root, *shortcodes):
    path = Path(staging_root).parent / "logs" / "discarded.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r["shortcode"] in shortcodes]


def test_two_discards_route_each_reason_to_its_own_proposal(tmp_path):
    import asyncio
    _stage(tmp_path, "DISCA01")
    _stage(tmp_path, "DISCB02")
    sent = []
    ctx = _FakeContext(tmp_path)

    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCA01"), ctx))
    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCB02"), ctx))

    # each prompt must name its own proposal, or they are indistinguishable
    assert "DISCA01" in sent[0]["text"]
    assert "DISCB02" in sent[1]["text"]

    asyncio.run(telegram_bot.handle_message(
        _FakeUpdate(sent, text="not useful", reply_to=_reply_to(sent[0]["message_id"])), ctx))
    asyncio.run(telegram_bot.handle_message(
        _FakeUpdate(sent, text="already have it", reply_to=_reply_to(sent[1]["message_id"])), ctx))

    reasons = {r["shortcode"]: r["reason"] for r in _discard_log(tmp_path, "DISCA01", "DISCB02")}
    assert reasons == {"DISCA01": "not useful", "DISCB02": "already have it"}


def test_lone_discard_reason_needs_no_explicit_reply(tmp_path):
    import asyncio
    _stage(tmp_path, "DISCC03")
    sent = []
    ctx = _FakeContext(tmp_path)

    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCC03"), ctx))
    asyncio.run(telegram_bot.handle_message(_FakeUpdate(sent, text="low signal"), ctx))

    assert [(r["shortcode"], r["reason"]) for r in _discard_log(tmp_path, "DISCC03")] == \
        [("DISCC03", "low signal")]
    assert not ctx.user_data.get("awaiting_reason_for")


def test_ambiguous_reason_is_not_consumed_and_asks_which(tmp_path):
    import asyncio
    _stage(tmp_path, "DISCD04")
    _stage(tmp_path, "DISCE05")
    sent = []
    ctx = _FakeContext(tmp_path)

    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCD04"), ctx))
    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCE05"), ctx))
    asyncio.run(telegram_bot.handle_message(_FakeUpdate(sent, text="meh"), ctx))

    assert _discard_log(tmp_path, "DISCD04", "DISCE05") == [], \
        "an unaddressed reason must not be filed against an arbitrary proposal"
    assert "DISCD04" in sent[-1]["text"] and "DISCE05" in sent[-1]["text"]
    # both prompts stay open so each can still be answered
    assert len(ctx.user_data["awaiting_reason_for"]) == 2


def test_discard_prompt_forces_a_reply(tmp_path):
    import asyncio
    from telegram import ForceReply
    _stage(tmp_path, "DISCF06")
    sent = []
    asyncio.run(telegram_bot.handle_callback(
        _FakeCallbackUpdate(sent, "discard:DISCF06"), _FakeContext(tmp_path)))
    assert isinstance(sent[0]["reply_markup"], ForceReply), \
        "without ForceReply the client gives no way to address one prompt of several"


def test_new_link_still_queues_while_a_discard_prompt_is_open(tmp_path):
    import asyncio
    import jobs_lib
    _stage(tmp_path, "DISCG07")
    jobs_root = tmp_path / "jobs"
    sent = []
    ctx = _FakeContext(tmp_path, jobs_root=jobs_root)

    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCG07"), ctx))
    asyncio.run(telegram_bot.handle_message(
        _FakeUpdate(sent, text="https://www.instagram.com/reel/XYZ9876/"), ctx))

    # an unanswered prompt must not swallow the next link as its reason
    assert jobs_lib.find_job("XYZ9876", jobs_root)[0] == "queued"
    assert _discard_log(tmp_path, "DISCG07") == []
    assert list(ctx.user_data["awaiting_reason_for"].values()) == ["DISCG07"]


def test_double_tapping_one_discard_button_asks_once(tmp_path):
    import asyncio
    _stage(tmp_path, "DISCH08")
    sent = []
    ctx = _FakeContext(tmp_path)

    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCH08"), ctx))
    asyncio.run(telegram_bot.handle_callback(_FakeCallbackUpdate(sent, "discard:DISCH08"), ctx))

    assert len(ctx.user_data["awaiting_reason_for"]) == 1
    asyncio.run(telegram_bot.handle_message(_FakeUpdate(sent, text="duplicate"), ctx))
    assert [r["reason"] for r in _discard_log(tmp_path, "DISCH08")] == ["duplicate"]


def test_install_results_report_what_a_bare_name_resolved_to():
    """skill_install can now resolve a name against the skills.sh directory, so
    the ✅ line has to name the repo that actually ran -- "installed" alone
    hides which of 73 owners publishing an "impeccable" skill was picked."""
    import asyncio
    sent = []
    query = _FakeCallbackQuery(sent, "approve_all:AAA111")
    results = [({"id": "a1", "type": "skill_install"},
                {"ok": True, "note": "Impeccable → pbakaus/impeccable@impeccable (266695 installs, skills.sh)"})]
    asyncio.run(telegram_bot._report_install_results(query, "AAA111", results))
    assert "pbakaus/impeccable" in sent[-1]["text"]


def _stage_repo(root, shortcode, created, action_type="git_repo"):
    import staging_lib
    payload = ({"name": "taste-skill", "url": "https://github.com/leonxlnx/taste-skill"}
               if action_type == "git_repo" else {"name": "taste"})
    staging_lib.write_proposal(shortcode, root, {
        "shortcode": shortcode, "summary": "s", "status": "pending", "created_at": created,
        "actions": [{"id": "a1", "type": action_type, "risk": "exec", "confidence": 0.9, "status": "pending",
                     "payload": payload, "evidence": {"repo": "leonxlnx/taste-skill"},
                     "decided_at": None, "result": None}]})


def test_cmd_pending_merges_duplicates_into_one_card(tmp_path):
    import asyncio
    _stage_repo(tmp_path, "P1", "2026-01-01")
    _stage_repo(tmp_path, "P2", "2026-02-01", action_type="skill_install")
    sent = []
    asyncio.run(telegram_bot.cmd_pending(_FakeUpdate(sent), _FakeContext(tmp_path)))
    assert len(sent) == 2
    assert "suggested in 2 posts" in sent[1]["text"]
    data = [b.callback_data for row in sent[1]["reply_markup"].inline_keyboard for b in row]
    assert data == ["act_ok:P2:a1", "act_skip:P2:a1"]


def test_approve_installs_once_and_marks_duplicates(tmp_path, monkeypatch):
    import asyncio
    import staging_lib
    _stage_repo(tmp_path, "P1", "2026-01-01")
    _stage_repo(tmp_path, "P2", "2026-02-01", action_type="skill_install")
    installed = []

    def fake_install(action, shortcode, staging_root, resolution="install"):
        installed.append((shortcode, action["id"]))
        result = {"ok": True, "path": None, "error": None}
        staging_lib.update_action_status(shortcode, staging_root, action["id"], "installed", result)
        return result
    monkeypatch.setattr(telegram_bot.install_artifact, "install_action", fake_install)
    sent = []
    query = _FakeQuery(sent, "act_ok:P2:a1")
    asyncio.run(telegram_bot.handle_callback(type("U", (), {"callback_query": query})(), _FakeContext(tmp_path)))
    assert installed == [("P2", "a1")]
    dup = staging_lib.read_proposal("P1", tmp_path)["actions"][0]
    assert dup["status"] == "skipped" and "duplicate of P2/a1" in dup["result"]["output"]


def test_skip_skips_whole_duplicate_group(tmp_path):
    import asyncio
    import staging_lib
    _stage_repo(tmp_path, "P1", "2026-01-01")
    _stage_repo(tmp_path, "P2", "2026-02-01", action_type="skill_install")
    sent = []
    query = _FakeQuery(sent, "act_skip:P2:a1")
    asyncio.run(telegram_bot.handle_callback(type("U", (), {"callback_query": query})(), _FakeContext(tmp_path)))
    assert {staging_lib.read_proposal(sc, tmp_path)["actions"][0]["status"] for sc in ("P1", "P2")} == {"skipped"}
