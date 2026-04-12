"""
Tests for dispatch.py — mention parsing, status parsing, routing table,
auth filtering, and TESTS_FAILING/BLOCKED routing corrections.
"""

import os
import pytest
import unittest.mock as mock

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("GITHUB_REPO", "owner/repo")
os.environ.setdefault("SQLITE_DB_PATH", "/tmp/test-dispatch-state.db")

import dispatch  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_mention
# ---------------------------------------------------------------------------

class TestParseMention:
    def test_last_line_mention(self):
        body = "Some content\n@implementer please implement."
        assert dispatch._parse_mention(body) == "implementer"

    def test_ignores_mid_body_mention(self):
        body = "@claude could you help?\nNo mention here."
        assert dispatch._parse_mention(body) is None

    def test_ignores_blockquote(self):
        body = "> @claude quoted\n@codex please review."
        # Last non-empty line is @codex, not the blockquote
        assert dispatch._parse_mention(body) == "codex"

    def test_blockquote_last_line_returns_none(self):
        body = "Some text\n> @implementer quoted"
        assert dispatch._parse_mention(body) is None

    def test_empty_body(self):
        assert dispatch._parse_mention("") is None

    def test_human_mention(self):
        body = "Something broke\n@human please review"
        assert dispatch._parse_mention(body) == "human"

    def test_trailing_whitespace_ignored(self):
        body = "@codex please review\n\n  "
        assert dispatch._parse_mention(body) == "codex"

    def test_fenced_code_block_last_line_returns_none(self):
        body = "Plan:\n```\n@codex please review\n```"
        assert dispatch._parse_mention(body) is None

    def test_mention_after_fenced_block(self):
        body = "```\nsome code\n```\n@implementer please implement"
        assert dispatch._parse_mention(body) == "implementer"

    def test_inline_backtick_span_ignored(self):
        body = "Run `@codex` to lint"
        assert dispatch._parse_mention(body) is None

    def test_scans_past_trailing_blockquote(self):
        """A mention before a trailing blockquote should still be found."""
        body = "@implementer please implement\n> quoted agent output"
        assert dispatch._parse_mention(body) == "implementer"

    def test_scans_past_trailing_fenced_block(self):
        """A mention before a trailing fenced block should still be found."""
        body = "@codex please review\n```\nsome code\n```"
        assert dispatch._parse_mention(body) == "codex"


# ---------------------------------------------------------------------------
# _parse_status
# ---------------------------------------------------------------------------

class TestParseStatus:
    def test_extracts_status(self):
        body = "Some plan\n---\nSTATUS: PLAN_COMPLETE\n@codex please review."
        assert dispatch._parse_status(body) == "PLAN_COMPLETE"

    def test_multi_word_status(self):
        body = "STATUS: PLAN_CHANGES_REQUESTED"
        assert dispatch._parse_status(body) == "PLAN_CHANGES_REQUESTED"

    def test_no_status(self):
        assert dispatch._parse_status("no status here") is None

    def test_status_with_spaces(self):
        body = "STATUS:  APPROVED"
        assert dispatch._parse_status(body) == "APPROVED"


# ---------------------------------------------------------------------------
# _handle_comment routing
# ---------------------------------------------------------------------------

