"""
webhook.py — FastAPI webhook receiver. Verifies signatures, parses events,
dispatches in background tasks.
"""

import hashlib
import hmac
import logging

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

import dispatch
import state
from config import GITHUB_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

app = FastAPI(title="agent-flow webhook receiver")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    # Verify HMAC signature
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(body, sig_header):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    import json
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    action = payload.get("action", "")

    logger.info("Received event: %s/%s delivery=%s", event_type, action, delivery_id)

    # Dispatch in background so webhook returns 200 immediately
    background_tasks.add_task(
        dispatch.handle_event,
        event_type=event_type,
        action=action,
        payload=payload,
        delivery_id=delivery_id,
    )

    return JSONResponse({"status": "queued"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    """Show queue depth and active runs per agent type."""
    agents = ["claude", "implementer", "codex"]
    result = {}
    for agent in agents:
        result[agent] = {
            "active": len([r for r in state.get_active_runs() if r["agent"] == agent]),
            "queued": state.get_queue_depth(agent),
            "breaker_tripped": state.is_breaker_tripped(agent),
        }
    return result


def _verify_signature(body: bytes, sig_header: str) -> bool:
    if not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)
