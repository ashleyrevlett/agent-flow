"""
dispatch.py — Core routing. Parses events, validates stage transitions,
builds prompts, enqueues and spawns agents.
"""

import logging
import re
from typing import Optional

import state
from config import (
    AGENTS,
    MAX_REVIEW_CYCLES,
    MAX_DECOMPOSITION_DEPTH,
)
from config import ALLOW_SELF_TRIGGER
from provider import WebhookEvent, get_provider
from prompts import planner as planner_prompt
from prompts import implementer as implementer_prompt
from prompts import reviewer as reviewer_prompt
import spawn  # worktree management (git, not tmux)
import hermes_spawn
import notifications as telegram

logger = logging.getLogger(__name__)

_provider = get_provider()

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

def handle_event(event: WebhookEvent):
    """Route a webhook event to the appropriate agent."""

    if state.is_duplicate(event.delivery_id):
        logger.info("Duplicate delivery %s — skipping", event.delivery_id)
        return

    try:
        _route(event)
    except Exception:
        logger.exception("Error handling event %s", event.kind)


def _route(event: WebhookEvent):
    repo = event.repo

    # ------------------------------------------------------------------
    # issue_opened
    # ------------------------------------------------------------------
    if event.kind == "issue_opened":
        issue_number = event.issue_number
        issue_body = event.issue_body

        # Self-triggered filter
        if event.is_bot and not ALLOW_SELF_TRIGGER:
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
            logger.warning("Issue #%s not in 'open' stage — ignoring issue_opened", issue_number)
            return

        _dispatch_agent(
            agent="claude",
            issue_number=issue_number,
            repo=repo,
            issue_title=event.issue_title,
            issue_body=issue_body,
            stage="planning",
        )
        return

    # ------------------------------------------------------------------
    # comment_created
    # ------------------------------------------------------------------
    if event.kind == "comment_created":
        issue_number = event.issue_number
        comment_body = event.comment_body or ""

        # Parse @mention from last non-empty line
        mention = _parse_mention(comment_body)

        if event.is_agent_comment:
            # Agent-tagged comments drive pipeline handoffs.
            # Trust was pre-computed by the provider.
            if not event.is_trusted:
                logger.info(
                    "Ignoring agent-tagged comment from untrusted user %s on #%s",
                    event.commenter, issue_number,
                )
                return
            # Ignore bare agent-tagged comments with no STATUS and no mention
            status_check = _parse_status(comment_body)
            if mention is None and status_check is None:
                return
        else:
            # Human-authored comments: require trusted + mention.
            # Filter bot's own non-agent comments to prevent loops.
            if event.is_bot:
                logger.info("Ignoring non-agent self-comment on #%s", issue_number)
                return
            if mention is None:
                return
            if not event.is_trusted:
                logger.info(
                    "Ignoring @%s mention from untrusted user %s",
                    mention, event.commenter,
                )
                return

        # Parse STATUS token
        status = _parse_status(comment_body)

        # Route by status + mention
        _handle_comment(
            issue_number=issue_number,
            repo=repo,
            issue_title=event.issue_title,
            issue_body=event.issue_body,
            mention=mention,
            status=status,
            comment_body=comment_body,
        )
        return

    # ------------------------------------------------------------------
    # issue_closed — satisfy dependencies
    # ------------------------------------------------------------------
    if event.kind == "issue_closed":
        state.satisfy_dependency(event.issue_number, repo)
        return

    # ------------------------------------------------------------------
    # workflow_completed — log only
    # ------------------------------------------------------------------
    if event.kind == "workflow_completed":
        logger.info("CI workflow completed with conclusion: %s", event.comment_body)
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
            issue_url=_provider.issue_url(repo, issue_number),
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
                issue_url=_provider.issue_url(repo, issue_number),
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
    comment_thread = _provider.fetch_comments(repo, issue_number)

    # MR context for code-facing stages
    mr_number = None
    mr_diff = None
    mr_description = None
    if stage in ("code_review", "implementing"):
        mr_number, mr_diff, mr_description = _provider.fetch_mr_context(repo, issue_number)

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
            provider=_provider,
        )
    elif agent == "implementer":
        prompt_file = implementer_prompt.build(
            issue_number=issue_number,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            comment_thread=comment_thread,
            pr_number=mr_number,
            pr_diff=mr_diff,
            pr_description=mr_description,
            provider=_provider,
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
            pr_number=mr_number,
            pr_diff=mr_diff,
            pr_description=mr_description,
            provider=_provider,
        )
    else:
        logger.error("Unknown agent: %s", agent)
        return

    # For code-review runs, resolve the MR branch BEFORE enqueue so the
    # INSERT is atomic with the branch value. try_promote() can fire the
    # moment a queued row exists; a post-enqueue update is racy.
    pr_branch_for_run: Optional[str] = None
    if agent == "codex" and stage == "code_review":
        if mr_number is None:
            logger.error(
                "Code review dispatched for issue #%s but no MR found — aborting (no row created)",
                issue_number,
            )
            return
        pr_branch_for_run = _provider.fetch_mr_branch(repo, mr_number)
        if not pr_branch_for_run:
            logger.error(
                "Could not resolve MR branch for MR #%s (issue #%s) — aborting (no row created)",
                mr_number, issue_number,
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
    """Spawn a promoted run via Hermes agent session."""
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
                repo_path=REPO_LOCAL_PATH,
            )
        except Exception:
            logger.exception("Failed to create reviewer worktree for run %d", run_id)
            state.fail_run(run_id)
            return
        state.update_run_worktree(run_id, worktree_path)
        repo_path = worktree_path
    else:
        repo_path = REPO_LOCAL_PATH

    # Launch via Hermes — handles trust prompts, monitors CLI, signals completion
    session_id = hermes_spawn.create_agent_run(
        run_id=run_id,
        agent_name=agent,
        issue_id=issue_id,
        prompt_file_path=prompt_file,
        repo_path=repo_path,
    )
    state.update_run_window(run_id, session_id)
    logger.info("Spawned run %d → Hermes session %s", run_id, session_id)


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
