"""
monitor.py — Periodic pane health checks, completion/stuck detection,
circuit breaker management, restart recovery.
"""

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from typing import Optional

import state
import spawn
import notifications as telegram
from config import (
    GITHUB_REPO,
    MONITOR_POLL_SECONDS,
    IDLE_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

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

# Agent completion/APPROVED patterns in GitHub comments
_AGENT_TAG_RE = re.compile(r"<!--\s*agent:(\w+)\s*-->")
_STATUS_RE = re.compile(r"STATUS:\s*(\w+(?:_\w+)*)")


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
                resume_at = _get_breaker_resume(agent)
                telegram.send_notification(
                    f"Circuit breaker tripped for @{agent} — rate limited. Resuming at {resume_at}.",
                    issue_url=f"https://github.com/{repo}/issues/{issue_number}",
                )
            else:
                excerpt = _extract_error(pane_content)
                logger.error("Error detected in run %d (%s): %s", run_id, agent, excerpt[:200])
                state.fail_run(run_id, new_status="failed")
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

        # --- GitHub comment polling (primary completion signal) ---
        # Claude Code uses alt-screen; pane exit is unreliable. Poll GitHub instead.
        completed, status_token = await _check_github_completion(
            repo, issue_number, agent, run["started_at"]
        )
        if completed:
            logger.info("Run %d completion detected via GitHub comment (STATUS: %s)", run_id, status_token)
            _handle_completion(run_id, agent, issue_number, repo, status_token, run["worktree_path"])


def _handle_completion(run_id: int, agent: str, issue_number: int, repo: str, status_token: Optional[str], worktree_path: Optional[str] = None):
    """Handle a completion event detected via GitHub comment polling."""

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

    # Clean up reviewer worktree
    if worktree_path and agent == "codex":
        from config import REPO_LOCAL_PATH
        spawn.cleanup_worktree(worktree_path, repo_path=REPO_LOCAL_PATH)


async def _check_github_completion(
    repo: str, issue_number: int, agent: str, run_started_at: str
) -> tuple[bool, Optional[str]]:
    """
    Poll GitHub for a completion comment from this agent on this issue that
    was posted AFTER the run started. Returns (completed, status_token).

    Guards against stale comments from prior runs on the same issue
    prematurely completing a new run.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
             "--jq",
             f'[.[] | select(.body | contains("<!-- agent:{agent} -->"))'
             f' | select(.created_at > "{run_started_at}")] | last'],
            capture_output=True, text=True, check=True,
        )
        raw = result.stdout.strip()
        if not raw or raw == "null":
            return False, None

        comment = json.loads(raw)
        body = comment.get("body", "")
        match = _STATUS_RE.search(body)
        if match:
            return True, match.group(1)
        return False, None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False, None


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
