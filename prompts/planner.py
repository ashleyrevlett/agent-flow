"""
prompts/planner.py — Build the per-invocation task file for @claude (planner).
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
    decomposition_depth: int = 0,
    max_decomposition_depth: int = 1,
    provider=None,
) -> str:
    """
    Write a task prompt file for the planner. Returns the file path.

    comment_thread: list of dicts with keys: user.login, body, created_at
    provider: GitProvider instance for CLI templates
    """
    os.makedirs(PROMPT_DIR, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = str(Path(PROMPT_DIR) / f"planner-{issue_number}-{ts}.md")

    depth_warning = ""
    if decomposition_depth >= max_decomposition_depth:
        depth_warning = (
            "\n> **DECOMPOSITION LIMIT REACHED.** "
            "You are at the maximum decomposition depth. "
            "You MUST use Mode A (direct plan) only. "
            "Do NOT create child issues or post STATUS: DECOMPOSED.\n"
        )

    thread_text = _format_thread(comment_thread)

    comment_cmd = provider.comment_cli(issue_number, repo)

    content = f"""# Planner Task — Issue #{issue_number}

Repo: {repo}
Issue: #{issue_number} — {issue_title}
{depth_warning}
## Issue Body

{issue_body or "(no body)"}

## Comment Thread

{thread_text or "(no comments yet)"}

---

## Your Task

Analyze the issue above and produce a plan following the instructions in your system prompt (roles/planner.md).

Key reminders:
- Post your output as an issue comment via: `{comment_cmd}`
- Your comment must start with `<!-- agent:claude -->`
- Use Mode A (direct plan with STATUS: PLAN_COMPLETE) or Mode B (decompose with STATUS: DECOMPOSED)
- In Mode A: end with `@codex please review this plan.`
- In Mode B: do not add a handoff @mention on the parent issue
- Never silently exit — always post a comment with a STATUS line
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
