"""
main.py — Entry point. Starts FastAPI, monitor loop, and Telegram bot.
"""

import asyncio
import logging

import uvicorn

import state
import spawn
import telegram
from config import TMUX_SESSION_NAME
from webhook import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _run_all():
    # Initialize state DB
    state.init_db()
    logger.info("State DB initialized")

    # Ensure tmux session exists
    spawn.ensure_session()
    logger.info("tmux session ready: %s", TMUX_SESSION_NAME)

    # Start monitor loop
    monitor_task = asyncio.create_task(
        _run_monitor(),
        name="monitor",
    )

    # Start Telegram bot
    telegram_task = asyncio.create_task(
        telegram.start_bot(),
        name="telegram-bot",
    )

    # Start FastAPI via uvicorn
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        loop="none",  # Use the running event loop
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    logger.info("agent-flow started on port 8000")

    # Run until any task exits (crash = restart the whole process)
    done, pending = await asyncio.wait(
        {monitor_task, telegram_task, server_task},
        return_when=asyncio.FIRST_EXCEPTION,
    )

    for task in done:
        if task.exception():
            logger.exception("Task %s failed", task.get_name(), exc_info=task.exception())

    for task in pending:
        task.cancel()


async def _run_monitor():
    from monitor import monitor_loop
    await monitor_loop()


if __name__ == "__main__":
    asyncio.run(_run_all())
