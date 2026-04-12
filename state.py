"""
state.py — SQLite pipeline state. Runs, dedup, cycles, decomposition, dependencies,
circuit breakers. Survives restarts.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config import SQLITE_DB_PATH, DEDUP_TTL_HOURS

import os
os.makedirs(os.path.dirname(SQLITE_DB_PATH), exist_ok=True)

_lock = threading.Lock()

# Allowed stage transitions. Only these forward/backward edges are valid;
# escalated is set via escalate() and never via transition().
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "open":         {"planning"},
    "planning":     {"plan_review", "decomposed"},
    "plan_review":  {"implementing", "planning"},
    "implementing": {"code_review", "planning"},  # planning: BLOCKED re-route to planner
    "code_review":  {"approved", "implementing"},
}


@contextmanager
def _conn():
    con = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    agent TEXT NOT NULL,
    tmux_window TEXT,
    status TEXT NOT NULL,
    prompt_file TEXT,
    worktree_path TEXT,
    pr_branch TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS issue_stages (
    issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'open',
    plan_review_count INTEGER DEFAULT 0,
    code_review_count INTEGER DEFAULT 0,
    escalated BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (issue_number, repo)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decompositions (
    parent_issue_number INTEGER NOT NULL,
    child_issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, child_issue_number)
);

CREATE TABLE IF NOT EXISTS dependencies (
    issue_number INTEGER NOT NULL,
    depends_on_issue INTEGER NOT NULL,
    repo TEXT NOT NULL,
    satisfied BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (repo, issue_number, depends_on_issue)
);

CREATE TABLE IF NOT EXISTS breakers (
    agent TEXT PRIMARY KEY,
    tripped_at TIMESTAMP,
    resume_at TIMESTAMP,
    backoff_seconds INTEGER DEFAULT 300
);

CREATE TABLE IF NOT EXISTS decomposition_meta (
    parent_issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    decomposition_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, parent_issue_number)
);
"""


def init_db():
    with _conn() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def enqueue_run(issue_number: int, repo: str, agent: str, prompt_file: str, pr_branch: Optional[str] = None) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO runs (issue_number, repo, agent, status, prompt_file, pr_branch) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (issue_number, repo, agent, prompt_file, pr_branch),
        )
        return cur.lastrowid


def try_promote(agent: str) -> Optional[sqlite3.Row]:
    """Atomically promote the oldest eligible queued run to active.

    Returns the run row if promoted, None if agent is busy, tripped, or no work.
    """
    if is_breaker_tripped(agent):
        return None

    with _lock:
        with _conn() as con:
            # Check if any active run exists for this agent type
            active = con.execute(
                "SELECT id FROM runs WHERE agent = ? AND status = 'active' LIMIT 1",
                (agent,),
            ).fetchone()
            if active:
                return None

            # Find oldest queued run with no unsatisfied deps
            candidate = con.execute(
                """
                SELECT r.* FROM runs r
                WHERE r.agent = ?
                  AND r.status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM dependencies d
                      WHERE d.issue_number = r.issue_number
                        AND d.repo = r.repo
                        AND d.satisfied = FALSE
                  )
                ORDER BY r.id ASC
                LIMIT 1
                """,
                (agent,),
            ).fetchone()

            if not candidate:
                return None

            con.execute(
                "UPDATE runs SET status = 'active' WHERE id = ? AND status = 'queued'",
                (candidate["id"],),
            )
            return con.execute("SELECT * FROM runs WHERE id = ?", (candidate["id"],)).fetchone()


def update_run_window(run_id: int, tmux_window: str):
    with _conn() as con:
        con.execute(
            "UPDATE runs SET tmux_window = ? WHERE id = ?",
            (tmux_window, run_id),
        )


def update_run_worktree(run_id: int, worktree_path: str):
    with _conn() as con:
        con.execute(
            "UPDATE runs SET worktree_path = ? WHERE id = ?",
            (worktree_path, run_id),
        )


def update_run_pr_branch(run_id: int, pr_branch: str):
    with _conn() as con:
        con.execute(
            "UPDATE runs SET pr_branch = ? WHERE id = ?",
            (pr_branch, run_id),
        )


