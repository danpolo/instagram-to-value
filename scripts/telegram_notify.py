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
    Fails loudly rather than silently dropping a progress notification --
    this function's own contract is unchanged. worker.py's safe_notify()
    wrapper is what degrades a failure to a warning for its own unattended
    drain (PLAN.md sec 9 open item #7); a caller that wants fail-loud still
    gets it by calling send() directly."""
    token = load_secret("TELEGRAM_BOT_TOKEN", secrets_path)
    if not token:
        raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_BOT_TOKEN not set in {secrets_path}")
    if chat_id is None:
        chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", secrets_path)
        if not chat_id:
            raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {secrets_path}")
    asyncio.run(_send_async(text, token, chat_id))


async def _send_with_buttons_async(text, token, chat_id, buttons):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in buttons])
    async with Bot(token) as bot:
        message = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return message.message_id


def send_with_buttons(text, buttons, chat_id=None, secrets_path=DEFAULT_SECRETS):
    """Like send(), but attaches an inline keyboard and returns the sent
    message's id (design spec's proposal.json "message_id" field). buttons:
    list of rows, each a list of (label, callback_data) tuples. Callback taps
    are received by whichever process is polling with this same bot token --
    telegram_bot.py's Application, not this send-only helper."""
    token = load_secret("TELEGRAM_BOT_TOKEN", secrets_path)
    if not token:
        raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_BOT_TOKEN not set in {secrets_path}")
    if chat_id is None:
        chat_id = load_secret("TELEGRAM_ALLOWED_CHAT_ID", secrets_path)
        if not chat_id:
            raise SystemExit(f"[telegram_notify] FATAL: TELEGRAM_ALLOWED_CHAT_ID not set in {secrets_path}")
    return asyncio.run(_send_with_buttons_async(text, token, chat_id, buttons))


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
