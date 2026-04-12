"""
provider.py — Git platform abstraction. Defines the WebhookEvent dataclass,
GitProvider protocol, and factory function for selecting providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Provider-agnostic webhook event, normalized at the boundary."""
    kind: str               # "issue_opened" | "comment_created" | "issue_closed" | "workflow_completed"
    delivery_id: str        # GitHub header or GitLab composite hash
    repo: str               # "owner/repo" or "group/project"
    issue_number: int       # GitHub issue number or GitLab issue iid (project-scoped)
    issue_title: str
    issue_body: str
    comment_body: Optional[str]
    commenter: Optional[str]
    is_trusted: bool        # OWNER/MEMBER/COLLABORATOR or access_level >= 30
    is_bot: bool            # commenter == BOT_USERNAME
    is_agent_comment: bool  # body contains <!-- agent:... -->


class GitProvider(Protocol):
    """Minimum viable interface for git platform operations."""

    # --- Webhook ---
    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> Optional[WebhookEvent]: ...

    # --- API (all return normalized shapes) ---
    def fetch_comments(self, repo: str, issue_number: int) -> list[dict]: ...
    def fetch_mr_context(self, repo: str, issue_number: int) -> tuple[Optional[int], Optional[str], Optional[str]]: ...
    def fetch_mr_branch(self, repo: str, mr_iid: int) -> Optional[str]: ...
    def check_completion(self, repo: str, issue_number: int, agent: str, since_iso: str) -> tuple[bool, Optional[str]]: ...
    def create_issue(self, repo: str, title: str, body: str) -> str: ...
    def issue_url(self, repo: str, issue_number: int) -> str: ...

    # --- CLI templates for generated agent prompts ---
    def comment_cli(self, issue_number: int, repo: str) -> str: ...
    def mr_create_cli(self, issue_number: int, repo: str) -> str: ...
    def mr_merge_cli(self, mr_iid: int, repo: str) -> str: ...
    def mr_checks_cli(self, mr_iid: int, repo: str) -> str: ...
    def issue_link_syntax(self, issue_number: int) -> str: ...


def get_provider() -> GitProvider:
    """Factory. Selects provider based on GIT_PROVIDER config."""
    from config import GIT_PROVIDER
    if GIT_PROVIDER == "github":
        from providers.github import GitHubProvider
        return GitHubProvider()
    elif GIT_PROVIDER == "gitlab":
        from providers.gitlab import GitLabProvider
        return GitLabProvider()
    raise ValueError(f"Unknown GIT_PROVIDER: {GIT_PROVIDER!r}. Use 'github' or 'gitlab'.")