def complete_run(run_id: int):
    """Idempotent. Only transitions active → completed."""
    with _conn() as con:
        rows = con.execute(
            "UPDATE runs SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'active'",
            (run_id,),
        ).rowcount
    if rows == 0:
        return  # Already completed/failed/stuck — no-op

    # Fetch the agent type to drain its queue
    with _conn() as con:
        run = con.execute("SELECT agent FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run:
        _drain_queue(run["agent"])


def cancel_queued_run(run_id: int) -> bool:
    """Cancel a run that is still queued (never promoted to active).

    fail_run() only matches status='active', so runs aborted before promotion
    (e.g. branch-resolution failures in dispatch) must use this instead.
    Returns True if the row was updated, False if already gone/non-queued.
    """
    with _conn() as con:
        rows = con.execute(
            "UPDATE runs SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'queued'",
            (run_id,),
        ).rowcount
    return rows > 0


def fail_run(run_id: int, new_status: str = "failed"):
    """Idempotent. Only transitions active → failed/stuck."""
    with _conn() as con:
        rows = con.execute(
            f"UPDATE runs SET status = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'active'",
            (new_status, run_id),
        ).rowcount
    if rows == 0:
        return

    with _conn() as con:
        run = con.execute("SELECT agent FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run:
        _drain_queue(run["agent"])


def get_active_runs() -> list:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM runs WHERE status = 'active'"
        ).fetchall()


def get_queue_depth(agent: Optional[str] = None) -> int:
    with _conn() as con:
        if agent:
            return con.execute(
                "SELECT COUNT(*) FROM runs WHERE status = 'queued' AND agent = ?",
                (agent,),
            ).fetchone()[0]
        return con.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'queued'"
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Queue drain (called after complete/fail)
# ---------------------------------------------------------------------------

def _drain_queue(agent: str):
    """Import here to avoid circular import; spawn needs state."""
    from dispatch import drain_queue
    drain_queue(agent)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def is_duplicate(delivery_id: str) -> bool:
    """Atomic insert. Returns True if already seen."""
    with _conn() as con:
        rows = con.execute(
            "INSERT OR IGNORE INTO deliveries (delivery_id) VALUES (?)",
            (delivery_id,),
        ).rowcount
    return rows == 0


def prune_deliveries(max_age_hours: int = DEDUP_TTL_HOURS):
    with _conn() as con:
        con.execute(
            "DELETE FROM deliveries WHERE received_at < datetime('now', ? || ' hours')",
            (f"-{max_age_hours}",),
        )


# ---------------------------------------------------------------------------
# Issue stage machine
# ---------------------------------------------------------------------------

def get_stage(issue_number: int, repo: str) -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT stage FROM issue_stages WHERE issue_number = ? AND repo = ?",
            (issue_number, repo),
        ).fetchone()
        if row:
            return row["stage"]
        # Create default open row
        con.execute(
            "INSERT OR IGNORE INTO issue_stages (issue_number, repo, stage) VALUES (?, ?, 'open')",
            (issue_number, repo),
        )
        return "open"


def transition(issue_number: int, repo: str, expected_stage: str, new_stage: str) -> bool:
    """Atomic stage transition. Returns True on success, False if the current stage
    didn't match expected_stage or the edge is not in the valid transitions graph."""
    if new_stage not in _VALID_TRANSITIONS.get(expected_stage, set()):
        return False
    with _conn() as con:
        # Ensure row exists
        con.execute(
            "INSERT OR IGNORE INTO issue_stages (issue_number, repo, stage) VALUES (?, ?, 'open')",
            (issue_number, repo),
        )
        rows = con.execute(
            "UPDATE issue_stages SET stage = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE issue_number = ? AND repo = ? AND stage = ?",
            (new_stage, issue_number, repo, expected_stage),
        ).rowcount
    return rows > 0


def escalate(issue_number: int, repo: str):
    """Set stage to escalated regardless of current stage."""
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO issue_stages (issue_number, repo, stage) VALUES (?, ?, 'open')",
            (issue_number, repo),
        )
        con.execute(
            "UPDATE issue_stages SET stage = 'escalated', escalated = TRUE, updated_at = CURRENT_TIMESTAMP "
            "WHERE issue_number = ? AND repo = ?",
            (issue_number, repo),
        )


def get_review_count(issue_number: int, repo: str, review_type: str) -> int:
    col = "plan_review_count" if review_type == "plan" else "code_review_count"
    with _conn() as con:
        row = con.execute(
            f"SELECT {col} FROM issue_stages WHERE issue_number = ? AND repo = ?",
            (issue_number, repo),
        ).fetchone()
    return row[col] if row else 0


