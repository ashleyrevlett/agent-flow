"""
hermes_spawn.py — Agent spawning via Hermes CLI inside tmux windows.

Hermes runs inside a tmux window for observability (tmux attach to watch),
but handles all CLI interaction itself — trust prompts, errors, monitoring.

Hermes is a reactive executor — it runs the CLI, handles interactive prompts,
and reports completion. It does NOT make pipeline decisions. Python owns the
state machine and all routing logic.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from config import TMUX_SESSION_NAME, ROLES_DIR

logger = logging.getLogger(__name__)

# Hermes system prompt — minimal, reactive, no pipeline knowledge
_RUNNER_SYSTEM_PROMPT = """\
You are a CLI runner for an automated development pipeline. Your ONLY job:

1. Run the exact command you are given via the terminal tool with pty=true
2. If you see a trust/confirmation prompt ("Do you trust", "Yes, continue", \
"y/n"), accept it by writing "y" or pressing Enter as appropriate
3. Wait for the CLI process to become ready for input
4. When the CLI is ready, send it the task instruction you were given
5. Monitor the process until it exits on its own
6. If the process appears stuck (no output for 5+ minutes), message the \
human on Telegram: "Run stuck: <agent> on issue #<number> — no output for 5min"
7. If you see a fatal error or crash, message the human on Telegram: \
"Run failed: <agent> on issue #<number> — <brief error description>"
8. When the process exits normally, report "DONE"

After messaging the human about a stuck or failed run, report "STUCK" or \
"ERROR: <description>" respectively so the pipeline can update its state.

CRITICAL RULES:
- Do NOT interpret the task content or make decisions about what to do next
- Do NOT modify files, run additional commands, or take any action beyond \
running and monitoring the specified CLI process
- You are a transparent wrapper — the CLI tool does the real work
"""


def _build_cli_command(agent_name: str, issue_id: str, run_id: int) -> str:
    """Build the CLI command string for a given agent type."""
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


def _build_task_message(
    agent_name: str,
    cli_command: str,
    repo_path: str,
    prompt_file_path: str,
) -> str:
    """Build the one-shot task message that Hermes receives."""
    return f"""\
Run this CLI tool and interact with it.

Step 1 — Launch the CLI:
Run this command with pty=true:
```
cd {repo_path} && {cli_command}
```

