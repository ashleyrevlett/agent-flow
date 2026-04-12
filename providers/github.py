"""
providers/github.py — GitHub implementation of GitProvider.
Extracts all existing gh CLI calls and payload parsing from dispatch/monitor/notifications.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
from typing import Optional

from provider import WebhookEvent

logger = logging.getLogger(__name__)

_AGENT_TAG_RE = re.compile(r"<!--\s*agent:(\w+)\s*-->")
_STATUS_RE = re.compile(r"STATUS:\s*(\w+(?:_\w+)*)")


class GitHubProvider:
    def __init__(self):
        from config import WEBHOOK_SECRET, GIT_REPO, BOT_USERNAME, GIT_BASE_URL, TRUSTED_AUTHOR_ASSOCIATIONS
        self._secret = WEBHOOK_SECRET
        self._repo = GIT_REPO
        self._bot_username = BOT_USERNAME
        self._base_url = GIT_BASE_URL
        self._trusted_associations = TRUSTED_AUTHOR_ASSOCIATIONS

    def _cli_env(self) -> dict[str, str]:
        """Return env dict with GH_TOKEN and optional GH_HOST for CLI auth."""
        from config import API_TOKEN
        env = dict(os.environ)
        # Ensure gh CLI can authenticate even if user only set API_TOKEN
        if API_TOKEN and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env:
            env["GH_TOKEN"] = API_TOKEN
        if self._base_url:
            # gh expects hostname-only for GH_HOST
            env["GH_HOST"] = self._base_url.replace("https://", "").replace("http://", "").rstrip("/")
        return env

    # --- Webhook ---

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self._secret:
            return False
        sig_header = headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            self._secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig_header)

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> Optional[WebhookEvent]:
        event_type = headers.get("x-github-event", "")
        delivery_id = headers.get("x-github-delivery", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None

        action = payload.get("action", "")
        repo = payload.get("repository", {}).get("full_name", self._repo)

        # --- issues.opened ---
        if event_type == "issues" and action == "opened":
            issue = payload.get("issue", {})
            opener = issue.get("user", {}).get("login", "")
            return WebhookEvent(
                kind="issue_opened",
                delivery_id=delivery_id,
                repo=repo,
                issue_number=issue.get("number", 0),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("body", "") or "",
                comment_body=None,
                commenter=opener,
                is_trusted=True,  # opener trust not checked for issue_opened
                is_bot=bool(self._bot_username and opener == self._bot_username),
                is_agent_comment=False,
            )

        # --- issue_comment.created ---
        if event_type == "issue_comment" and action == "created":
            comment = payload.get("comment", {})
            issue = payload.get("issue", {})
            comment_body = comment.get("body", "") or ""
            commenter = comment.get("user", {}).get("login", "")
            author_association = comment.get("author_association", "")
            is_agent = "<!-- agent:" in comment_body

            is_trusted = (
                (bool(self._bot_username) and commenter == self._bot_username)
                or author_association in self._trusted_associations
            )

            return WebhookEvent(
                kind="comment_created",
                delivery_id=delivery_id,
                repo=repo,
                issue_number=issue.get("number", 0),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("body", "") or "",
                comment_body=comment_body,
                commenter=commenter,
                is_trusted=is_trusted,
                is_bot=bool(self._bot_username and commenter == self._bot_username),
                is_agent_comment=is_agent,
            )

        # --- issues.closed ---
        if event_type == "issues" and action == "closed":
            issue = payload.get("issue", {})
            return WebhookEvent(
                kind="issue_closed",
                delivery_id=delivery_id,
                repo=repo,
                issue_number=issue.get("number", 0),
                issue_title=issue.get("title", ""),
                issue_body=issue.get("body", "") or "",
                comment_body=None,
                commenter=None,
                is_trusted=True,
                is_bot=False,
                is_agent_comment=False,
            )

        # --- workflow_run.completed ---
        if event_type == "workflow_run" and action == "completed":
            conclusion = payload.get("workflow_run", {}).get("conclusion", "unknown")
            return WebhookEvent(
                kind="workflow_completed",
                delivery_id=delivery_id,
                repo=repo,
                issue_number=0,
                issue_title="",
                issue_body="",
                comment_body=conclusion,  # stash conclusion in comment_body
                commenter=None,
                is_trusted=True,
                is_bot=False,
                is_agent_comment=False,
            )

        return None  # Unhandled event type

    # --- API ---

    def fetch_comments(self, repo: str, issue_number: int) -> list[dict]:
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
                 "--paginate"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            raw = result.stdout.strip()
            if not raw:
                return []
            objects: list[dict] = []
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
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning("Failed to fetch comments for #%s: %s", issue_number, exc)
            return []

    def fetch_mr_context(self, repo: str, issue_number: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", repo,
                 "--search", f"Closes #{issue_number}",
                 "--json", "number,title,body,headRefName"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            prs = json.loads(result.stdout or "[]")
            if not prs:
                return None, None, None
            pr = prs[0]
            pr_number = pr["number"]
            pr_description = pr.get("body", "")

            diff_result = subprocess.run(
                ["gh", "pr", "diff", str(pr_number), "--repo", repo],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            return pr_number, diff_result.stdout, pr_description
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to fetch PR context for #%s: %s", issue_number, exc.stderr)
            return None, None, None

    def fetch_mr_branch(self, repo: str, mr_iid: int) -> Optional[str]:
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(mr_iid), "--repo", repo,
                 "--json", "headRefName"],
                capture_output=True, text=True, check=True, env=self._cli_env(),
            )
            data = json.loads(result.stdout)
            return data.get("headRefName")
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.warning("Failed to fetch PR branch for PR #%s: %s", mr_iid, exc)
            return None

    def check_completion(self, repo: str, issue_number: int, agent: str, since_iso: str) -> tuple[bool, Optional[str]]:
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
                 "--jq",
                 f'[.[] | select(.body | contains("<!-- agent:{agent} -->"))'
                 f' | select(.created_at > "{since_iso}")] | last'],
                capture_output=True, text=True, check=True, env=self._cli_env(),
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

    def create_issue(self, repo: str, title: str, body: str) -> str:
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo,
             "--title", title, "--body", body],
            capture_output=True, text=True, check=True, env=self._cli_env(),
        )
        return result.stdout.strip()

    def issue_url(self, repo: str, issue_number: int) -> str:
        base = self._base_url or "https://github.com"
        return f"{base}/{repo}/issues/{issue_number}"

    # --- CLI templates for agent prompts ---

    def comment_cli(self, issue_number: int, repo: str) -> str:
        prefix = f"GH_HOST={self._base_url.replace('https://', '').replace('http://', '').rstrip('/')} " if self._base_url else ""
        return f'{prefix}gh issue comment {issue_number} --repo {repo} --body "..."'

    def mr_create_cli(self, repo: str) -> str:
        prefix = f"GH_HOST={self._base_url.replace('https://', '').replace('http://', '').rstrip('/')} " if self._base_url else ""
        return f'{prefix}gh pr create --repo {repo} ...'

    def mr_merge_cli(self, mr_iid: int, repo: str) -> str:
        prefix = f"GH_HOST={self._base_url.replace('https://', '').replace('http://', '').rstrip('/')} " if self._base_url else ""
        return f"{prefix}gh pr merge {mr_iid} --repo {repo} --squash --delete-branch"

    def mr_checks_cli(self, mr_iid: int, repo: str) -> str:
        prefix = f"GH_HOST={self._base_url.replace('https://', '').replace('http://', '').rstrip('/')} " if self._base_url else ""
        return f"{prefix}gh pr checks {mr_iid} --repo {repo} --required --watch"

    def issue_link_syntax(self, issue_number: int) -> str:
        return f"Closes #{issue_number}"