def increment_review_count(issue_number: int, repo: str, review_type: str):
    col = "plan_review_count" if review_type == "plan" else "code_review_count"
    with _conn() as con:
        con.execute(
            f"UPDATE issue_stages SET {col} = {col} + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE issue_number = ? AND repo = ?",
            (issue_number, repo),
        )


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

def trip_breaker(agent: str):
    with _conn() as con:
        row = con.execute(
            "SELECT backoff_seconds FROM breakers WHERE agent = ?", (agent,)
        ).fetchone()
        current_backoff = row["backoff_seconds"] if row else 300
        new_backoff = min(current_backoff * 2, 3600)
        now = datetime.now(timezone.utc).isoformat()
        resume = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + new_backoff, tz=timezone.utc
        ).isoformat()
        con.execute(
            "INSERT INTO breakers (agent, tripped_at, resume_at, backoff_seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET tripped_at = excluded.tripped_at, "
            "resume_at = excluded.resume_at, backoff_seconds = excluded.backoff_seconds",
            (agent, now, resume, new_backoff),
        )


def is_breaker_tripped(agent: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT resume_at FROM breakers WHERE agent = ?", (agent,)
        ).fetchone()
    if not row or not row["resume_at"]:
        return False
    return row["resume_at"] > datetime.now(timezone.utc).isoformat()


def reset_breaker(agent: str):
    with _conn() as con:
        con.execute(
            "UPDATE breakers SET tripped_at = NULL, resume_at = NULL, backoff_seconds = 300 "
            "WHERE agent = ?",
            (agent,),
        )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def record_dependency(issue_number: int, depends_on_issue: int, repo: str):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO dependencies (issue_number, depends_on_issue, repo) VALUES (?, ?, ?)",
            (issue_number, depends_on_issue, repo),
        )


def satisfy_dependency(closed_issue: int, repo: str):
    with _conn() as con:
        con.execute(
            "UPDATE dependencies SET satisfied = TRUE WHERE depends_on_issue = ? AND repo = ?",
            (closed_issue, repo),
        )
    # Drain queues for all agent types now that deps may be satisfied
    for agent in ("claude", "implementer", "codex"):
        _drain_queue(agent)


def is_blocked(issue_number: int, repo: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM dependencies WHERE issue_number = ? AND repo = ? AND satisfied = FALSE",
            (issue_number, repo),
        ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------

def record_decomposition(parent_issue: int, child_issue: int, repo: str, sequence_index: int):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO decompositions "
            "(parent_issue_number, child_issue_number, repo, sequence_index) VALUES (?, ?, ?, ?)",
            (parent_issue, child_issue, repo, sequence_index),
        )


def mark_decomposition_done(parent_issue: int, repo: str):
    with _conn() as con:
        con.execute(
            "INSERT INTO decomposition_meta (parent_issue_number, repo, decomposition_done) VALUES (?, ?, TRUE) "
            "ON CONFLICT(repo, parent_issue_number) DO UPDATE SET decomposition_done = TRUE, updated_at = CURRENT_TIMESTAMP",
            (parent_issue, repo),
        )


def is_decomposition_done(parent_issue: int, repo: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT decomposition_done FROM decomposition_meta WHERE parent_issue_number = ? AND repo = ?",
            (parent_issue, repo),
        ).fetchone()
    return bool(row and row["decomposition_done"])


def get_decomposition_depth(issue: int, repo: str) -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT depth FROM decomposition_meta WHERE parent_issue_number = ? AND repo = ?",
            (issue, repo),
        ).fetchone()
    return row["depth"] if row else 0


def record_decomposition_meta(issue: int, repo: str, depth: int):
    with _conn() as con:
        con.execute(
            "INSERT INTO decomposition_meta (parent_issue_number, repo, depth) VALUES (?, ?, ?) "
            "ON CONFLICT(repo, parent_issue_number) DO UPDATE SET depth = excluded.depth, updated_at = CURRENT_TIMESTAMP",
            (issue, repo, depth),
        )


def list_children(parent_issue: int, repo: str) -> list:
    with _conn() as con:
        return con.execute(
            "SELECT * FROM decompositions WHERE parent_issue_number = ? AND repo = ? ORDER BY sequence_index ASC",
            (parent_issue, repo),
        ).fetchall()
