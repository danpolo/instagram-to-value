#!/usr/bin/env python3
"""Send-only Telegram helper for worker.py's progress notifications. Does not
poll -- telegram_bot.py owns the long-poll loop; this just posts one message
via a fresh Bot instance per call. See PLAN.md sec 7 Phase 3 ("Progress
notifications on stage transitions").

Usage (as a library):
    from telegram_notify import send
    send("fetching DW1O6ZBEfDa")

CLI (for manual testing):
    python3 scripts/telegram_notify.py "some message" [--chat-id ID]
"""
import argparse
import asyncio
import sys
from pathlib import Path

from telegram import Bot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lib import DEFAULT_SECRETS, load_secret


async def _send_async(text, token, chat_id):
    async with Bot(token) as bot:
        await bot.send_message(chat_id=chat_id, text=text)


def send(text, chat_id=None, secrets_path=DEFAULT_SECRETS):
    """Fire-and-wait send of one Telegram message from synchronous code.
    Fails loudly rather than silently dropping a progress notification -- a
    worker that can't notify should be noticed, not silently degraded."""
    token = load_secret("TELEGRAM_BOT_TOKEN", secrets_path)
    if not token:
        raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_BOT_TOKEN not set in {secrets_path}")
    if chat_id is None:
        chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", secrets_path)
        if not chat_id:
            raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {secrets_path}")
    asyncio.run(_send_async(text, token, chat_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--chat-id", default=None)
    ap.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    args = ap.parse_args()
    send(args.text, chat_id=args.chat_id, secrets_path=args.secrets)
    print(f"[telegram_notify] sent to {args.chat_id or '(default chat)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
