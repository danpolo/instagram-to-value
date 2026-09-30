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

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                        Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                            ContextTypes, MessageHandler, filters)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import extract_shortcode
from jobs_lib import find_job, write_job
from secrets_lib import DEFAULT_SECRETS, load_secret
import agents_lib
sys.path.insert(0, str(Path(__file__).resolve().parent / "actions"))
import benchmark
import benchmarks_lib
import install_artifact
import registry
import staging_lib

registry.load_all_handlers()

REPO_ROOT = Path(__file__).resolve().parent.parent
# How many staged proposals /pending pushes per invocation, each with buttons.
DEFAULT_PENDING_BATCH = 10
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


def parse_shortcode(text: str):
    """extract_shortcode is a CLI helper and raises SystemExit; the bot needs a
    value it can branch on, because a discard reason like "already have it" is
    simply not a post URL."""
    try:
        return extract_shortcode(text)
    except SystemExit:
        return None


def pick_reason_prompt(reply_to_message_id, awaiting, is_new_link):
    """Pure: which open "why discard X?" prompt this message answers, or None.

    `awaiting` maps each prompt's message id to the shortcode it asked about.
    Discarding two proposals before answering either used to share ONE slot, so
    the second tap overwrote the first shortcode: answer #1 was filed against
    proposal #2, and answer #2 found an empty slot and fell through to the URL
    parser as if it were a new post. So: an explicit reply always wins, a bare
    message is only attributable when exactly one prompt is open, and a fresh
    post URL is never a reason."""
    if reply_to_message_id in awaiting:
        return reply_to_message_id
    if len(awaiting) == 1 and not is_new_link:
        return next(iter(awaiting))
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed_chat_id = context.bot_data["allowed_chat_id"]
    jobs_root = context.bot_data["jobs_root"]
    staging_root = context.bot_data["staging_root"]
    chat_id = update.effective_chat.id

    if not is_allowed_chat(chat_id, allowed_chat_id):
        logger.warning("rejected message from unauthorized chat_id=%s", chat_id)
        return

    text = (update.message.text or "").strip()

    awaiting = context.user_data.setdefault("awaiting_reason_for", {})
    reply_to = getattr(update.message, "reply_to_message", None)
    shortcode = parse_shortcode(text)
    prompt_id = pick_reason_prompt(reply_to.message_id if reply_to else None,
                                    awaiting, shortcode is not None)
    if prompt_id is not None:
        discarded = awaiting.pop(prompt_id)
        staging_lib.record_reject_reason(discarded, staging_root, text)
        await update.message.reply_text(f"Noted. {discarded} discarded.")
        return

    if awaiting and shortcode is None:
        # Never guess: filing this against an arbitrary open prompt is exactly
        # the bug the single slot caused.
        open_list = ", ".join(sorted(awaiting.values()))
        await update.message.reply_text(
            f"Which one? Reply to its \"why discard …?\" message — open: {open_list}.")
        return

    if shortcode is None:
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
    groups = staging_lib.pending_action_groups(staging_root)
    if not groups:
        await update.message.reply_text("Nothing pending.")
        return
    try:
        limit = int(context.args[0]) if context.args else DEFAULT_PENDING_BATCH
    except (TypeError, ValueError):
        limit = DEFAULT_PENDING_BATCH
    limit = max(1, min(limit, len(groups)))

    # A flat list of actions, one card each with its own Approve/Skip -- not
    # grouped by post. Dan decides per action ("what is it, why, how many
    # stars"), so the post is kept only in the callback data. The same tool
    # suggested by several posts is one card (staging_lib.pending_action_groups).
    # Batched because the backlog is dozens of actions and Telegram rate-limits bursts.
    describe_fns = {t: h.describe for t, h in registry.REGISTRY.items()}
    header = f"{len(groups)} actions pending — sending {limit}."
    if limit < len(groups):
        header += f" `/pending {min(len(groups), limit * 2)}` for more."
    await update.message.reply_text(header)

    for group in groups[:limit]:
        shortcode, proposal, action = group["items"][0]
        posts = len({sc for sc, _, _ in group["items"]})
        text = staging_lib.format_action_card(proposal, action, describe_fns.get(action["type"]), posts=posts,
                                              installed_elsewhere=group["installed_elsewhere"])
        await update.message.reply_text(text, reply_markup=_action_buttons(shortcode, action["id"]))


