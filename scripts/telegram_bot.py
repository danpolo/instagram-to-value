#!/usr/bin/env python3
"""Phase 3 ingest bot: standalone Telegram bot, own token, allowlisted to one
chat ID. URL in -> jobs/queued/<shortcode>.json -> ack. This cannot be the
Claude Telegram bridge (PLAN.md sec 0 Correction 3: it binds to exactly one
session and isn't pointed at this project) -- it's a fully separate process
that owns nothing but the job queue.

Usage:
    python3 scripts/telegram_bot.py [--jobs-root DIR] [--secrets PATH]

Long-running; run under tmux/nohup (a systemd unit is Phase 5). Ctrl-C to stop.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import extract_shortcode
from jobs_lib import find_job, write_job
from secrets_lib import DEFAULT_SECRETS, load_secret

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telegram_bot")


def is_allowed_chat(chat_id, allowed_chat_id) -> bool:
    """Pure: PLAN.md sec 0b's access decision -- only one chat may ever queue
    jobs. Compared as strings since Telegram chat IDs and the secrets-file
    value may differ in type (int vs str)."""
    return str(chat_id) == str(allowed_chat_id)


def status_reply(state: str, shortcode: str) -> str:
    """Pure: the idempotent-resend reply (PLAN.md sec 6, 'a re-sent URL is a
    no-op')."""
    return f"Already tracked: {shortcode} is in {state}/."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = context.bot_data["allowed_chat_id"]
    jobs_root = context.bot_data["jobs_root"]
    chat_id = update.effective_chat.id

    if not is_allowed_chat(chat_id, allowed_chat_id):
        logger.warning("rejected message from unauthorized chat_id=%s", chat_id)
        return

    text = update.message.text or ""
    try:
        shortcode = extract_shortcode(text)
    except SystemExit:
        await update.message.reply_text("Send an Instagram post/reel URL.")
        return

    state, _ = find_job(shortcode, jobs_root)
    if state is not None:
        await update.message.reply_text(status_reply(state, shortcode))
        return

    write_job(
        shortcode, jobs_root, "queued",
        url=text.strip(), chat_id=chat_id,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    await update.message.reply_text(f"Queued {shortcode}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()

    token = load_secret("TELEGRAM_BOT_TOKEN", args.secrets)
    if not token:
        raise SystemExit(f"[telegram_bot] FATAL: TELEGRAM_BOT_TOKEN not set in {args.secrets}")
    allowed_chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", args.secrets)
    if not allowed_chat_id:
        raise SystemExit(f"[telegram_bot] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {args.secrets}")

    application = Application.builder().token(token).build()
    application.bot_data["allowed_chat_id"] = allowed_chat_id
    application.bot_data["jobs_root"] = args.jobs_root
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot starting, jobs_root=%s", args.jobs_root)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
