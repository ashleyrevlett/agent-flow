"""
prompts/implementer.py — Build the per-invocation task file for @implementer.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from config import PROMPT_DIR


def build(
    issue_number: int,
    repo: str,
    issue_title: str,
    issue_body: str,
    comment_thread: list[dict],
    pr_number: int | None = None,
    pr_diff: str | None = None,
    pr_description: str | None = None,
    review_comments: str | None = None,
) -> str:
    """
    Write a task prompt file for the implementer. Returns the file path.

    When invoked for initial implementation: pr_number, pr_diff, review_comments are None.
    When invoked to address review feedback: all PR fields are populated.
    """
    os.makedirs(PROMPT_DIR, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = str(Path(PROMPT_DIR) / f"implementer-{issue_number}-{ts}.md")

    thread_text = _format_thread(comment_thread)

    # Extract planner's plan from comment thread (last <!-- agent:claude --> comment)
    plan_text = _extract_agent_comment(comment_thread, "agent:claude")

    review_section = ""
    if review_comments:
        review_section = f"""
## Review Feedback to Address

{review_comments}

---
"""

    pr_section = ""
    if pr_number:
        pr_section = f"""
## Existing PR

PR #{pr_number}
{f"Branch: see PR" if pr_number else ""}
{f"Description: {pr_description}" if pr_description else ""}
"""

    content = f"""# Implementer Task — Issue #{issue_number}

Repo: {repo}
Issue: #{issue_number} — {issue_title}
{pr_section}
## Issue Body

{issue_body or "(no body)"}

## The Plan

{plan_text or "(see comment thread below for planner's plan)"}

## Comment Thread

{thread_text or "(no comments)"}
{review_section}
---

## Your Task

Implement the plan above following the instructions in your system prompt (roles/implementer.md).

Key reminders:
- Branch: `issue-{issue_number}-<short-description>` from latest main
- Open a PR with `gh pr create --repo {repo} ...` with "Closes #{issue_number}" in the body
- Post your handoff on the **issue** (not the PR): `gh issue comment {issue_number} --repo {repo} --body "..."`
- Your comment must start with `<!-- agent:implementer -->`
- End with `STATUS: IMPLEMENTATION_COMPLETE` and `@codex please review PR #N.`
- Never silently exit — always post an issue comment with a STATUS line
"""

    Path(path).write_text(content)
    return path


def _format_thread(comments: list[dict]) -> str:
    if not comments:
        return ""
    lines = []
    for c in comments:
        author = c.get("user", {}).get("login", "unknown")
        created = c.get("created_at", "")
        body = c.get("body", "").strip()
        lines.append(f"**@{author}** ({created}):\n{body}\n")
    return "\n---\n".join(lines)


def _extract_agent_comment(comments: list[dict], tag: str) -> str:
    """Return the body of the last comment containing the given agent tag."""
    for c in reversed(comments):
        body = c.get("body", "")
        if f"<!-- {tag} -->" in body:
            return body.strip()
    return ""
