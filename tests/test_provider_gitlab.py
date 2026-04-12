"""
Tests for providers/gitlab.py — webhook parsing, verification, dedup, URL generation.
"""

import json
import os

os.environ.setdefault("WEBHOOK_SECRET", "gl-test-secret")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "gl-test-secret")
os.environ.setdefault("API_TOKEN", "gl-test-token")
os.environ.setdefault("GITHUB_TOKEN", "gl-test-token")
os.environ.setdefault("GIT_REPO", "group/project")
os.environ.setdefault("GITHUB_REPO", "group/project")
os.environ.setdefault("SQLITE_DB_PATH", "/tmp/test-gl-provider.db")

from providers.gitlab import GitLabProvider


def _make_note_hook_payload(username="testuser", user_id=100, note="hello", issue_iid=5):
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"id": user_id, "username": username},
        "project": {"id": 999, "path_with_namespace": "group/project"},
        "object_attributes": {
            "id": 12345,
            "note": note,
            "noteable_type": "Issue",
            "action": "create",
        },
        "issue": {
            "iid": issue_iid,
            "title": "Test Issue",
            "description": "issue description",
        },
    }


def _make_issue_hook_payload(action="open", issue_iid=3, username="testuser", user_id=100):
    return {
        "object_kind": "issue",
        "event_type": "issue",
        "user": {"id": user_id, "username": username},
        "project": {"id": 999, "path_with_namespace": "group/project"},
        "object_attributes": {
            "id": 54321,
            "iid": issue_iid,
            "title": "New Issue",
            "description": "new issue body",
            "action": action,
        },
    }


class TestGitLabVerifyWebhook:
    def test_valid_token_passes(self):
        p = GitLabProvider()
        p._secret = "my-gl-secret"
        assert p.verify_webhook(b"body", {"x-gitlab-token": "my-gl-secret"}) is True

    def test_wrong_token_fails(self):
        p = GitLabProvider()
        p._secret = "my-gl-secret"
        assert p.verify_webhook(b"body", {"x-gitlab-token": "wrong"}) is False

    def test_missing_token_fails(self):
        p = GitLabProvider()
        p._secret = "my-gl-secret"
        assert p.verify_webhook(b"body", {}) is False

    def test_empty_secret_fails(self):
        p = GitLabProvider()
        p._secret = ""
        assert p.verify_webhook(b"body", {"x-gitlab-token": ""}) is False


class TestGitLabParseWebhook:
    def test_note_hook_issue_comment(self):
        p = GitLabProvider()
        p._bot_username = ""  # no bot for this test
        payload = _make_note_hook_payload(
            note="<!-- agent:claude -->\nSTATUS: PLAN_COMPLETE\n@codex please review",
        )
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Note Hook"}

        # Mock trust lookup to avoid subprocess
        p._is_trusted = lambda repo, username, user_id: True

        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "comment_created"
        assert event.issue_number == 5  # iid, not global id
        assert event.commenter == "testuser"
        assert event.is_agent_comment is True

    def test_extracts_iid_not_id(self):
        p = GitLabProvider()
        p._bot_username = ""
        payload = _make_note_hook_payload(issue_iid=77)
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Note Hook"}
        p._is_trusted = lambda repo, username, user_id: True

        event = p.parse_webhook(body, headers)
        assert event.issue_number == 77

    def test_mr_note_returns_none(self):
        p = GitLabProvider()
        payload = _make_note_hook_payload()
        payload["object_attributes"]["noteable_type"] = "MergeRequest"
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Note Hook"}
        assert p.parse_webhook(body, headers) is None

    def test_issue_opened(self):
        p = GitLabProvider()
        payload = _make_issue_hook_payload(action="open", issue_iid=10)
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Issue Hook"}

        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "issue_opened"
        assert event.issue_number == 10

    def test_issue_closed(self):
        p = GitLabProvider()
        payload = _make_issue_hook_payload(action="close", issue_iid=10)
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Issue Hook"}

        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "issue_closed"

    def test_pipeline_non_terminal_returns_none(self):
        p = GitLabProvider()
        payload = {
            "object_kind": "pipeline",
            "user": {"id": 1, "username": "bot"},
            "project": {"id": 999, "path_with_namespace": "group/project"},
            "object_attributes": {"id": 100, "status": "running", "action": ""},
        }
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Pipeline Hook"}
        assert p.parse_webhook(body, headers) is None

    def test_pipeline_terminal_emits_event(self):
        p = GitLabProvider()
        payload = {
            "object_kind": "pipeline",
            "user": {"id": 1, "username": "bot"},
            "project": {"id": 999, "path_with_namespace": "group/project"},
            "object_attributes": {"id": 100, "status": "failed", "action": ""},
        }
        body = json.dumps(payload).encode()
        headers = {"x-gitlab-event": "Pipeline Hook"}
        event = p.parse_webhook(body, headers)
        assert event is not None
        assert event.kind == "workflow_completed"
        assert event.comment_body == "failed"

    def test_unhandled_event_returns_none(self):
        p = GitLabProvider()
        body = json.dumps({"object_kind": "push"}).encode()
        headers = {"x-gitlab-event": "Push Hook"}
        assert p.parse_webhook(body, headers) is None


class TestGitLabDedup:
    def test_same_payload_same_hash(self):
        p = GitLabProvider()
        payload = _make_note_hook_payload()
        id1 = p._make_delivery_id(payload, "Note Hook")
        id2 = p._make_delivery_id(payload, "Note Hook")
        assert id1 == id2

    def test_different_note_id_different_hash(self):
        p = GitLabProvider()
        p1 = _make_note_hook_payload()
        p2 = _make_note_hook_payload()
        p2["object_attributes"]["id"] = 99999
        id1 = p._make_delivery_id(p1, "Note Hook")
        id2 = p._make_delivery_id(p2, "Note Hook")
        assert id1 != id2

    def test_different_event_type_different_hash(self):
        p = GitLabProvider()
        payload = _make_note_hook_payload()
        id1 = p._make_delivery_id(payload, "Note Hook")
        id2 = p._make_delivery_id(payload, "Issue Hook")
        assert id1 != id2


class TestGitLabIssueUrl:
    def test_default_base(self):
        p = GitLabProvider()
        p._base_url = ""
        assert p.issue_url("group/project", 7) == "https://gitlab.com/group/project/-/issues/7"

    def test_custom_base(self):
        p = GitLabProvider()
        p._base_url = "https://gitlab.company.com"
        assert p.issue_url("group/project", 7) == "https://gitlab.company.com/group/project/-/issues/7"


class TestGitLabPaginatedParsing:
    def test_single_array(self):
        raw = '[{"id": 1}, {"id": 2}]'
        assert len(GitLabProvider._parse_paginated_json(raw)) == 2

    def test_concatenated_arrays(self):
        raw = '[{"id": 1}][{"id": 2}, {"id": 3}]'
        assert len(GitLabProvider._parse_paginated_json(raw)) == 3

    def test_empty_string(self):
        assert GitLabProvider._parse_paginated_json("") == []
