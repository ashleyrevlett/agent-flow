"""
Tests for providers/github.py — webhook parsing, verification, URL generation.
"""

import hashlib
import hmac
import json
import os
import unittest.mock as mock

os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("API_TOKEN", "test-token")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GIT_REPO", "owner/repo")
os.environ.setdefault("GITHUB_REPO", "owner/repo")
os.environ.setdefault("SQLITE_DB_PATH", "/tmp/test-gh-provider.db")

from providers.github import GitHubProvider


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_issue_comment_payload(commenter="testuser", body="hello", association="MEMBER"):
    return {
        "action": "created",
        "comment": {
            "user": {"login": commenter},
            "author_association": association,
            "body": body,
        },
        "issue": {
            "number": 42,
            "title": "Test Issue",
            "body": "issue body",
        },
        "repository": {"full_name": "owner/repo"},
    }


def _make_issue_opened_payload(opener="testuser"):
    return {
        "action": "opened",
        "issue": {
            "number": 7,
            "title": "New Issue",
            "body": "new issue body",
            "user": {"login": opener},
        },
        "repository": {"full_name": "owner/repo"},
    }


class TestGitHubVerifyWebhook:
    def test_valid_signature_passes(self):
        p = GitHubProvider()
        p._secret = "my-secret"
        body = b'{"test": true}'
        sig = _sign(body, "my-secret")
        assert p.verify_webhook(body, {"x-hub-signature-256": sig}) is True

    def test_wrong_signature_fails(self):
        p = GitHubProvider()
        p._secret = "my-secret"
        body = b'{"test": true}'
        assert p.verify_webhook(body, {"x-hub-signature-256": "sha256=wrong"}) is False

    def test_missing_signature_fails(self):
        p = GitHubProvider()
        p._secret = "my-secret"
        assert p.verify_webhook(b"body", {}) is False

    def test_empty_secret_fails(self):
        p = GitHubProvider()
        p._secret = ""
        assert p.verify_webhook(b"body", {"x-hub-signature-256": "sha256=anything"}) is False


class TestGitHubParseWebhook:
    def test_issue_comment_created(self):
        p = GitHubProvider()
        payload = _make_issue_comment_payload(
            commenter="mybot", body="<!-- agent:claude -->\nSTATUS: PLAN_COMPLETE", association="NONE",
        )
        body = json.dumps(payload).encode()
        headers = {"x-github-event": "issue_comment", "x-github-delivery": "del-123"}

        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "comment_created"
        assert event.delivery_id == "del-123"
        assert event.issue_number == 42
        assert event.commenter == "mybot"
        assert event.is_agent_comment is True

    def test_issues_opened(self):
        p = GitHubProvider()
        payload = _make_issue_opened_payload(opener="someuser")
        body = json.dumps(payload).encode()
        headers = {"x-github-event": "issues", "x-github-delivery": "del-456"}

        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "issue_opened"
        assert event.issue_number == 7
        assert event.commenter == "someuser"

    def test_unhandled_event_returns_none(self):
        p = GitHubProvider()
        body = json.dumps({"action": "labeled"}).encode()
        headers = {"x-github-event": "label", "x-github-delivery": "del-789"}
        assert p.parse_webhook(body, headers) is None

    def test_trust_owner(self):
        p = GitHubProvider()
        payload = _make_issue_comment_payload(association="OWNER")
        body = json.dumps(payload).encode()
        headers = {"x-github-event": "issue_comment", "x-github-delivery": "d1"}
        event = p.parse_webhook(body, headers)
        assert event.is_trusted is True

    def test_trust_none_association(self):
        p = GitHubProvider()
        payload = _make_issue_comment_payload(commenter="rando", association="NONE")
        body = json.dumps(payload).encode()
        headers = {"x-github-event": "issue_comment", "x-github-delivery": "d2"}
        event = p.parse_webhook(body, headers)
        assert event.is_trusted is False


class TestGitHubIssueUrl:
    def test_default_base(self):
        p = GitHubProvider()
        p._base_url = ""
        assert p.issue_url("owner/repo", 42) == "https://github.com/owner/repo/issues/42"

    def test_custom_base(self):
        p = GitHubProvider()
        p._base_url = "https://github.example.com"
        assert p.issue_url("owner/repo", 42) == "https://github.example.com/owner/repo/issues/42"


class TestGitHubCliEnv:
    def test_injects_gh_token(self):
        p = GitHubProvider()
        with mock.patch.dict(os.environ, {}, clear=True):
            env = p._cli_env()
            assert "GH_TOKEN" in env or "GITHUB_TOKEN" in env
