"""
Tests for state.py — stage machine, dedup, run lifecycle, circuit breakers.
"""

import os
import tempfile
import pytest

# Point DB at a temp file before importing state
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["SQLITE_DB_PATH"] = _db_path
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("GITHUB_REPO", "owner/repo")

import state  # noqa: E402 — env must be set first

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Each test gets a fresh DB."""
    import importlib, os, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    state.SQLITE_DB_PATH = path  # patch module-level constant
    # Monkey-patch the config import inside state
    import config
    config.SQLITE_DB_PATH = path
    # Re-open connections will use the new path via _conn()
    # Simplest: recreate the module's path via monkeypatching the env
    os.environ["SQLITE_DB_PATH"] = path
    # Reload state to pick up new path
    importlib.reload(state)
    state.init_db()
    yield
    os.unlink(path)


# ---------------------------------------------------------------------------
# Stage machine
# ---------------------------------------------------------------------------

class TestStageMachine:
    def test_default_stage_is_open(self):
        assert state.get_stage(1, REPO) == "open"

    def test_valid_transition_succeeds(self):
        state.get_stage(1, REPO)  # create row
        assert state.transition(1, REPO, "open", "planning") is True
        assert state.get_stage(1, REPO) == "planning"

    def test_invalid_transition_fails(self):
        state.get_stage(1, REPO)
        # Can't jump from open to implementing
        assert state.transition(1, REPO, "open", "implementing") is False
        assert state.get_stage(1, REPO) == "open"

    def test_wrong_expected_stage_fails(self):
        state.get_stage(1, REPO)
        state.transition(1, REPO, "open", "planning")
        # Already in planning; trying "open" → "planning" again should fail
        assert state.transition(1, REPO, "open", "planning") is False

    def test_escalate_overrides_any_stage(self):
        state.get_stage(1, REPO)
        state.transition(1, REPO, "open", "planning")
        state.escalate(1, REPO)
        assert state.get_stage(1, REPO) == "escalated"

    def test_full_happy_path(self):
        issue = 42
        for expected, new in [
            ("open",         "planning"),
            ("planning",     "plan_review"),
            ("plan_review",  "implementing"),
            ("implementing", "code_review"),
            ("code_review",  "approved"),
        ]:
            assert state.transition(issue, REPO, expected, new) is True
        assert state.get_stage(issue, REPO) == "approved"

    def test_review_count(self):
        state.get_stage(1, REPO)
        assert state.get_review_count(1, REPO, "plan") == 0
        state.increment_review_count(1, REPO, "plan")
        state.increment_review_count(1, REPO, "plan")
        assert state.get_review_count(1, REPO, "plan") == 2
        assert state.get_review_count(1, REPO, "code") == 0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_first_delivery_not_duplicate(self):
        assert state.is_duplicate("abc-123") is False

    def test_second_delivery_is_duplicate(self):
        state.is_duplicate("abc-123")
        assert state.is_duplicate("abc-123") is True

    def test_different_deliveries_not_duplicate(self):
        state.is_duplicate("abc-123")
        assert state.is_duplicate("def-456") is False


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

class TestRunLifecycle:
    def test_enqueue_and_promote(self):
        run_id = state.enqueue_run(1, REPO, "claude", "/tmp/prompt.md")
        assert run_id > 0
        run = state.try_promote("claude")
        assert run is not None
        assert run["id"] == run_id
        assert run["status"] == "active"

    def test_second_promote_blocked_while_active(self):
        state.enqueue_run(1, REPO, "claude", "/tmp/p1.md")
        state.enqueue_run(2, REPO, "claude", "/tmp/p2.md")
        state.try_promote("claude")  # promotes first
        assert state.try_promote("claude") is None  # second blocked

    def test_complete_run_idempotent(self):
        # Patch drain_queue to no-op for this test
        import unittest.mock as mock
        with mock.patch("dispatch.drain_queue"):
            run_id = state.enqueue_run(1, REPO, "claude", "/tmp/p.md")
            state.try_promote("claude")
            state.complete_run(run_id)
            state.complete_run(run_id)  # should not raise

    def test_fail_run_idempotent(self):
        import unittest.mock as mock
        with mock.patch("dispatch.drain_queue"):
            run_id = state.enqueue_run(1, REPO, "claude", "/tmp/p.md")
            state.try_promote("claude")
            state.fail_run(run_id)
            state.fail_run(run_id)  # should not raise

    def test_get_active_runs(self):
        import unittest.mock as mock
        with mock.patch("dispatch.drain_queue"):
            r1 = state.enqueue_run(1, REPO, "claude", "/tmp/p.md")
            state.try_promote("claude")
            active = state.get_active_runs()
            assert any(r["id"] == r1 for r in active)
            state.complete_run(r1)
            active = state.get_active_runs()
            assert not any(r["id"] == r1 for r in active)

    def test_queue_depth(self):
        state.enqueue_run(1, REPO, "claude", "/tmp/p1.md")
        state.enqueue_run(2, REPO, "claude", "/tmp/p2.md")
        assert state.get_queue_depth("claude") == 2
        assert state.get_queue_depth("codex") == 0


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_blocked_until_satisfied(self):
        import unittest.mock as mock
        with mock.patch("dispatch.drain_queue"):
            state.record_dependency(10, 9, REPO)  # 10 depends on 9
            run_id = state.enqueue_run(10, REPO, "claude", "/tmp/p.md")
            assert state.try_promote("claude") is None  # blocked
            state.satisfy_dependency(9, REPO)
            run = state.try_promote("claude")
            assert run is not None
            assert run["id"] == run_id

    def test_is_blocked(self):
        state.record_dependency(10, 9, REPO)
        assert state.is_blocked(10, REPO) is True
        state.satisfy_dependency(9, REPO)
        assert state.is_blocked(10, REPO) is False


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_breaker_not_tripped_initially(self):
        assert state.is_breaker_tripped("claude") is False

    def test_trip_and_detect(self):
        state.trip_breaker("claude")
        assert state.is_breaker_tripped("claude") is True

    def test_reset_clears_breaker(self):
        state.trip_breaker("claude")
        state.reset_breaker("claude")
        assert state.is_breaker_tripped("claude") is False

    def test_tripped_agent_not_promoted(self):
        state.enqueue_run(1, REPO, "claude", "/tmp/p.md")
        state.trip_breaker("claude")
        assert state.try_promote("claude") is None
