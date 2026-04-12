"""
dispatch.py — Core routing. Parses events, validates stage transitions,
builds prompts, enqueues and spawns agents.
"""

import logging
import re
import subprocess
from typing import Optional

import state
from config import (
    AGENTS,
    GITHUB_REPO,
    MAX_REVIEW_CYCLES,
    MAX_DECOMPOSITION_DEPTH,
    TRUSTED_AUTHOR_ASSOCIATIONS,
    BOT_GITHUB_USERNAME,
)
from prompts import planner as planner_prompt
from prompts import implementer as implementer_prompt
from prompts import reviewer as reviewer_prompt
import spawn
import telegram

logger = logging.getLogger(__name__)

# Regex for @mention on the last non-empty line
_MENTION_RE = re.compile(r"@(claude|implementer|codex|human)\b")

# STATUS token extraction
_STATUS_RE = re.compile(r"STATUS:\s*(\w+(?:_\w+)*)")

# Depends-on parser
_DEPENDS_RE = re.compile(r"Depends-on:\s*#(\d+)", re.IGNORECASE)

# Parent issue parser
_PARENT_RE = re.compile(r"Parent:\s*#(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handle_event(event_type: str, action: str, payload: dict, delivery_id: str):
    """Route a GitHub webhook event to the appropriate agent."""

    if state.is_duplicate(delivery_id):
        logger.info("Duplicate delivery %s — skipping", delivery_id)
        return

    try:
        _route(event_type, action, payload)
    except Exception:
        logger.exception("Error handling event %s/%s", event_type, action)


def _route(event_type: str, action: str, payload: dict):
    repo = payload.get("repository", {}).get("full_name", GITHUB_REPO)

    # ------------------------------------------------------------------
    # issues.opened
    # ------------------------------------------------------------------
    if event_type == "issues" and action == "opened":
        issue = payload["issue"]
        issue_number = issue["number"]
        issue_title = issue.get("title", "")
        issue_body = issue.get("body", "") or ""

        # Self-triggered filter
        opener = issue.get("user", {}).get("login", "")
        if opener == BOT_GITHUB_USERNAME:
            logger.info("Ignoring self-opened issue #%s", issue_number)
            return

        # Record decomposition depth from parent link
        parent_match = _PARENT_RE.search(issue_body)
        if parent_match:
            parent_issue = int(parent_match.group(1))
            parent_depth = state.get_decomposition_depth(parent_issue, repo)
            state.record_decomposition_meta(issue_number, repo, depth=parent_depth + 1)
        else:
            state.record_decomposition_meta(issue_number, repo, depth=0)

        # Record dependencies
        for dep_match in _DEPENDS_RE.finditer(issue_body):
            depends_on = int(dep_match.group(1))
            state.record_dependency(issue_number, depends_on, repo)

        if not state.transition(issue_number, repo, "open", "planning"):
            logger.warning("Issue #%s not in 'open' stage — ignoring issues.opened", issue_number)
            return

        _dispatch_agent(
            agent="claude",
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            stage="planning",
        )
        return

    # ------------------------------------------------------------------
    # issue_comment.created
    # ------------------------------------------------------------------
    if event_type == "issue_comment" and action == "created":
        comment = payload["comment"]
        issue = payload["issue"]
        issue_number = issue["number"]
        repo = payload.get("repository", {}).get("full_name", GITHUB_REPO)
        issue_title = issue.get("title", "")
        issue_body = issue.get("body", "") or ""
        comment_body = comment.get("body", "") or ""
        commenter = comment.get("user", {}).get("login", "")
        author_association = comment.get("author_association", "")
        is_agent_comment = "<!-- agent:" in comment_body

        # Self-trigger filter
        if commenter == BOT_GITHUB_USERNAME:
            logger.info("Ignoring self-comment on #%s", issue_number)
            return

        # Parse @mention from last non-empty line
        mention = _parse_mention(comment_body)

        # For human-authored comments: require trusted collaborator AND a mention
        if not is_agent_comment:
            if mention is None:
                return
            if author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
                logger.info(
                    "Ignoring @%s mention from untrusted user %s (%s)",
                    mention, commenter, author_association,
                )
                return
        else:
            # Agent comments: route even without mention (e.g. STATUS: DECOMPOSED, STATUS: APPROVED)
            # but only if there's a STATUS token or a mention — ignore bare agent-tagged comments
            status_check = _parse_status(comment_body)
            if mention is None and status_check is None:
                return

        # Parse STATUS token
        status = _parse_status(comment_body)

        # Route by status + mention
        _handle_comment(
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            mention=mention,
            status=status,
            comment_body=comment_body,
        )
        return

    # ------------------------------------------------------------------
    # issues.closed — satisfy dependencies
    # ------------------------------------------------------------------
    if event_type == "issues" and action == "closed":
        issue_number = payload["issue"]["number"]
        state.satisfy_dependency(issue_number, repo)
        return

    # ------------------------------------------------------------------
    # workflow_run.completed — log only
    # ------------------------------------------------------------------
    if event_type == "workflow_run" and action == "completed":
        conclusion = payload.get("workflow_run", {}).get("conclusion", "unknown")
        logger.info("CI workflow completed with conclusion: %s", conclusion)
        return


# ---------------------------------------------------------------------------
# Comment routing
# ---------------------------------------------------------------------------

def _handle_comment(
    issue_number: int,
    repo: str,
    issue_title: str,
    issue_body: str,
    mention: str,
    status: Optional[str],
    comment_body: str,
):
    # Determine expected transition
    transitions = {
        # (status, mention) → (expected_stage, new_stage, agent, review_type)
        ("PLAN_COMPLETE", "codex"):               ("planning",      "plan_review",   "codex",       "plan"),
        ("DECOMPOSED", None):                     ("planning",      "decomposed",    None,          None),
        ("PLAN_APPROVED", "implementer"):         ("plan_review",   "implementing",  "implementer", None),
        ("PLAN_CHANGES_REQUESTED", "claude"):     ("plan_review",   "planning",      "claude",      "plan"),
        ("IMPLEMENTATION_COMPLETE", "codex"):     ("implementing",  "code_review",   "codex",       "code"),
        ("CONFLICTS", "codex"):                   ("implementing",  "code_review",   "codex",       "code"),
        ("CHANGES_REQUESTED", "implementer"):     ("code_review",   "implementing",  "implementer", None),
        ("CI_FAILING", "implementer"):            ("code_review",   "implementing",  "implementer", None),
    }
    # Escalations
    escalation_statuses = {"BLOCKED", "FAILED", "TESTS_FAILING"}

    key = (status, mention)

    if status in escalation_statuses or mention == "human":
        state.escalate(issue_number, repo)
        telegram.send_notification(
            f"Escalated to @human: issue #{issue_number} in {repo}. Status: {status}",
            issue_url=f"https://github.com/{repo}/issues/{issue_number}",
        )
        return

    route = transitions.get(key)
    if route is None:
        logger.warning(
            "No transition for status=%r mention=%r on issue #%s — skipping",
            status, mention, issue_number,
        )
        return

    expected_stage, new_stage, agent, review_type = route

    # Terminal: DECOMPOSED — no dispatch
    if new_stage == "decomposed":
        if not state.transition(issue_number, repo, expected_stage, new_stage):
            logger.warning("Stage transition failed for issue #%s (%s→%s)", issue_number, expected_stage, new_stage)
        return

    # Review cycle limit check
    if review_type:
        count = state.get_review_count(issue_number, repo, review_type)
        if count >= MAX_REVIEW_CYCLES:
            logger.info(
                "Issue #%s exceeded max review cycles (%d) for %s review — escalating",
                issue_number, MAX_REVIEW_CYCLES, review_type,
            )
            state.escalate(issue_number, repo)
            telegram.send_notification(
                f"Max review cycles reached for issue #{issue_number} ({review_type} review). Escalating.",
                issue_url=f"https://github.com/{repo}/issues/{issue_number}",
            )
            return
        state.increment_review_count(issue_number, repo, review_type)

    # Atomic stage transition
    if not state.transition(issue_number, repo, expected_stage, new_stage):
        logger.warning(
            "Stage transition failed for issue #%s (%s→%s) — stale/duplicate/out-of-sequence",
            issue_number, expected_stage, new_stage,
        )
        return

    if agent:
        _dispatch_agent(
            agent=agent,
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            stage=new_stage,
            comment_body=comment_body,
        )


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------

def _dispatch_agent(
    agent: str,
    issue_number: int,
    repo: str,
    issue_title: str,
    issue_body: str,
    stage: str,
    comment_body: str = "",
):
    """Fetch context, build prompt, enqueue, try to spawn."""
    comment_thread = _fetch_comments(repo, issue_number)

    # PR context for code-facing stages
    pr_number = None
    pr_diff = None
    pr_description = None
    if stage in ("code_review", "implementing"):
        pr_number, pr_diff, pr_description = _fetch_pr_context(repo, issue_number)

    # Build prompt file
    if agent == "claude":
        depth = state.get_decomposition_depth(issue_number, repo)
        prompt_file = planner_prompt.build(
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            comment_thread=comment_thread,
            decomposition_depth=depth,
            max_decomposition_depth=MAX_DECOMPOSITION_DEPTH,
        )
    elif agent == "implementer":
        prompt_file = implementer_prompt.build(
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            comment_thread=comment_thread,
            pr_number=pr_number,
            pr_diff=pr_diff,
            pr_description=pr_description,
        )
    elif agent == "codex":
        review_mode = "plan" if stage == "plan_review" else "code"
        prompt_file = reviewer_prompt.build(
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            comment_thread=comment_thread,
            review_mode=review_mode,
            pr_number=pr_number,
            pr_diff=pr_diff,
            pr_description=pr_description,
        )
    else:
        logger.error("Unknown agent: %s", agent)
        return

    # Enqueue
    run_id = state.enqueue_run(issue_number, repo, agent, prompt_file)
    logger.info("Enqueued run %d for %s on issue #%s", run_id, agent, issue_number)

    # Try to promote
    run = state.try_promote(agent)
    if run is None:
        logger.info("Agent %s busy or blocked — run %d queued", agent, run_id)
        return

    # Spawn
    _spawn_run(run, repo)


def _spawn_run(run, repo: str):
    """Spawn a promoted run into a tmux window."""
    run_id = run["id"]
    agent = run["agent"]
    issue_number = run["issue_number"]
    issue_id = str(issue_number)
    prompt_file = run["prompt_file"]

    # Determine repo_path (worktree for codex, main repo for others)
    if agent == "codex":
        pr_branch = run["pr_branch"]
        try:
            worktree_path = spawn.create_reviewer_worktree(
                issue_id=issue_id,
                run_id=run_id,
                pr_branch=pr_branch,
                repo_path=repo,
            )
        except Exception:
            logger.exception("Failed to create reviewer worktree for run %d", run_id)
            state.fail_run(run_id)
            return
        state.update_run_worktree(run_id, worktree_path)
        repo_path = worktree_path
    else:
        from config import REPO_LOCAL_PATH
        repo_path = REPO_LOCAL_PATH

    window_name = spawn.create_agent_window(
        run_id=run_id,
        agent_name=agent,
        issue_id=issue_id,
        prompt_file_path=prompt_file,
        repo_path=repo_path,
    )
    state.update_run_window(run_id, window_name)
    logger.info("Spawned run %d → window %s", run_id, window_name)


# ---------------------------------------------------------------------------
# Queue drain (called by state after complete/fail)
# ---------------------------------------------------------------------------

def drain_queue(agent: str):
    """Promote and spawn the next queued run for this agent type, if any."""
    run = state.try_promote(agent)
    if run is None:
        return
    repo = run["repo"]
    _spawn_run(run, repo)


# ---------------------------------------------------------------------------
# Context fetching
# ---------------------------------------------------------------------------

def _fetch_comments(repo: str, issue_number: int) -> list[dict]:
    import json
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
             "--paginate", "--jq", ".[]"],
            capture_output=True, text=True, check=True,
        )
        # gh --jq .[] returns one JSON object per line
        objects = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    objects.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return objects
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to fetch comments for #%s: %s", issue_number, exc.stderr)
        return []