class TestHandleCommentRouting:
    """Test that the transitions table routes correctly."""

    def _call(self, status, mention, issue=99, repo="owner/repo"):
        with mock.patch.object(dispatch.state, "transition", return_value=True), \
             mock.patch.object(dispatch.state, "get_review_count", return_value=0), \
             mock.patch.object(dispatch.state, "increment_review_count"), \
             mock.patch.object(dispatch.state, "escalate"), \
             mock.patch.object(dispatch.telegram, "send_notification"), \
             mock.patch.object(dispatch, "_dispatch_agent") as mock_dispatch:
            dispatch._handle_comment(
                issue_number=issue,
                repo=repo,
                issue_title="Test",
                issue_body="",
                mention=mention,
                status=status,
                comment_body="",
            )
            return mock_dispatch

    def test_plan_complete_routes_to_codex(self):
        m = self._call("PLAN_COMPLETE", "codex")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "codex"
        assert m.call_args.kwargs["stage"] == "plan_review"

    def test_plan_approved_routes_to_implementer(self):
        m = self._call("PLAN_APPROVED", "implementer")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "implementer"

    def test_implementation_complete_routes_to_codex(self):
        m = self._call("IMPLEMENTATION_COMPLETE", "codex")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "codex"
        assert m.call_args.kwargs["stage"] == "code_review"

    def test_changes_requested_routes_to_implementer(self):
        m = self._call("CHANGES_REQUESTED", "implementer")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "implementer"

    def test_ci_failing_routes_to_implementer(self):
        m = self._call("CI_FAILING", "implementer")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "implementer"

    def test_tests_failing_routes_to_codex_not_human(self):
        """TESTS_FAILING should route to @codex for code review, not escalate."""
        m = self._call("TESTS_FAILING", "codex")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "codex"

    def test_blocked_with_claude_routes_to_planner(self):
        """BLOCKED + @claude should re-route to planner, not escalate."""
        m = self._call("BLOCKED", "claude")
        m.assert_called_once()
        assert m.call_args.kwargs["agent"] == "claude"

    def test_human_mention_escalates(self):
        with mock.patch.object(dispatch.state, "escalate") as mock_esc, \
             mock.patch.object(dispatch.telegram, "send_notification"), \
             mock.patch.object(dispatch, "_dispatch_agent") as mock_dispatch:
            dispatch._handle_comment(
                issue_number=1, repo="owner/repo", issue_title="T",
                issue_body="", mention="human", status="BLOCKED",
                comment_body="",
            )
            mock_esc.assert_called_once()
            mock_dispatch.assert_not_called()

    def test_unknown_status_mention_skipped(self):
        m = self._call("GIBBERISH", "codex")
        m.assert_not_called()

    def test_stage_transition_failure_skips_dispatch(self):
        with mock.patch.object(dispatch.state, "transition", return_value=False), \
             mock.patch.object(dispatch.state, "get_review_count", return_value=0), \
             mock.patch.object(dispatch.state, "increment_review_count"), \
             mock.patch.object(dispatch, "_dispatch_agent") as mock_dispatch:
            dispatch._handle_comment(
                issue_number=1, repo="owner/repo", issue_title="T",
                issue_body="", mention="codex", status="PLAN_COMPLETE",
                comment_body="",
            )
            mock_dispatch.assert_not_called()

    def test_max_review_cycles_escalates(self):
        with mock.patch.object(dispatch.state, "transition", return_value=True), \
             mock.patch.object(dispatch.state, "get_review_count", return_value=3), \
             mock.patch.object(dispatch.state, "escalate") as mock_esc, \
             mock.patch.object(dispatch.telegram, "send_notification"), \
             mock.patch.object(dispatch, "_dispatch_agent") as mock_dispatch:
            dispatch._handle_comment(
                issue_number=1, repo="owner/repo", issue_title="T",
                issue_body="", mention="codex", status="PLAN_COMPLETE",
                comment_body="",
            )
            mock_esc.assert_called_once()
            mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Auth filtering
# ---------------------------------------------------------------------------

