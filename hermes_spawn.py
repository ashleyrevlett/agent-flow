"""
hermes_spawn.py — Agent spawning via Hermes Agent. Replaces tmux send-keys
with interactive Hermes sessions that can handle trust prompts, errors,
and bidirectional CLI interaction.

Hermes is a reactive executor — it runs the CLI, handles interactive prompts,
and reports completion. It does NOT make pipeline decisions. Python owns the
state machine and all routing logic.

Tmux windows are still created for observability (tmux attach to watch),
but Hermes manages the actual CLI interaction via its terminal tool with
PTY support.
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from config import TMUX_SESSION_NAME, ROLES_DIR

logger = logging.getLogger(__name__)

# Map agent names to their role files (used as context for Hermes)
_AGENT_ROLES = {
    "claude": "planner",
    "implementer": "implementer",
    "codex": "reviewer",
}

# Hermes system prompt — minimal, reactive, no pipeline knowledge
_RUNNER_SYSTEM_PROMPT = """\
You are a CLI runner for an automated development pipeline. Your ONLY job:

1. Run the exact command you are given via the terminal tool with pty=true
2. If you see a trust/confirmation prompt ("Do you trust", "Yes, continue", \
"y/n"), accept it by writing "y" or pressing Enter as appropriate
3. Wait for the CLI process to become ready for input
4. When the CLI is ready, send it the task instruction you were given
5. Monitor the process until it exits on its own
6. If the process appears stuck (no output for 5+ minutes), report "STUCK"
7. If you see a fatal error or crash, report "ERROR: <brief description>"
8. When the process exits, report "DONE"

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
Run this CLI tool in a tmux window for observability, then interact with it.

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
    Launch a Hermes agent session to run the specified CLI tool.
    Returns a session identifier (used as tmux_window equivalent in state DB).

    The Hermes agent runs in a background thread. When agent.chat() returns,
    the run is complete — dispatch/monitor handles the rest.
    """
    from run_agent import AIAgent

    cli_command = _build_cli_command(agent_name, issue_id, run_id)
    task_message = _build_task_message(agent_name, cli_command, repo_path, prompt_file_path)

    session_id = f"hermes-{agent_name}-{issue_id}-{run_id}"

    def _run():
        try:
            logger.info("Hermes session %s starting for run %d", session_id, run_id)

            agent = AIAgent(
                model="anthropic/claude-sonnet-4-5-20250514",
                ephemeral_system_prompt=_RUNNER_SYSTEM_PROMPT,
                enabled_toolsets=["terminal"],
                max_iterations=30,
            )

            response = agent.chat(task_message)

            logger.info(
                "Hermes session %s completed for run %d: %s",
                session_id, run_id, response[:200] if response else "(no response)",
            )

            # Signal completion — import here to avoid circular import
            import state
            if response and "ERROR" in response:
                state.fail_run(run_id, new_status="failed")
            elif response and "STUCK" in response:
                state.fail_run(run_id, new_status="stuck")
            else:
                state.complete_run(run_id)

        except Exception:
            logger.exception("Hermes session %s crashed for run %d", session_id, run_id)
            import state
            state.fail_run(run_id)
        finally:
            _active_threads.pop(run_id, None)

    thread = threading.Thread(target=_run, name=session_id, daemon=True)
    _active_threads[run_id] = thread
    thread.start()

    logger.info("Spawned Hermes session: %s (thread %s)", session_id, thread.name)
    return session_id


def is_session_alive(run_id: int) -> bool:
    """Check if a Hermes session thread is still running."""
    thread = _active_threads.get(run_id)
    return thread is not None and thread.is_alive()


def get_active_sessions() -> list[int]:
    """Return run_ids of all active Hermes sessions."""
    return [rid for rid, t in _active_threads.items() if t.is_alive()]


def kill_session(run_id: int):
    """Best-effort kill of a Hermes session.

    Hermes agent threads are daemon threads — they'll die when the process exits.
    For explicit kill, we can't safely kill a thread in Python, but we can
    mark the run as failed so the queue unblocks.
    """
    import state
    thread = _active_threads.pop(run_id, None)
    if thread:
        logger.warning("Marking Hermes session for run %d as failed (cannot kill thread)", run_id)
        state.fail_run(run_id, new_status="failed")