def _fetch_pr_context(repo: str, issue_number: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Find the PR linked to this issue and return (pr_number, diff, description)."""
    import json
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo,
             "--search", f"Closes #{issue_number}", "--json", "number,title,body"],
            capture_output=True, text=True, check=True,
        )
        prs = json.loads(result.stdout or "[]")
        if not prs:
            return None, None, None
        pr = prs[0]
        pr_number = pr["number"]
        pr_description = pr.get("body", "")

        diff_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo],
            capture_output=True, text=True, check=True,
        )
        return pr_number, diff_result.stdout, pr_description
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to fetch PR context for #%s: %s", issue_number, exc.stderr)
        return None, None, None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_mention(body: str) -> Optional[str]:
    """Extract @mention from last non-empty line of body.

    Only parses if the body contains <!-- agent:NAME --> tag (agent comment)
    or the caller already verified author_association (handled in _route).
    Ignores mentions inside code blocks or blockquotes.
    """
    lines = body.splitlines()
    # Find last non-empty line
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip blockquotes
        if stripped.startswith(">"):
            return None
        match = _MENTION_RE.search(stripped)
        if match:
            return match.group(1)
        return None
    return None


def _parse_status(body: str) -> Optional[str]:
    """Extract STATUS: TOKEN from comment body."""
    match = _STATUS_RE.search(body)
    return match.group(1) if match else None
