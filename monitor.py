"""
monitor.py — Periodic pane health checks, completion/stuck detection,
circuit breaker management, restart recovery.
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import state
import spawn
import notifications as telegram
from config import (
    MONITOR_POLL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
)
from provider import get_provider

logger = logging.getLogger(__name__)

_provider = get_provider()

# Track pane content hashes and last-change timestamps per run_id
_pane_hashes: dict[int, str] = {}
_pane_last_changed: dict[int, float] = {}

# Error patterns that indicate a failed or rate-limited agent
_ERROR_PATTERNS = re.compile(
    r"Error:|Traceback|FATAL|rate limit|authentication failed|401 Unauthorized",
    re.IGNORECASE,
)
_RATE_LIMIT_PATTERNS = re.compile(
    r"rate limit|429|too many requests|authentication failed|401",
    re.IGNORECASE,
)


async def monitor_loop():
    """Main async monitoring loop. Runs alongside FastAPI."""
    logger.info("Monitor starting — recovery pass")
    await _startup_recovery()

    while True:
        try:
            await _poll()
        except Exception:
            logger.exception("Monitor poll error")
        await asyncio.sleep(MONITOR_POLL_SECONDS)


def _cleanup_run_worktree(run) -> None:
    """Remove a reviewer worktree when the run ends (any terminal status)."""
    worktree_path = run["worktree_path"] if hasattr(run, "__getitem__") else None
    if worktree_path and run["agent"] == "codex":
        from config import REPO_LOCAL_PATH
        spawn.cleanup_worktree(worktree_path, repo_path=REPO_LOCAL_PATH)


async def _startup_recovery():
    """On startup: reconcile DB state with actual tmux windows."""
    active_runs = state.get_active_runs()
    live_windows = set(spawn.list_windows())

    for run in active_runs:
        run_id = run["id"]
        window = run["tmux_window"]
        if window and window not in live_windows:
            logger.warning("Orphaned run %d (window %s gone) — marking failed", run_id, window)
            state.fail_run(run_id)
            _cleanup_run_worktree(run)
        else:
            # Re-register in our tracking dicts
            import time
            _pane_last_changed[run_id] = time.time()

    # Drain queues — pick up any work that was queued before restart
    for agent in ("claude", "implementer", "codex"):
        from dispatch import drain_queue
        drain_queue(agent)


async def _poll():
    import time

    active_runs = state.get_active_runs()
    if not active_runs:
        return

    for run in active_runs:
        run_id = run["id"]
        agent = run["agent"]
        issue_number = run["issue_number"]
        repo = run["repo"]
        window = run["tmux_window"]

        if not window:
            continue

        # --- Pane health check ---
        pane_content = spawn.capture_pane(window)
        content_hash = hashlib.md5(pane_content.encode()).hexdigest()

        if content_hash != _pane_hashes.get(run_id):
            _pane_hashes[run_id] = content_hash
            _pane_last_changed[run_id] = time.time()

        idle_seconds = time.time() - _pane_last_changed.get(run_id, time.time())

        # --- Stuck detection ---
        if idle_seconds >= IDLE_TIMEOUT_SECONDS:
            logger.warning(
                "Run %d (%s on #%s) stuck for %.0fs — alerting",
                run_id, agent, issue_number, idle_seconds,
            )
            state.fail_run(run_id, new_status="stuck")
            _cleanup_run_worktree(run)
            telegram.send_stuck_alert(
                agent=agent,
                window=window,
                excerpt=pane_content[-500:],
            )
            continue

        # --- Error detection ---
        if _ERROR_PATTERNS.search(pane_content):
            if _RATE_LIMIT_PATTERNS.search(pane_content):
                logger.warning("Circuit breaker trip: rate limit/auth error for %s", agent)
                state.trip_breaker(agent)
                # Mark run failed so try_promote() is not permanently blocked
                # waiting for an active run that will never complete.
                state.fail_run(run_id, new_status="failed")
                _cleanup_run_worktree(run)
                resume_at = _get_breaker_resume(agent)
                telegram.send_notification(
                    f"Circuit breaker tripped for @{agent} — rate limited. Resuming at {resume_at}.",
                    issue_url=_provider.issue_url(repo, issue_number),
                )
            else:
                excerpt = _extract_error(pane_content)
                logger.error("Error detected in run %d (%s): %s", run_id, agent, excerpt[:200])
                state.fail_run(run_id, new_status="failed")
                _cleanup_run_worktree(run)
                telegram.send_stuck_alert(
                    agent=agent,
                    window=window,
                    excerpt=excerpt,
                )
            continue

        # --- Circuit breaker expiry ---
        if state.is_breaker_tripped(agent):
            # Check if it's time to reset
            # (resume_at comparison is done inside is_breaker_tripped)
            pass
        else:
            # Proactively reset if breaker was previously tripped but has now expired
            _try_reset_breaker(agent)

        # --- Comment polling (primary completion signal) ---
        # Claude Code uses alt-screen; pane exit is unreliable. Poll git platform instead.
        iso_started_at = _sqlite_ts_to_iso(run["started_at"])
        completed, status_token = _provider.check_completion(
            repo, issue_number, agent, iso_started_at
        )
        if completed:
            logger.info("Run %d completion detected via comment (STATUS: %s)", run_id, status_token)
            _handle_completion(run, status_token)


def _handle_completion(run, status_token: Optional[str]):
    """Handle a completion event detected via comment polling."""
    run_id = run["id"]
    agent = run["agent"]
    issue_number = run["issue_number"]
    repo = run["repo"]

    # APPROVED — monitor handles this (no @mention so dispatch never sees it)
    if status_token == "APPROVED" and agent == "codex":
        if state.transition(issue_number, repo, "code_review", "approved"):
            logger.info("Issue #%s transitioned to approved", issue_number)
        state.complete_run(run_id)

    # DECOMPOSED — monitor handles this similarly
    elif status_token == "DECOMPOSED" and agent == "claude":
        if state.transition(issue_number, repo, "planning", "decomposed"):
            logger.info("Issue #%s transitioned to decomposed", issue_number)
        state.complete_run(run_id)

    else:
        # All other terminal statuses — mark complete and let dispatch handle routing
        state.complete_run(run_id)

    _cleanup_run_worktree(run)


def _sqlite_ts_to_iso(ts: str) -> str:
    """Convert SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS") to ISO 8601 UTC.

    Git platform comment timestamps use ISO 8601. Normalising the SQLite
    value ensures consistent comparison regardless of provider.
    """
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        # Already ISO or unrecognised — return as-is and let JQ decide.
        return ts


def _extract_error(pane_content: str) -> str:
    """Return the last 800 chars of pane around the first error pattern."""
    match = _ERROR_PATTERNS.search(pane_content)
    if not match:
        return pane_content[-500:]
    start = max(0, match.start() - 100)
    return pane_content[start:start + 800]


def _get_breaker_resume(agent: str) -> str:
    with state._conn() as con:
        row = con.execute(
            "SELECT resume_at FROM breakers WHERE agent = ?", (agent,)
        ).fetchone()
    return row["resume_at"] if row else "unknown"


def _try_reset_breaker(agent: str):
    """Reset expired breakers."""
    with state._conn() as con:
        row = con.execute(
            "SELECT resume_at FROM breakers WHERE agent = ? AND resume_at IS NOT NULL",
            (agent,),
        ).fetchone()
    if row:
        from datetime import datetime, timezone
        if row["resume_at"] <= datetime.now(timezone.utc).isoformat():
            state.reset_breaker(agent)
            logger.info("Circuit breaker reset for %s", agent)
