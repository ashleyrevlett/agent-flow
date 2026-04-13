"""
notifications.py — Pipeline event notifications via Hermes gateway.

Python sends structured event messages to the local Hermes gateway API.
The Hermes agent (connected to Telegram via its gateway) decides whether
and how to relay them to the human.

Falls back to logging if the gateway is unreachable.
"""

import json
import logging
from typing import Optional

import httpx

from config import HERMES_GATEWAY_URL

logger = logging.getLogger(__name__)


def send_notification(message: str, issue_url: Optional[str] = None):
    """Send a pipeline event to the Hermes gateway for human relay."""
    text = message
    if issue_url:
        text += f"\n{issue_url}"

    if not HERMES_GATEWAY_URL:
        logger.info("Hermes gateway not configured — notification: %s", text)
        return

    _send_to_gateway(text)


def send_stuck_alert(agent: str, window: str, excerpt: str):
    """Alert when an agent session appears stuck or errored."""
    message = (
        f"Agent alert: @{agent} (session: {window})\n\n"
        f"```\n{excerpt[-400:]}\n```"
    )
    send_notification(message)


def _send_to_gateway(message: str):
    """Send a message to the Hermes gateway API.

    Uses the OpenAI-compatible chat completions endpoint that the Hermes
    gateway exposes. The gateway agent receives the message and relays
    it to the human via Telegram.
    """
    try:
        response = httpx.post(
            f"{HERMES_GATEWAY_URL}/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"[PIPELINE EVENT — relay this to the human on Telegram]\n\n"
                            f"{message}"
                        ),
                    }
                ],
            },
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        if response.status_code != 200:
            logger.warning(
                "Hermes gateway returned %d: %s",
                response.status_code, response.text[:200],
            )
    except httpx.ConnectError:
        logger.warning("Hermes gateway unreachable at %s — notification logged only: %s",
                       HERMES_GATEWAY_URL, message[:200])
    except Exception:
        logger.exception("Failed to send notification via Hermes gateway")


async def start_bot():
    """No-op. Telegram is handled by the Hermes gateway now.

    Kept for backward compatibility with main.py — remove once main.py
    is updated.
    """
    pass
