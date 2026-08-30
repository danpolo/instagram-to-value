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
                       available_ram_gb_fn=lambda: 10.0, process_job_fn=lambda *a: calls.append(a))
    assert len(calls) == 1
    assert calls[0][0] == "ABC123"
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
