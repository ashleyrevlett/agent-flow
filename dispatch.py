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
import notifications as telegram

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

        # Parse @mention from last non-empty line
        mention = _parse_mention(comment_body)

        if is_agent_comment:
            # Agent-tagged comments drive pipeline handoffs.
            # Accept only from BOT_GITHUB_USERNAME (our bot) OR trusted collaborators.
            # This prevents external users from spoofing <!-- agent:... --> tags.
            is_trusted = (
                (BOT_GITHUB_USERNAME and commenter == BOT_GITHUB_USERNAME)
                or author_association in TRUSTED_AUTHOR_ASSOCIATIONS
            )
            if not is_trusted:
                logger.info(
                    "Ignoring agent-tagged comment from untrusted user %s (%s) on #%s",
                    commenter, author_association, issue_number,
                )
                return
            # Ignore bare agent-tagged comments with no STATUS and no mention
            status_check = _parse_status(comment_body)
            if mention is None and status_check is None:
                return
        else:
            # Human-authored comments: require trusted collaborator AND a mention.
            # Filter bot's own non-agent comments to prevent loops.
            if commenter == BOT_GITHUB_USERNAME:
                logger.info("Ignoring non-agent self-comment on #%s", issue_number)
                return
            if mention is None:
                return
            if author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
                logger.info(
                    "Ignoring @%s mention from untrusted user %s (%s)",
                    mention, commenter, author_association,
                )
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
        # TESTS_FAILING → route to codex for code review (reviewer decides if real issue)
        ("TESTS_FAILING", "codex"):              ("implementing",  "code_review",   "codex",       "code"),
        # BLOCKED from implementer → planner to clarify ambiguous plan
        ("BLOCKED", "claude"):                   ("implementing",  "planning",      "claude",      None),
    }

    key = (status, mention)

    # Escalate to human when explicitly requested or on unrecoverable failures
    if mention == "human" or (status == "FAILED" and mention != "codex"):
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

    # Review cycle limit check (count read before transition; increment after)
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

    # Atomic stage transition
    if not state.transition(issue_number, repo, expected_stage, new_stage):
        logger.warning(
            "Stage transition failed for issue #%s (%s→%s) — stale/duplicate/out-of-sequence",
            issue_number, expected_stage, new_stage,
        )
        return

    # Increment only after a successful transition so stale/duplicate comments
    # don't consume the cycle budget.
    if review_type:
        state.increment_review_count(issue_number, repo, review_type)

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

    # For code-review runs, resolve the PR branch BEFORE enqueue so the
    # INSERT is atomic with the branch value. try_promote() can fire the
    # moment a queued row exists; a post-enqueue update_run_pr_branch call
    # has a race window where promotion can happen while pr_branch is NULL.
    pr_branch_for_run: Optional[str] = None
    if agent == "codex" and stage == "code_review":
        if pr_number is None:
            logger.error(
                "Code review dispatched for issue #%s but no pr_number — aborting (no row created)",
                issue_number,
            )
            return
        pr_branch_for_run = _fetch_pr_branch(repo, pr_number)
        if not pr_branch_for_run:
            logger.error(
                "Could not resolve PR branch for PR #%s (issue #%s) — aborting (no row created)",
                pr_number, issue_number,
            )
            return

    # Enqueue — pr_branch stored atomically for code-review runs
    run_id = state.enqueue_run(issue_number, repo, agent, prompt_file, pr_branch=pr_branch_for_run)
    logger.info("Enqueued run %d for %s on issue #%s", run_id, agent, issue_number)

    # Try to promote
    run = state.try_promote(agent)
    if run is None:
        logger.info("Agent %s busy or blocked — run %d queued", agent, run_id)
        return

    # Spawn
    _spawn_run(run)


def _spawn_run(run):
    """Spawn a promoted run into a tmux window."""
    from config import REPO_LOCAL_PATH
    run_id = run["id"]
    agent = run["agent"]
    issue_number = run["issue_number"]
    issue_id = str(issue_number)
    prompt_file = run["prompt_file"]

    # Determine repo_path (worktree for codex, local clone for others)
    if agent == "codex":
        pr_branch = run["pr_branch"]
        try:
            worktree_path = spawn.create_reviewer_worktree(
                issue_id=issue_id,
                run_id=run_id,
                pr_branch=pr_branch,
                repo_path=REPO_LOCAL_PATH,  # always the local clone, not GitHub repo name
            )
        except Exception:
            logger.exception("Failed to create reviewer worktree for run %d", run_id)
            state.fail_run(run_id)
            return
        state.update_run_worktree(run_id, worktree_path)
        repo_path = worktree_path
    else:
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
    _spawn_run(run)


# ---------------------------------------------------------------------------
# Context fetching
# ---------------------------------------------------------------------------

def _fetch_comments(repo: str, issue_number: int) -> list[dict]:
    """Fetch all comments for an issue as a list of dicts."""
    import json
    try:
        # Use --paginate without --jq so output is a valid JSON array per page.
        # With --paginate, gh concatenates pages — output may be multiple arrays.
        # Use --jq '.' at the array level to flatten into newline-separated objects.
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments",
             "--paginate"],
            capture_output=True, text=True, check=True,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        # gh --paginate outputs one JSON array per page, concatenated.
        # Wrap in a single parse by collecting all objects via decoder.
        objects = []
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


def _fetch_pr_context(repo: str, issue_number: int) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Find the PR linked to this issue and return (pr_number, diff, description)."""
    import json
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo,
             "--search", f"Closes #{issue_number}", "--json", "number,title,body,headRefName"],
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


def _fetch_pr_branch(repo: str, pr_number: int) -> Optional[str]:
    """Return the head branch name for a given PR number."""
    import json
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "headRefName"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        return data.get("headRefName")
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch PR branch for PR #%s: %s", pr_number, exc)
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_mention(body: str) -> Optional[str]:
    """Extract @mention from last non-empty, non-code, non-blockquote line.

    Ignores:
    - Lines inside fenced code blocks (``` or ~~~)
    - Lines that start with > (blockquotes)
    - @mentions inside inline backtick spans on the last line
    """
    lines = body.splitlines()

    # Track which lines are inside fenced code blocks (forward pass).
    in_fence = False
    fence_marker: str = ""
    fenced: list[bool] = []
    for line in lines:
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
                fenced.append(True)
            else:
                fenced.append(False)
        else:
            fenced.append(True)
            if stripped.startswith(fence_marker):
                in_fence = False

    # Find last non-empty, non-fenced, non-blockquote line.
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        if fenced[idx]:
            continue
        if stripped.startswith(">"):
            continue
        # Strip inline backtick spans before searching for a mention so that
        # `@some-handle` in code does not route.
        sanitised = re.sub(r"`[^`]*`", "", stripped)
        match = _MENTION_RE.search(sanitised)
        if match:
            return match.group(1)
        return None
    return None


def _parse_status(body: str) -> Optional[str]:
    """Extract STATUS: TOKEN from comment body."""
    match = _STATUS_RE.search(body)
    return match.group(1) if match else None
