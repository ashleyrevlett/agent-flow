"""
telegram.py — Telegram bot for @human escalation, status commands, and alerts.
"""

import asyncio
import logging
from typing import Optional

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_REPO

logger = logging.getLogger(__name__)

# Optional: only initialize if token is configured
_bot = None
_app = None


def _get_bot():
    global _bot, _app
    if _bot is None and TELEGRAM_BOT_TOKEN:
        try:
            from telegram import Bot
            from telegram.ext import Application, CommandHandler
            _bot = Bot(token=TELEGRAM_BOT_TOKEN)
        except ImportError:
            logger.warning("python-telegram-bot not installed — Telegram disabled")
    return _bot


# ---------------------------------------------------------------------------
# Outbound notifications
# ---------------------------------------------------------------------------

def send_notification(message: str, issue_url: Optional[str] = None):
    """Send a plain text notification to the configured chat."""
    if not TELEGRAM_CHAT_ID:
        logger.info("Telegram not configured — notification: %s", message)
        return
    bot = _get_bot()
    if not bot:
        return
    text = message
    if issue_url:
        text += f"\n{issue_url}"
    try:
        asyncio.get_event_loop().run_until_complete(
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        )
    except RuntimeError:
        # If there's a running event loop (inside FastAPI), schedule as coroutine
        asyncio.ensure_future(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text))
    except Exception:
        logger.exception("Failed to send Telegram notification")


def send_stuck_alert(agent: str, window: str, excerpt: str):
    """Alert when an agent window appears stuck or errored."""
    message = (
        f"Agent alert: @{agent} (window: {window})\n\n"
        f"```\n{excerpt[-400:]}\n```"
    )
    send_notification(message)


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------

async def _start_bot():
    """Start the Telegram bot with command handlers."""
    if not TELEGRAM_BOT_TOKEN:
        logger.info("TELEGRAM_BOT_TOKEN not set — bot disabled")
        return

    try:
        from telegram.ext import Application, CommandHandler
    except ImportError:
        logger.warning("python-telegram-bot not installed — bot disabled")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("create_issue", _cmd_create_issue))
    application.add_handler(CommandHandler("status", _cmd_status))
    application.add_handler(CommandHandler("kill", _cmd_kill))

    logger.info("Starting Telegram bot")
    await application.run_polling(close_loop=False)


async def _cmd_create_issue(update, context):
    """Create a GitHub issue from Telegram: /create_issue <title>"""
    import subprocess
    if not context.args:
        await update.message.reply_text("Usage: /create_issue <issue title>")
        return

    title = " ".join(context.args)
    try:
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", GITHUB_REPO,
             "--title", title, "--body", "(Created via Telegram)"],
            capture_output=True, text=True, check=True,
        )
        issue_url = result.stdout.strip()
        await update.message.reply_text(f"Created: {issue_url}")
    except subprocess.CalledProcessError as exc:
        await update.message.reply_text(f"Failed: {exc.stderr}")


async def _cmd_status(update, context):
    """/status — show active agent windows and queue depth."""
    import state
    import spawn

    lines = ["*Pipeline Status*\n"]
    for agent in ("claude", "implementer", "codex"):
        active = [r for r in state.get_active_runs() if r["agent"] == agent]
        queued = state.get_queue_depth(agent)
        breaker = "TRIPPED" if state.is_breaker_tripped(agent) else "ok"
        lines.append(f"@{agent}: {len(active)} active, {queued} queued, breaker={breaker}")
        for r in active:
            lines.append(f"  └ run {r['id']} → issue #{r['issue_number']} window={r['tmux_window']}")

    windows = spawn.list_windows()
    lines.append(f"\nTmux windows: {', '.join(windows) or 'none'}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_kill(update, context):
    """/kill <window> — kill a stuck agent window."""
    import state
    import spawn

    if not context.args:
        await update.message.reply_text("Usage: /kill <window_name>")
        return

    window_name = context.args[0]

    # Find the run by window name and mark failed
    active_runs = state.get_active_runs()
    matched = [r for r in active_runs if r["tmux_window"] == window_name]

    spawn.kill_window(window_name)

    for run in matched:
        state.fail_run(run["id"], new_status="failed")

    if matched:
        await update.message.reply_text(
            f"Killed window '{window_name}' and marked {len(matched)} run(s) as failed."
        )
    else:
        await update.message.reply_text(f"Killed window '{window_name}' (no active runs matched).")


async def start_bot():
    """Public entry point called from main.py."""
    await _start_bot()
