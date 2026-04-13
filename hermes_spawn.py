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
    cli_window_name: str,
) -> str:
    """Build the one-shot task message that Hermes receives."""
    session = TMUX_SESSION_NAME
    target = f"{session}:{cli_window_name}"
    return f"""\
You will manage a CLI tool running in a tmux window. Use tmux commands to \
interact with it — do NOT run the CLI directly in your own terminal.

The tmux session is: {session}
The CLI window is: {cli_window_name}

CRITICAL tmux send-keys rules:
- ALWAYS pass Enter as a SEPARATE ARGUMENT, never as part of the text string.
- CORRECT:   tmux send-keys -t {target} 'some text' Enter
- WRONG:     tmux send-keys -t {target} 'some text\\n'
- WRONG:     tmux send-keys -t {target} 'some text' 'Enter'
- The word Enter must be an unquoted bare argument at the end of the command.
- After sending text + Enter, ALWAYS verify it was received by capturing \
the pane and checking the output changed. If the text appears but was not \
submitted (no new output below it), send a bare Enter:
  tmux send-keys -t {target} Enter

Step 1 — Create the CLI window and launch the tool:
```
tmux new-window -t {session} -n {cli_window_name}
tmux send-keys -t {target} 'cd {repo_path} && {cli_command}' Enter
```

Step 2 — Wait for the CLI to initialize, then check for trust prompts:
Wait a few seconds, then read the window:
```
sleep 5
tmux capture-pane -t {target} -p -S -30
```
If you see a trust/confirmation prompt ("Do you trust", "Yes, continue", \
"y/n"), dismiss it:
- For "y/n" prompts: `tmux send-keys -t {target} y Enter`
- For numbered menus with "Press enter": `tmux send-keys -t {target} Enter`

Re-check the pane after dismissing to confirm the CLI is ready for input.

Step 3 — Send the task instruction:
Once the CLI shows an input prompt (like ">" or "$"):
```
tmux send-keys -t {target} 'Read and execute the task in {prompt_file_path}' Enter
```
Then verify the instruction was submitted — capture the pane and check for \
new output. If the text is visible but the CLI hasn't started processing \
(no activity below the pasted text), send a bare Enter to submit:
```
tmux send-keys -t {target} Enter
```

Step 4 — Monitor until done:
Periodically check the window (every 30-60 seconds):
```
tmux capture-pane -t {target} -p -S -50
```
- If the window no longer exists (`tmux has-window` fails), the CLI exited. Report "DONE".
- If no new output for 5+ minutes, report "STUCK".
- If you see a fatal error or crash, report "ERROR: <brief description>".

Do NOT interfere with the CLI's work. Just watch and wait.
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


def create_agent_run(
    run_id: int,
    agent_name: str,
    issue_id: str,
    prompt_file_path: str,
    repo_path: str,
) -> str:
    """
    Launch a Hermes agent session inside a tmux window. Hermes will create
    a second tmux window for the actual CLI tool (Claude Code / Codex),
    giving you visibility into both:

      tmux attach -t agent-flow    → see all windows
      Ctrl-b n/p                   → switch between hermes runner and CLI

    Returns the hermes window name (stored in state DB for monitoring).
    """
    cli_command = _build_cli_command(agent_name, issue_id, run_id)
    cli_window = f"{agent_name}-{issue_id}-{run_id}"
    hermes_window = f"hermes-{agent_name}-{issue_id}-{run_id}"
    task_message = _build_task_message(
        agent_name, cli_command, repo_path, prompt_file_path, cli_window,
    )

    # Ensure tmux session exists
    ensure_session()

    # Create the Hermes runner window
    _tmux(["new-window", "-t", TMUX_SESSION_NAME, "-n", hermes_window])

    # Write system prompt and task to temp files to avoid shell escaping
    tmp_dir = os.path.join(os.path.dirname(__file__), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    system_file = os.path.join(tmp_dir, f"sys-{run_id}.md")
    task_file = os.path.join(tmp_dir, f"task-{run_id}.md")
    with open(system_file, "w") as f:
        f.write(_RUNNER_SYSTEM_PROMPT)
    with open(task_file, "w") as f:
        f.write(task_message)

    # Launch hermes in the window — reads prompts from files
    hermes_cmd = (
        f"export HERMES_EPHEMERAL_SYSTEM_PROMPT=\"$(cat {system_file})\" && "
        f"hermes chat --toolsets terminal --quiet --yolo "
        f"-q \"$(cat {task_file})\""
    )
    _tmux(["send-keys", "-t", f"{TMUX_SESSION_NAME}:{hermes_window}", hermes_cmd, "Enter"])

    # Start monitor thread — watches the hermes window
    def _monitor():
        try:
            logger.info("Monitoring Hermes window %s for run %d", hermes_window, run_id)

            while _window_exists(hermes_window):
                time.sleep(5)

            logger.info("Hermes window %s closed for run %d", hermes_window, run_id)

            # Also clean up the CLI window if hermes left it behind
            if _window_exists(cli_window):
                kill_window(cli_window)

            import state
            state.complete_run(run_id)

        except Exception:
            logger.exception("Monitor thread crashed for run %d", run_id)
            import state
            state.fail_run(run_id)
        finally:
            _active_threads.pop(run_id, None)
            for p in (system_file, task_file):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    thread = threading.Thread(target=_monitor, name=hermes_window, daemon=True)
    _active_threads[run_id] = thread
    thread.start()

    logger.info("Spawned Hermes window: %s (CLI will appear as: %s)", hermes_window, cli_window)
    return hermes_window


def is_session_alive(run_id: int) -> bool:
    """Check if a Hermes session thread is still running."""
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


def get_active_sessions() -> list[int]:
    """Return run_ids of all active Hermes sessions."""
    return [rid for rid, t in _active_threads.items() if t.is_alive()]


def kill_session(run_id: int):
    """Kill a Hermes session by killing its tmux windows (hermes + CLI)."""
    import state
    thread = _active_threads.pop(run_id, None)
    if thread:
        hermes_window = thread.name
        # Derive CLI window name: strip "hermes-" prefix
        cli_window = hermes_window.replace("hermes-", "", 1)
        kill_window(hermes_window)
        if _window_exists(cli_window):
            kill_window(cli_window)
        logger.info("Killed Hermes session for run %d (windows: %s, %s)", run_id, hermes_window, cli_window)
        state.fail_run(run_id, new_status="failed")
