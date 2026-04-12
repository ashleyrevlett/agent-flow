"""
spawn.py — tmux session/window management. Uses subprocess.run(["tmux", ...])
directly (not libtmux) for reliable automation.
"""

import subprocess
import time
import logging
from pathlib import Path
from typing import Optional

from config import TMUX_SESSION_NAME, ROLES_DIR, WORKTREE_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def ensure_session():
    """Create the master tmux session if it doesn't exist."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION_NAME],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION_NAME],
            check=True,
        )
        logger.info("Created tmux session: %s", TMUX_SESSION_NAME)


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------

def create_reviewer_worktree(issue_id: str, run_id: int, pr_branch: str | None, repo_path: str) -> str:
    """
    For @codex code review: worktree on pr_branch.
    For @codex plan review: pr_branch=None → detached HEAD on default branch.
    Returns worktree path.
    """
    import os
    os.makedirs(WORKTREE_DIR, exist_ok=True)

    if pr_branch:
        name = f"review-{issue_id}-{run_id}"
        worktree_path = str(Path(WORKTREE_DIR) / name)
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", worktree_path, pr_branch],
            check=True,
        )
    else:
        name = f"planreview-{issue_id}-{run_id}"
        worktree_path = str(Path(WORKTREE_DIR) / name)
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", "--detach", worktree_path, "HEAD"],
            check=True,
        )

    logger.info("Created reviewer worktree: %s", worktree_path)
    return worktree_path


def cleanup_worktree(worktree_path: str, repo_path: Optional[str] = None):
    """Remove a manually-created worktree (reviewer only).

    repo_path: the main repo clone directory. Required so git -C works from
    any cwd. If not provided, falls back to cwd (less reliable).
    """
    from config import REPO_LOCAL_PATH
    effective_repo = repo_path or REPO_LOCAL_PATH
    try:
        subprocess.run(
            ["git", "-C", effective_repo, "worktree", "remove", worktree_path, "--force"],
            check=True,
        )
        logger.info("Cleaned up worktree: %s", worktree_path)
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to clean up worktree %s: %s", worktree_path, exc)


# ---------------------------------------------------------------------------
# Agent window creation
# ---------------------------------------------------------------------------

def _build_cli_command(agent_name: str, issue_id: str, run_id: int) -> str:
    roles_dir = str(ROLES_DIR)
    if agent_name == "claude":
        return (
            f"claude -w plan-{issue_id}-{run_id} "
            f"--dangerously-skip-permissions "
            f"--append-system-prompt-file {roles_dir}/planner.md"
        )
    elif agent_name == "implementer":
        return (
            f"claude -w feature-{issue_id}-{run_id} "
            f"--model sonnet "
            f"--dangerously-skip-permissions "
            f"--append-system-prompt-file {roles_dir}/implementer.md"
        )
    elif agent_name == "codex":
        return f"codex -c model_instructions_file={roles_dir}/reviewer.md"
    else:
        raise ValueError(f"Unknown agent: {agent_name}")


def _tmux(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux"] + args, capture_output=True, text=True, check=check)


def _send_keys(window: str, keys: str, enter: bool = True):
    cmd = ["send-keys", "-t", f"{TMUX_SESSION_NAME}:{window}", keys]
    if enter:
        cmd.append("Enter")
    _tmux(cmd)


def create_agent_window(
    run_id: int,
    agent_name: str,
    issue_id: str,
    prompt_file_path: str,
    repo_path: str,
) -> str:
    """
    Create a named tmux window, cd into repo_path, launch the agent CLI,
    and send the task instruction. Returns the window name.
    """
    window_name = f"{agent_name}-{issue_id}-{run_id}"

    # Create window
    _tmux(["new-window", "-t", TMUX_SESSION_NAME, "-n", window_name])

    # cd into the worktree/repo
    _send_keys(window_name, f"cd {repo_path}")
    time.sleep(0.3)

    # Launch CLI
    cli_cmd = _build_cli_command(agent_name, issue_id, run_id)
    _send_keys(window_name, cli_cmd)

    # Wait for CLI to initialize
    time.sleep(2)

    # Send single-line task instruction
    instruction = f"Read and execute the task in {prompt_file_path}"
    _send_keys(window_name, instruction)

    logger.info("Spawned %s window: %s", agent_name, window_name)
    return window_name


# ---------------------------------------------------------------------------
# Pane utilities
# ---------------------------------------------------------------------------

def capture_pane(window_name: str, lines: int = 500) -> str:
    result = _tmux(
        ["capture-pane", "-p", "-t", f"{TMUX_SESSION_NAME}:{window_name}", "-S", f"-{lines}"],
        check=False,
    )
    return result.stdout


def kill_window(window_name: str):
    _tmux(["kill-window", "-t", f"{TMUX_SESSION_NAME}:{window_name}"], check=False)
    logger.info("Killed window: %s", window_name)


def list_windows() -> list[str]:
    result = _tmux(
        ["list-windows", "-t", TMUX_SESSION_NAME, "-F", "#{window_name}"],
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