Step 2 — Handle initialization:
The CLI may show a trust/confirmation prompt. If so, accept it.
Wait until the CLI is ready for input (you'll see a prompt like ">" or "$").

Step 3 — Send the task:
Once the CLI is ready, send it this exact text:
```
Read and execute the task in {prompt_file_path}
```

Step 4 — Monitor:
Wait for the CLI process to finish. Do not interfere with its work.
When it exits, report "DONE".
If it appears stuck (no new output for 5 minutes), report "STUCK".
If it crashes or shows a fatal error, report "ERROR: <brief description>".
"""


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def _tmux(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux"] + args, capture_output=True, text=True, check=check)


def _window_exists(window_name: str) -> bool:
    result = _tmux(
        ["list-windows", "-t", TMUX_SESSION_NAME, "-F", "#{window_name}"],
        check=False,
    )
    if result.returncode != 0:
        return False
    return window_name in result.stdout.splitlines()


def _capture_pane(window_name: str, lines: int = 200) -> str:
    result = _tmux(
        ["capture-pane", "-p", "-t", f"{TMUX_SESSION_NAME}:{window_name}", "-S", f"-{lines}"],
        check=False,
    )
    return result.stdout


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


def list_windows() -> list[str]:
    result = _tmux(
        ["list-windows", "-t", TMUX_SESSION_NAME, "-F", "#{window_name}"],
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def kill_window(window_name: str):
    _tmux(["kill-window", "-t", f"{TMUX_SESSION_NAME}:{window_name}"], check=False)
    logger.info("Killed window: %s", window_name)


# ---------------------------------------------------------------------------
# Hermes session management
# ---------------------------------------------------------------------------

# Track active Hermes sessions: run_id -> thread
_active_threads: dict[int, threading.Thread] = {}


def _build_hermes_command(task_message: str) -> str:
    """Build the full hermes chat shell command.

    We need to escape the task message for shell embedding since it's
    sent via tmux send-keys into a shell.
    """
    # Single-quote the task message to prevent shell interpretation.
    # Escape any single quotes within the message.
    escaped = task_message.replace("'", "'\\''")
    return (
        f"HERMES_EPHEMERAL_SYSTEM_PROMPT='{_RUNNER_SYSTEM_PROMPT.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' "
        f"hermes chat --toolsets terminal --quiet --yolo "
        f"-q '{escaped}'"
    )


def create_agent_run(
    run_id: int,
    agent_name: str,
    issue_id: str,
    prompt_file_path: str,
    repo_path: str,
) -> str:
    """
    Launch a Hermes agent session inside a tmux window.
    Returns the window name (stored in state DB for monitoring).

    A background thread monitors the tmux window. When hermes exits
    (window closes or process ends), the thread updates run state.
    """
    cli_command = _build_cli_command(agent_name, issue_id, run_id)
    task_message = _build_task_message(agent_name, cli_command, repo_path, prompt_file_path)
    window_name = f"hermes-{agent_name}-{issue_id}-{run_id}"

    # Ensure tmux session exists
    ensure_session()

    # Create tmux window
    _tmux(["new-window", "-t", TMUX_SESSION_NAME, "-n", window_name])

    # Build and send the hermes command
    # Write the system prompt and task to temp files to avoid shell escaping hell
    os.makedirs(os.path.join(os.path.dirname(__file__), "tmp"), exist_ok=True)
    system_file = os.path.join(os.path.dirname(__file__), "tmp", f"sys-{run_id}.md")
    task_file = os.path.join(os.path.dirname(__file__), "tmp", f"task-{run_id}.md")
    with open(system_file, "w") as f:
        f.write(_RUNNER_SYSTEM_PROMPT)
    with open(task_file, "w") as f:
        f.write(task_message)

    # Construct a shell command that reads from files — no escaping needed
    hermes_cmd = (
        f"export HERMES_EPHEMERAL_SYSTEM_PROMPT=\"$(cat {system_file})\" && "
        f"hermes chat --toolsets terminal --quiet --yolo "
        f"-q \"$(cat {task_file})\""
    )

    # Send the command to the tmux window
    _tmux(["send-keys", "-t", f"{TMUX_SESSION_NAME}:{window_name}", hermes_cmd, "Enter"])

    # Start monitor thread
    def _monitor():
        try:
            logger.info("Monitoring Hermes window %s for run %d", window_name, run_id)

            # Poll until the window disappears (hermes exited)
            while _window_exists(window_name):
                time.sleep(5)

            # Window gone — hermes exited. Check final pane output
            # (window is gone so we can't capture; rely on exit behavior)
            logger.info("Hermes window %s closed for run %d", window_name, run_id)

            # Signal completion
            import state
            state.complete_run(run_id)

        except Exception:
            logger.exception("Monitor thread crashed for run %d", run_id)
            import state
            state.fail_run(run_id)
        finally:
            _active_threads.pop(run_id, None)
            # Clean up temp files
            for p in (system_file, task_file):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    thread = threading.Thread(target=_monitor, name=window_name, daemon=True)
    _active_threads[run_id] = thread
    thread.start()

    logger.info("Spawned Hermes in tmux window: %s", window_name)
    return window_name


def is_session_alive(run_id: int) -> bool:
    """Check if a Hermes session thread is still running."""
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


def get_active_sessions() -> list[int]:
    """Return run_ids of all active Hermes sessions."""
    return [rid for rid, t in _active_threads.items() if t.is_alive()]


def kill_session(run_id: int):
    """Kill a Hermes session by killing its tmux window."""
    import state
    thread = _active_threads.pop(run_id, None)
    if thread:
        # Find the window name from the thread name
        window_name = thread.name
        kill_window(window_name)
        logger.info("Killed Hermes session for run %d (window %s)", run_id, window_name)
        state.fail_run(run_id, new_status="failed")
