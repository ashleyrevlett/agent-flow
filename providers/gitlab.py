"""
providers/gitlab.py — GitLab implementation of GitProvider.
Uses glab CLI for API calls. Supports self-managed instances via GIT_BASE_URL.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from provider import WebhookEvent

logger = logging.getLogger(__name__)

_STATUS_RE = re.compile(r"STATUS:\s*(\w+(?:_\w+)*)")

# Trust cache: (repo, user_id) -> (trusted, expires_at)
_TRUST_CACHE: dict[tuple[str, int], tuple[bool, float]] = {}
_TRUST_CACHE_TTL = 300  # 5 minutes


class GitLabProvider:
    def __init__(self):
        from config import WEBHOOK_SECRET, GIT_REPO, BOT_USERNAME, GIT_BASE_URL
        self._secret = WEBHOOK_SECRET
        self._repo = GIT_REPO
        self._bot_username = BOT_USERNAME
        self._base_url = GIT_BASE_URL

    def _cli_env(self) -> dict[str, str]:
        """Return env dict with GITLAB_TOKEN and optional GITLAB_HOST for CLI auth.

        glab uses GITLAB_HOST for the instance URL. The exact format accepted
        (full URL vs hostname-only) should be verified during setup — see
        PLAN-gitlab-support.md for verification steps. glab generally expects
        the full URL form (https://gitlab.example.com).
        """
        from config import API_TOKEN
        env = dict(os.environ)
        # Ensure glab CLI can authenticate even if user only set API_TOKEN
        if API_TOKEN and "GITLAB_TOKEN" not in env:
            env["GITLAB_TOKEN"] = API_TOKEN
        if self._base_url:
            env["GITLAB_HOST"] = self._base_url
        return env

    def _host_prefix(self) -> str:
        """Shell prefix for CLI templates embedded in agent prompts."""
        if not self._base_url:
            return ""
        return f"GITLAB_HOST={self._base_url} "

    def _encoded_repo(self, repo: str) -> str:
        return urllib.parse.quote_plus(repo)

    @staticmethod
    def _parse_paginated_json(raw: str) -> list:
        """Parse potentially concatenated JSON arrays from --paginate output.

        glab --paginate may output multiple JSON arrays concatenated without
        a separator, just like gh. This uses raw_decode to handle that.
        """
        if not raw:
            return []
        objects: list = []
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \t\n\r":
                pos += 1
            if pos >= len(raw):
                break
            obj, end = decoder.raw_decode(raw, pos)
            if isinstance(obj, list):
                objects.extend(obj)
            else:
                objects.append(obj)
            pos = end
        return objects

    # --- Trust ---

    def _is_trusted(self, repo: str, username: str, user_id: int) -> bool:
        """Check project membership via direct API lookup with TTL cache."""
        if self._bot_username and username == self._bot_username:
            return True

        # Evict expired entries periodically to prevent unbounded growth
        now = time.time()
        if len(_TRUST_CACHE) > 500:
            expired = [k for k, (_, exp) in _TRUST_CACHE.items() if exp <= now]
            for k in expired:
                del _TRUST_CACHE[k]

        cache_key = (repo, user_id)
        cached = _TRUST_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

        # Direct lookup: GET /projects/:id/members/all/:user_id
        # Returns 200 with access_level if member, 404 if not
        encoded = self._encoded_repo(repo)
        try:
            result = subprocess.run(
                ["glab", "api", f"projects/{encoded}/members/all/{user_id}"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            member = json.loads(result.stdout)
            trusted = member.get("access_level", 0) >= 30  # 30 = Developer
        except subprocess.CalledProcessError:
            trusted = False
        except (json.JSONDecodeError, TypeError):
            trusted = False

        _TRUST_CACHE[cache_key] = (trusted, time.time() + _TRUST_CACHE_TTL)
        return trusted

    # --- Dedup ---

    def _make_delivery_id(self, payload: dict, event_type: str) -> str:
        """Generate a stable dedup key from per-event canonical fields."""
        obj = payload.get("object_attributes", {})
        project_id = str(payload.get("project", {}).get("id", ""))
        obj_id = str(obj.get("id", ""))
        action = obj.get("action", "")

        # Only include timestamp for close events to disambiguate reopen/reclose.
        disambiguator = ""
        if action == "close":
            disambiguator = obj.get("closed_at", "")

        parts = [event_type, project_id, obj_id, action, disambiguator]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    # --- Webhook ---

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self._secret:
            return False
        token = headers.get("x-gitlab-token", "")
        if not token:
            return False
        return hmac.compare_digest(token, self._secret)

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> Optional[WebhookEvent]:
        event_type = headers.get("x-gitlab-event", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None

        delivery_id = self._make_delivery_id(payload, event_type)
        project_path = payload.get("project", {}).get("path_with_namespace", self._repo)
        user = payload.get("user", {})
        username = user.get("username", "")
        user_id = user.get("id", 0)

        # --- Note Hook (issue comment) ---
        if event_type == "Note Hook":
            obj = payload.get("object_attributes", {})
            noteable_type = obj.get("noteable_type", "")
            if noteable_type != "Issue":
                return None  # Only route issue notes, not MR notes

            issue = payload.get("issue", {})
            comment_body = obj.get("note", "") or ""
            is_agent = "<!-- agent:" in comment_body

            return WebhookEvent(
                kind="comment_created",
                delivery_id=delivery_id,
                repo=project_path,
                issue_number=issue.get("iid", 0),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("description", "") or "",
                comment_body=comment_body,
                commenter=username,
                is_trusted=self._is_trusted(project_path, username, user_id),
                is_bot=bool(self._bot_username and username == self._bot_username),
                is_agent_comment=is_agent,
            )

        # --- Issue Hook ---
        if event_type == "Issue Hook":
            obj = payload.get("object_attributes", {})
            action = obj.get("action", "")

            if action == "open":
                return WebhookEvent(
                    kind="issue_opened",
                    delivery_id=delivery_id,
                    repo=project_path,
                    issue_number=obj.get("iid", 0),
                    issue_title=obj.get("title", ""),
                    issue_body=obj.get("description", "") or "",
                    comment_body=None,
                    commenter=username,
                    is_trusted=True,
                    is_bot=bool(self._bot_username and username == self._bot_username),
                    is_agent_comment=False,
                )

            if action == "close":
                return WebhookEvent(
                    kind="issue_closed",
                    delivery_id=delivery_id,
                    repo=project_path,
                    issue_number=obj.get("iid", 0),
                    issue_title=obj.get("title", ""),
                    issue_body=obj.get("description", "") or "",
                    comment_body=None,
                    commenter=None,
                    is_trusted=True,
                    is_bot=False,
                    is_agent_comment=False,
                )

        # --- Pipeline Hook (terminal statuses only) ---
        if event_type == "Pipeline Hook":
            obj = payload.get("object_attributes", {})
            conclusion = obj.get("status", "unknown")
            if conclusion not in ("success", "failed", "canceled", "skipped"):
                return None  # Ignore non-terminal statuses (pending, running, created)
            return WebhookEvent(
                kind="workflow_completed",
                delivery_id=delivery_id,
                repo=project_path,
                issue_number=0,
                issue_title="",
                issue_body="",
                comment_body=conclusion,
                commenter=None,
                is_trusted=True,
                is_bot=False,
                is_agent_comment=False,
            )

        return None

    # --- API ---

    def fetch_comments(self, repo: str, issue_number: int) -> list[dict]:
        """Fetch issue notes, normalized to GitHub comment shape for _format_thread()."""
        encoded = self._encoded_repo(repo)
        try:
            result = subprocess.run(
                ["glab", "api", f"projects/{encoded}/issues/{issue_number}/notes",
                 "--paginate"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            notes = self._parse_paginated_json(result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to fetch notes for #%s: %s", issue_number, exc)
            return []

        # Normalize to {"user": {"login": ...}, "body": ..., "created_at": ...}
        return [
            {
                "user": {"login": n.get("author", {}).get("username", "unknown")},
                "body": n.get("body", ""),
                "created_at": n.get("created_at", ""),
            }
            for n in notes
            if not n.get("system", False)  # Skip system notes (label changes, etc.)
        ]

    def fetch_mr_context(self, repo: str, issue_number: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
        """Find MR(s) that will close this issue via GitLab's closing references API."""
        encoded = self._encoded_repo(repo)
        try:
            result = subprocess.run(
                ["glab", "api", f"projects/{encoded}/issues/{issue_number}/closed_by"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            mrs = json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return None, None, None

        if not mrs or not isinstance(mrs, list):
            return None, None, None

        # Sort by updated_at descending — parse to datetime for correctness
        def _sort_key(m: dict) -> datetime:
            return self._parse_iso_ts(m.get("updated_at", ""))

        open_mrs = sorted(
            [m for m in mrs if m.get("state") == "opened"],
            key=_sort_key, reverse=True,
        )
        mr = open_mrs[0] if open_mrs else sorted(mrs, key=_sort_key, reverse=True)[0]
        mr_iid = mr["iid"]
        description = mr.get("description", "")

        # Fetch diff
        try:
            diff_result = subprocess.run(
                ["glab", "mr", "diff", str(mr_iid), "--repo", repo],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            diff = diff_result.stdout
        except subprocess.CalledProcessError:
            diff = None

        return mr_iid, diff, description

    def fetch_mr_branch(self, repo: str, mr_iid: int) -> Optional[str]:
        encoded = self._encoded_repo(repo)
        try:
            result = subprocess.run(
                ["glab", "api", f"projects/{encoded}/merge_requests/{mr_iid}"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            data = json.loads(result.stdout)
            return data.get("source_branch")
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning("Failed to fetch MR branch for MR !%s: %s", mr_iid, exc)
            return None

    @staticmethod
    def _parse_iso_ts(ts: str) -> datetime:
        """Parse an ISO 8601 timestamp to a timezone-aware datetime.

        Handles both "Z" and "+00:00" suffixes, and fractional seconds.
        Returns datetime.min (UTC) on parse failure so comparisons are safe.
        """
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    def check_completion(self, repo: str, issue_number: int, agent: str, since_iso: str) -> tuple[bool, Optional[str]]:
        """Poll issue notes for an agent completion comment posted after since_iso."""
        encoded = self._encoded_repo(repo)
        try:
            result = subprocess.run(
                ["glab", "api", f"projects/{encoded}/issues/{issue_number}/notes",
                 "--paginate"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            notes = self._parse_paginated_json(result.stdout.strip())
        except subprocess.CalledProcessError:
            return False, None

        # Parse since_iso to datetime for reliable comparison across
        # timestamp formats ("Z" vs "+00:00", fractional seconds).
        since_dt = self._parse_iso_ts(since_iso)

        agent_tag = f"<!-- agent:{agent} -->"
        for note in reversed(notes):  # most recent first
            body = note.get("body", "")
            if agent_tag not in body:
                continue
            created_dt = self._parse_iso_ts(note.get("created_at", ""))
            if created_dt <= since_dt:
                continue
            match = _STATUS_RE.search(body)
            if match:
                return True, match.group(1)
        return False, None

    def create_issue(self, repo: str, title: str, body: str) -> str:
        result = subprocess.run(
            ["glab", "issue", "create", "--repo", repo,
             "--title", title, "--description", body],
            capture_output=True, text=True, check=True, env=self._cli_env(),
        )
        return result.stdout.strip()

    def issue_url(self, repo: str, issue_number: int) -> str:
        base = self._base_url or "https://gitlab.com"
        return f"{base}/{repo}/-/issues/{issue_number}"

    # --- CLI templates for agent prompts ---
    # Agents write comment/description bodies to files in TMP_DIR (absolute
    # path inside the agent-flow project), then pass the file to the CLI.
    # This avoids shell quoting issues and works regardless of the agent's cwd.

    def comment_cli(self, issue_number: int, repo: str) -> str:
        from config import TMP_DIR
        p = self._host_prefix()
        f = f"{TMP_DIR}/comment-{issue_number}.md"
        return (
            f"Write your comment body to {f}, then run:\n"
            f"`{p}glab issue note {issue_number} --repo {repo} -m \"$(cat {f})\"`"
        )

    def mr_create_cli(self, issue_number: int, repo: str) -> str:
        from config import TMP_DIR
        p = self._host_prefix()
        f = f"{TMP_DIR}/pr-body-{issue_number}.md"
        return (
            f"Write your MR description to {f}, then run:\n"
            f"`{p}glab mr create --repo {repo} --description \"$(cat {f})\" --title \"<title>\"`"
        )

    def mr_merge_cli(self, mr_iid: int, repo: str) -> str:
        return f"{self._host_prefix()}glab mr merge {mr_iid} --repo {repo} --squash"

    def mr_checks_cli(self, mr_iid: int, repo: str) -> str:
        p = self._host_prefix()
        return f"{p}glab ci status --repo {repo} --branch $(glab mr view {mr_iid} --repo {repo} -F json | jq -r '.source_branch')"

    def issue_link_syntax(self, issue_number: int) -> str:
        return f"Closes #{issue_number}"