class TestAuthFiltering:
    """Test that agent-tagged comments require trusted authorship."""

    def _make_payload(self, commenter, association, body):
        return {
            "comment": {
                "user": {"login": commenter},
                "author_association": association,
                "body": body,
            },
            "issue": {
                "number": 1,
                "title": "Test",
                "body": "",
            },
            "repository": {"full_name": "owner/repo"},
        }

    def test_trusted_bot_agent_comment_is_routed(self):
        body = "<!-- agent:claude -->\nSTATUS: PLAN_COMPLETE\n@codex please review."
        with mock.patch.object(dispatch, "BOT_GITHUB_USERNAME", "mybot"), \
             mock.patch.object(dispatch, "_handle_comment") as mock_handle, \
             mock.patch.object(dispatch.state, "is_duplicate", return_value=False):
            payload = self._make_payload("mybot", "NONE", body)
            dispatch._route("issue_comment", "created", payload)
            mock_handle.assert_called_once()

    def test_external_spoof_agent_comment_is_rejected(self):
        body = "<!-- agent:claude -->\nSTATUS: PLAN_COMPLETE\n@codex please review."
        with mock.patch.object(dispatch, "BOT_GITHUB_USERNAME", "mybot"), \
             mock.patch.object(dispatch, "_handle_comment") as mock_handle, \
             mock.patch.object(dispatch.state, "is_duplicate", return_value=False):
            payload = self._make_payload("external-user", "NONE", body)
            dispatch._route("issue_comment", "created", payload)
            mock_handle.assert_not_called()

    def test_owner_can_send_agent_comment(self):
        body = "<!-- agent:claude -->\nSTATUS: PLAN_COMPLETE\n@codex please review."
        with mock.patch.object(dispatch, "BOT_GITHUB_USERNAME", "mybot"), \
             mock.patch.object(dispatch, "_handle_comment") as mock_handle, \
             mock.patch.object(dispatch.state, "is_duplicate", return_value=False):
            payload = self._make_payload("repo-owner", "OWNER", body)
            dispatch._route("issue_comment", "created", payload)
            mock_handle.assert_called_once()

    def test_non_agent_self_comment_ignored(self):
        """Bot's own non-agent comments (e.g. status updates) should not loop."""
        body = "Processing your request..."
        with mock.patch.object(dispatch, "BOT_GITHUB_USERNAME", "mybot"), \
             mock.patch.object(dispatch, "_handle_comment") as mock_handle, \
             mock.patch.object(dispatch.state, "is_duplicate", return_value=False):
            payload = self._make_payload("mybot", "NONE", body)
            dispatch._route("issue_comment", "created", payload)
            mock_handle.assert_not_called()


# ---------------------------------------------------------------------------
# PR-branch resolution failure — zombie run prevention (Finding #1 fix)
# ---------------------------------------------------------------------------

class TestCodeReviewBranchResolution:
    """PR branch must be resolved before enqueue; no row is created on failure."""

    def _dispatch_code_review(self, pr_branch_return):
        """Call _dispatch_agent for a code-review; return (mock_enqueue, mock_cancel)."""
        with mock.patch.object(dispatch.state, "enqueue_run", return_value=42) as mock_enqueue, \
             mock.patch.object(dispatch.state, "cancel_queued_run") as mock_cancel, \
             mock.patch.object(dispatch.state, "try_promote", return_value=None), \
             mock.patch.object(dispatch, "_fetch_pr_branch", return_value=pr_branch_return), \
             mock.patch.object(dispatch, "_fetch_pr_context", return_value=(99, "diff", "desc")), \
             mock.patch.object(dispatch, "_fetch_comments", return_value=[]), \
             mock.patch.object(dispatch.reviewer_prompt, "build", return_value="/tmp/prompt.md"):
            dispatch._dispatch_agent(
                agent="codex",
                issue_number=10,
                repo="owner/repo",
                issue_title="Test",
                issue_body="",
                stage="code_review",
                comment_body="",
            )
            return mock_enqueue, mock_cancel

    def test_missing_pr_branch_never_enqueues(self):
        """When _fetch_pr_branch returns None, no run row is created at all."""
        mock_enqueue, mock_cancel = self._dispatch_code_review(pr_branch_return=None)
        mock_enqueue.assert_not_called()
        mock_cancel.assert_not_called()

    def test_successful_branch_enqueues_with_branch(self):
        """When PR branch resolves, enqueue_run is called with pr_branch set."""
        mock_enqueue, mock_cancel = self._dispatch_code_review(pr_branch_return="feature/my-branch")
        mock_enqueue.assert_called_once()
        _, kwargs = mock_enqueue.call_args
        assert kwargs.get("pr_branch") == "feature/my-branch"
        mock_cancel.assert_not_called()
