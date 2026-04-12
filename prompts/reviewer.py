"""
prompts/reviewer.py — Build the per-invocation task file for @codex (reviewer).
Branches between plan review and code review mode.
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
    review_mode: str,  # "plan" or "code"
    pr_number: int | None = None,
    pr_diff: str | None = None,
    pr_description: str | None = None,
) -> str:
    """
    Write a task prompt file for the reviewer. Returns the file path.

    review_mode="plan" — evaluate the planner's plan before implementation.
    review_mode="code" — evaluate the PR diff after implementation.
    """
    os.makedirs(PROMPT_DIR, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = str(Path(PROMPT_DIR) / f"reviewer-{review_mode}-{issue_number}-{ts}.md")

    if review_mode == "plan":
        content = _build_plan_review(issue_number, repo, issue_title, issue_body, comment_thread)
    elif review_mode == "code":
        content = _build_code_review(
            issue_number, repo, issue_title, issue_body, comment_thread,
            pr_number, pr_diff, pr_description,
        )
    else:
        raise ValueError(f"Unknown review_mode: {review_mode!r}")

    Path(path).write_text(content)
    return path


def _build_plan_review(
    issue_number: int,
    repo: str,
    issue_title: str,
    issue_body: str,
    comment_thread: list[dict],
) -> str:
    thread_text = _format_thread(comment_thread)
    plan_text = _extract_agent_comment(comment_thread, "agent:claude")

    return f"""# Reviewer Task — Plan Review — Issue #{issue_number}

Repo: {repo}
Issue: #{issue_number} — {issue_title}
review_mode: plan

## Issue Body

{issue_body or "(no body)"}

## Planner's Plan

{plan_text or "(see full comment thread below)"}

## Full Comment Thread

{thread_text or "(no comments)"}

---

## Your Task

Review the planner's plan above following the instructions in your system prompt (roles/reviewer.md).

Key reminders:
- Post your review as a GitHub issue comment: `gh issue comment {issue_number} --repo {repo} --body "..."`
- Your comment must start with `<!-- agent:codex -->`
- If approving: end with `STATUS: PLAN_APPROVED` and `@implementer please implement.`
- If requesting changes: end with `STATUS: PLAN_CHANGES_REQUESTED` and `@claude please revise the plan.`
- All handoff @mentions go in issue comments, never PR comments
- Never silently exit — always post an issue comment with a STATUS line
"""


def _build_code_review(
    issue_number: int,
    repo: str,
    issue_title: str,
    issue_body: str,
    comment_thread: list[dict],
    pr_number: int | None,
    pr_diff: str | None,
    pr_description: str | None,
) -> str:
    thread_text = _format_thread(comment_thread)
    plan_text = _extract_agent_comment(comment_thread, "agent:claude")

    diff_section = f"""
## PR Diff

```diff
{pr_diff or "(no diff available)"}
```
""" if pr_diff else ""

    pr_desc_section = f"""
## PR Description

{pr_description}
""" if pr_description else ""

    return f"""# Reviewer Task — Code Review — Issue #{issue_number}

Repo: {repo}
Issue: #{issue_number} — {issue_title}
PR: #{pr_number}
review_mode: code

## Issue Body

{issue_body or "(no body)"}

## Original Plan

{plan_text or "(see comment thread for plan)"}
{pr_desc_section}{diff_section}
## Comment Thread

{thread_text or "(no comments)"}

---

## Your Task

Review the PR above following the instructions in your system prompt (roles/reviewer.md).

Key reminders:
- Run tests if available before deciding
- Submit a GitHub PR review with `gh pr review {pr_number} --repo {repo} ...`
- Post your handoff on the **issue**: `gh issue comment {issue_number} --repo {repo} --body "..."`
- Your comment must start with `<!-- agent:codex -->`
- If approving:
  1. `gh pr checks {pr_number} --repo {repo} --required --watch`
  2. `gh pr merge {pr_number} --repo {repo} --squash --delete-branch`
  3. Post `STATUS: APPROVED` on the issue (no @mention)
- If requesting changes: end with `STATUS: CHANGES_REQUESTED` and `@implementer please address the feedback.`
- If CI is failing: end with `STATUS: CI_FAILING` and `@implementer please fix CI failures.`
- All handoff @mentions go in issue comments, never PR comments
- Never silently exit — always post an issue comment with a STATUS line
"""


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
