"""
webhook.py — FastAPI webhook receiver. Delegates verification and payload
parsing to the configured git provider.
"""

import logging

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

import dispatch
import state
from provider import get_provider

logger = logging.getLogger(__name__)

app = FastAPI(title="agent-flow webhook receiver")

_provider = get_provider()


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    headers = dict(request.headers)

    if not _provider.verify_webhook(body, headers):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = _provider.parse_webhook(body, headers)
    if event is None:
        return JSONResponse({"status": "ignored"})

    logger.info("Received event: %s delivery=%s", event.kind, event.delivery_id)

    background_tasks.add_task(dispatch.handle_event, event=event)

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
