#!/usr/bin/env python3
"""Phase 3 ingest bot: standalone Telegram bot, own token, allowlisted to one
chat ID. URL in -> jobs/queued/<shortcode>.json -> ack. This cannot be the
Claude Telegram bridge (PLAN.md sec 0 Correction 3: it binds to exactly one
session and isn't pointed at this project) -- it's a fully separate process
that owns nothing but the job queue.

Phase 4a adds: source="telegram" on write_job, an inline-button approval flow
for staged proposals (CallbackQueryHandler), /agent claude|codex to switch the
interpret backend, /pending to list staged proposals awaiting a decision, and
reject-reason capture after Discard (design spec's 'Reject' section).

Usage:
    python3 scripts/telegram_bot.py [--jobs-root DIR] [--staging-root DIR] [--secrets PATH]

Long-running; run under tmux/nohup (a systemd unit is Phase 5). Ctrl-C to stop.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                            ContextTypes, MessageHandler, filters)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import extract_shortcode
from jobs_lib import find_job, write_job
from secrets_lib import DEFAULT_SECRETS, load_secret
import agents_lib
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import install_artifact
import registry
import staging_lib

registry.load_all_handlers()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_ROOT = REPO_ROOT / "jobs"
DEFAULT_STAGING_ROOT = REPO_ROOT / "staging"

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
    staging_root = context.bot_data["staging_root"]
    chat_id = update.effective_chat.id

    if not is_allowed_chat(chat_id, allowed_chat_id):
        logger.warning("rejected message from unauthorized chat_id=%s", chat_id)
        return

    text = (update.message.text or "").strip()

    awaiting = context.user_data.get("awaiting_reason_for")
    if awaiting:
        del context.user_data["awaiting_reason_for"]
        staging_lib.record_reject_reason(awaiting, staging_root, text)
        await update.message.reply_text(f"Noted. {awaiting} discarded.")
        return

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
        url=text, chat_id=chat_id, source="telegram",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    await update.message.reply_text(f"Queued {shortcode}.")


async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update.effective_chat.id, context.bot_data["allowed_chat_id"]):
        return
    if not context.args:
        await update.message.reply_text(f"Current backend: {agents_lib.get_backend()}. Usage: /agent claude|codex")
        return
    try:
        agents_lib.set_backend(context.args[0])
        await update.message.reply_text(f"Backend set to {context.args[0]}.")
    except ValueError as e:
        await update.message.reply_text(str(e))


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update.effective_chat.id, context.bot_data["allowed_chat_id"]):
        return
    staging_root = context.bot_data["staging_root"]
    pending = staging_lib.list_pending(staging_root)
    if not pending:
        await update.message.reply_text("Nothing pending.")
        return
    lines = []
    for shortcode in pending:
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        lines.append(f"{shortcode}: {proposal.get('summary', '')}")
    await update.message.reply_text(f"{len(pending)} pending:\n" + "\n".join(lines))


def _proposal_buttons(shortcode):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ All", callback_data=f"approve_all:{shortcode}"),
         InlineKeyboardButton("☑️ Pick…", callback_data=f"pick:{shortcode}")],
        [InlineKeyboardButton("📄 Show full", callback_data=f"show:{shortcode}"),
         InlineKeyboardButton("❌ Discard", callback_data=f"discard:{shortcode}")],
    ])


async def _report_install_results(query, shortcode, results):
    lines = []
    for action, result in results:
        if result.get("collision"):
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("Replace", callback_data=f"replace:{shortcode}:{action['id']}"),
                InlineKeyboardButton("Keep both", callback_data=f"keep_both:{shortcode}:{action['id']}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{shortcode}:{action['id']}"),
            ]])
            await query.message.reply_text(f"⚠️ {result['path']} exists — [{action['id']}] {action['type']}",
                                              reply_markup=buttons)
            continue
        status = "✅ installed" if result["ok"] else f"❌ {result.get('error')}"
        lines.append(f"[{action['id']}] {action['type']}: {status}")
    if lines:
        await query.message.reply_text("\n".join(lines))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed_chat(query.message.chat_id, context.bot_data["allowed_chat_id"]):
        return
    await query.answer()
    staging_root = context.bot_data["staging_root"]
    action_str, _, rest = query.data.partition(":")

    if action_str == "approve_all":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        results = [(a, install_artifact.install_action(a, shortcode, staging_root))
                    for a in proposal["actions"] if a["status"] == "pending"]
        await _report_install_results(query, shortcode, results)
        return

    if action_str == "pick":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        buttons = [[InlineKeyboardButton(f"{a['id']} {a['type']}", callback_data=f"toggle:{shortcode}:{a['id']}")]
                    for a in proposal["actions"] if a["status"] == "pending"]
        buttons.append([InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_pick:{shortcode}")])
        context.user_data.setdefault("picked", {})[shortcode] = set()
        await query.message.reply_text("Tap actions to include, then Confirm:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action_str == "toggle":
        shortcode, action_id = rest.split(":", 1)
        picked = context.user_data.setdefault("picked", {}).setdefault(shortcode, set())
        picked.symmetric_difference_update({action_id})
        await query.answer(f"{action_id}: {'included' if action_id in picked else 'excluded'}")
        return

    if action_str == "confirm_pick":
        shortcode = rest
        picked = context.user_data.get("picked", {}).get(shortcode, set())
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        results = [(a, install_artifact.install_action(a, shortcode, staging_root,
                                                           resolution="install" if a["id"] in picked else "cancel"))
                    for a in proposal["actions"] if a["status"] == "pending"]
        await _report_install_results(query, shortcode, results)
        return

    if action_str == "show":
        shortcode = rest
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        parts = [f"[{a['id']}] {a['type']}:\n{registry.get_handler(a['type']).preview(a['payload'])}"
                  for a in proposal["actions"]]
        text = "\n\n".join(parts) or "No actions."
        for start in range(0, len(text), 3500):
            await query.message.reply_text(text[start:start + 3500])
        return

    if action_str == "discard":
        context.user_data["awaiting_reason_for"] = rest
        await query.message.reply_text("why?")
        return

    if action_str in ("replace", "keep_both", "cancel"):
        shortcode, action_id = rest.split(":", 1)
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        action = next(a for a in proposal["actions"] if a["id"] == action_id)
        result = install_artifact.install_action(action, shortcode, staging_root, resolution=action_str)
        status = "✅ installed" if result["ok"] else f"❌ {result.get('error')}"
        await query.message.reply_text(f"[{action_id}] {action['type']}: {status}")
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-root", default=str(DEFAULT_JOBS_ROOT))
    ap.add_argument("--staging-root", default=str(DEFAULT_STAGING_ROOT))
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
    application.bot_data["staging_root"] = args.staging_root
    application.add_handler(CommandHandler("agent", cmd_agent))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot starting, jobs_root=%s staging_root=%s", args.jobs_root, args.staging_root)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