async def cmd_benchmarks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Benchmark gate B4: what still needs a benchmark and what failed one."""
    if not is_allowed_chat(update.effective_chat.id, context.bot_data["allowed_chat_id"]):
        return
    view = benchmarks_lib.attention(benchmarks_lib.load(_benchmarks_path(context)))
    lines = []
    if view["failing"]:
        lines += ["Failed their benchmark:"] + [f"• {r['name']}" for r in view["failing"]] + [""]
    if view["not_benchmarked"]:
        lines += ["Not benchmarked yet:"] + [
            f"• {r['name']}" + (" — last try hit a problem, will retry" if r.get("last_error") else "")
            for r in view["not_benchmarked"]]
    await update.message.reply_text("\n".join(lines).strip() or "Everything installed has passed its benchmark.")


def _benchmarks_path(context):
    return context.bot_data.get("benchmarks_path") or benchmarks_lib.path_for(context.bot_data["staging_root"])


def _action_buttons(shortcode, action_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"act_ok:{shortcode}:{action_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"act_skip:{shortcode}:{action_id}"),
    ]])


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
        # Handlers that had to resolve something themselves report it here --
        # skill_install turns a bare name into an owner/repo, and an exec
        # install that says only "installed" hides whose code just ran.
        if result.get("note"):
            status += f" — {result['note']}"
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

    if action_str in ("bench_keep", "bench_rm"):
        path = _benchmarks_path(context)
        record = benchmarks_lib.load(path)["records"].get(rest)
        if record is None or record["state"] != "verdict_sent":
            state = record["state"].replace("_", " ") if record else "gone"
            await query.message.reply_text(f"That one is already {state}.")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning("could not clear buttons: %s", e)
        now = benchmarks_lib.now_iso()
        if action_str == "bench_keep":
            benchmarks_lib.update(path, lambda reg: benchmarks_lib.set_state(reg, rest, "kept_despite_failure", now))
            await query.message.reply_text(f"👍 Kept {record['name']}. It stays installed.")
            return
        notes = benchmark.remove_installs(record)
        benchmarks_lib.update(path, lambda reg: benchmarks_lib.set_state(reg, rest, "uninstalled", now,
                                                                         removal_notes=notes))
        await query.message.reply_text(f"🗑 Removed {record['name']}.\n" + "\n".join(notes))
        return

    if action_str in ("act_ok", "act_skip"):
        shortcode, _, action_id = rest.partition(":")
        proposal = staging_lib.read_proposal(shortcode, staging_root)
        action = next((a for a in (proposal or {}).get("actions", []) if a["id"] == action_id), None)
        if action is None or action["status"] != "pending":
            state = action["status"] if action else "gone"
            await query.message.reply_text(f"That one is already {state}.")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning("could not clear buttons: %s", e)
        group = next((g for g in staging_lib.pending_action_groups(staging_root)
                      if any(sc == shortcode and a["id"] == action_id for sc, _, a in g["items"])), None)
        others = [(sc, a) for sc, _, a in (group or {"items": []})["items"]
                  if not (sc == shortcode and a["id"] == action_id)]
        if action_str == "act_skip":
            for sc, a in [(shortcode, action)] + others:
                install_artifact.install_action(a, sc, staging_root, resolution="cancel")
            describe = registry.get_handler(action["type"]).describe(action["payload"])
            await query.message.reply_text(f"❌ Skipped: {describe}")
            return
        result = install_artifact.install_action(action, shortcode, staging_root)
        if result.get("ok"):
            # The same tool suggested by other posts is now handled; close those too.
            for sc, a in others:
                staging_lib.update_action_status(sc, staging_root, a["id"], "skipped", {
                    "ok": True, "path": None, "command": None, "exit_code": None, "error": None,
                    "output": f"duplicate of {shortcode}/{action_id}, installed together"})
        await _report_install_results(query, shortcode, [(action, result)])
        return

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
                  + (f"\n{staging_lib.format_evidence_block(a)}" if a.get("evidence") else "")
                  for a in proposal["actions"]]
        text = "\n\n".join(parts) or "No actions."
        for start in range(0, len(text), 3500):
            await query.message.reply_text(text[start:start + 3500])
        return

    if action_str == "discard":
        awaiting = context.user_data.setdefault("awaiting_reason_for", {})
        if rest in awaiting.values():
            await query.message.reply_text(f"Already asked why {rest} — reply to that message.")
            return
        # Named + ForceReply so several open prompts stay tellable apart: the
        # client pre-addresses the reply, and pick_reason_prompt routes it.
        prompt = await query.message.reply_text(f"why discard {rest}?",
                                                    reply_markup=ForceReply(selective=True))
        awaiting[prompt.message_id] = rest
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
    application.add_handler(CommandHandler("benchmarks", cmd_benchmarks))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("telegram_bot starting, jobs_root=%s staging_root=%s", args.jobs_root, args.staging_root)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
