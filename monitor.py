"""
monitor.py — Periodic health checks, completion detection via comment polling,
circuit breaker management, and restart recovery.

With Hermes-based spawning, idle/error detection is handled by the Hermes agent
itself. Monitor focuses on:
- GitHub/GitLab comment polling (primary completion signal for terminal statuses)
- Circuit breaker expiry
- Startup recovery (reconcile DB with live Hermes sessions)
- Worktree cleanup
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import state
import spawn
import hermes_spawn
import notifications as telegram
from config import (
    MONITOR_POLL_SECONDS,
)
from provider import get_provider

logger = logging.getLogger(__name__)

_provider = get_provider()


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
    """On startup: reconcile DB state with live tmux windows / Hermes sessions."""
    active_runs = state.get_active_runs()
    live_windows = set(hermes_spawn.list_windows())

    for run in active_runs:
        run_id = run["id"]
        window = run["tmux_window"]
        # Check both: tmux window exists AND monitor thread alive
        window_alive = window and window in live_windows
        thread_alive = hermes_spawn.is_session_alive(run_id)
        if not window_alive and not thread_alive:
            logger.warning("Orphaned run %d (window %s gone) — marking failed", run_id, window)
            state.fail_run(run_id)
            _cleanup_run_worktree(run)
            _cleanup_run_artifacts(run)

    # Drain queues — pick up any work that was queued before restart
    for agent in ("claude", "implementer", "codex"):
        from dispatch import drain_queue
        drain_queue(agent)


async def _poll():
    active_runs = state.get_active_runs()
    if not active_runs:
        return

    for run in active_runs:
        run_id = run["id"]
        agent = run["agent"]
        issue_number = run["issue_number"]
        repo = run["repo"]

        # --- Session health check ---
        # If both the tmux window and monitor thread are gone but the run
        # is still active in DB, the completion signal didn't land.
        window = run["tmux_window"]
        window_alive = window and window in hermes_spawn.list_windows()
        thread_alive = hermes_spawn.is_session_alive(run_id)
        if not window_alive and not thread_alive:
            logger.warning(
                "Run %d (%s on #%s) — window and thread gone, marking failed",
                run_id, agent, issue_number,
            )
            state.fail_run(run_id, new_status="failed")
            _cleanup_run_worktree(run)
            _cleanup_run_artifacts(run)
            continue

        # --- Circuit breaker expiry ---
        if state.is_breaker_tripped(agent):
            pass
        else:
            _try_reset_breaker(agent)

        # --- Comment polling (primary completion signal) ---
        # The Hermes agent handles CLI interaction and calls complete_run/fail_run
        # when done. But some terminal statuses (APPROVED, DECOMPOSED) are detected
        # via GitHub/GitLab comments because they have no @mention for dispatch.
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
    _cleanup_run_artifacts(run)


def _cleanup_run_artifacts(run):
    """Clean up prompt files and Claude Code worktrees after a run ends."""
    prompt_file = run["prompt_file"]
    if prompt_file:
        hermes_spawn._cleanup_prompt_file(prompt_file)

    agent = run["agent"]
    issue_id = str(run["issue_number"])
    run_id = run["id"]
    hermes_spawn._cleanup_claude_worktrees(agent, issue_id, run_id)


def _sqlite_ts_to_iso(ts: str) -> str:
    """Convert SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS") to ISO 8601 UTC.

    Git platform comment timestamps use ISO 8601. Normalising the SQLite
    value ensures consistent comparison regardless of provider.
    """
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ts


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
        if row["resume_at"] <= datetime.now(timezone.utc).isoformat():
            state.reset_breaker(agent)
            logger.info("Circuit breaker reset for %s", agent)
