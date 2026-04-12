"""
config.py — Environment variables, agent definitions, constants.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Git provider: "github" or "gitlab"
GIT_PROVIDER: str = os.getenv("GIT_PROVIDER", "github")

# Git platform (generic names, with backward-compat aliases)
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", os.environ.get("GITHUB_WEBHOOK_SECRET", ""))
API_TOKEN: str = os.environ.get("API_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
GIT_REPO: str = os.environ.get("GIT_REPO", os.environ.get("GITHUB_REPO", ""))
BOT_USERNAME: str = os.environ.get("BOT_USERNAME", os.environ.get("BOT_GITHUB_USERNAME", ""))
GIT_BASE_URL: str = os.getenv("GIT_BASE_URL", "")  # e.g. "https://gitlab.company.com"

# Allow bot to respond to its own opened issues (for testing with single user)
ALLOW_SELF_TRIGGER: bool = os.getenv("ALLOW_SELF_TRIGGER", "false").lower() == "true"

# Backward-compat aliases (existing .env files keep working)
GITHUB_WEBHOOK_SECRET: str = WEBHOOK_SECRET
GITHUB_TOKEN: str = API_TOKEN
GITHUB_REPO: str = GIT_REPO
BOT_GITHUB_USERNAME: str = BOT_USERNAME

# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# tmux
TMUX_SESSION_NAME: str = os.getenv("TMUX_SESSION_NAME", "agent-flow")
REPO_LOCAL_PATH: str = os.getenv("REPO_LOCAL_PATH", str(Path.home() / "repo"))

# Timing
MONITOR_POLL_SECONDS: int = int(os.getenv("MONITOR_POLL_SECONDS", "30"))
IDLE_TIMEOUT_SECONDS: int = int(os.getenv("IDLE_TIMEOUT_SECONDS", "300"))

# Pipeline limits
MAX_REVIEW_CYCLES: int = int(os.getenv("MAX_REVIEW_CYCLES", "3"))
MAX_DECOMPOSITION_DEPTH: int = int(os.getenv("MAX_DECOMPOSITION_DEPTH", "1"))

# Storage
SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "/tmp/agent-flow/state.db")
PROMPT_DIR: str = os.getenv("PROMPT_DIR", "/tmp/agent-flow/prompts")
WORKTREE_DIR: str = os.getenv("WORKTREE_DIR", "/tmp/agent-flow/worktrees")

# Deduplication TTL
DEDUP_TTL_HOURS: int = int(os.getenv("DEDUP_TTL_HOURS", "24"))

# Paths relative to this file (agent-flow project root)
_HERE = Path(__file__).parent
ROLES_DIR = _HERE / "roles"
TMP_DIR = str(_HERE / "tmp")  # scratch dir for agent comment/PR body files

# Agent definitions keyed by mention handle
AGENTS = {
    "claude": {
        "mention": "@claude",
        "role": "planner",
        "cli_template": "claude -w plan-{issue_id}-{run_id} --dangerously-skip-permissions --append-system-prompt-file {roles_dir}/planner.md",
        "role_file": str(ROLES_DIR / "planner.md"),
        "completion_markers": ["STATUS: PLAN_COMPLETE", "STATUS: DECOMPOSED", "STATUS: BLOCKED", "STATUS: FAILED"],
    },
    "implementer": {
        "mention": "@implementer",
        "role": "implementer",
        "cli_template": "claude -w feature-{issue_id}-{run_id} --model sonnet --dangerously-skip-permissions --append-system-prompt-file {roles_dir}/implementer.md",
        "role_file": str(ROLES_DIR / "implementer.md"),
        "completion_markers": ["STATUS: IMPLEMENTATION_COMPLETE", "STATUS: BLOCKED", "STATUS: FAILED", "STATUS: TESTS_FAILING", "STATUS: CONFLICTS", "STATUS: CI_FAILING"],
    },
    "codex": {
        "mention": "@codex",
        "role": "reviewer",
        "cli_template": "codex -c model_instructions_file={roles_dir}/reviewer.md",
        "role_file": str(ROLES_DIR / "reviewer.md"),
        "completion_markers": ["STATUS: PLAN_APPROVED", "STATUS: PLAN_CHANGES_REQUESTED", "STATUS: APPROVED", "STATUS: CHANGES_REQUESTED", "STATUS: CI_FAILING", "STATUS: BLOCKED"],
    },
    "human": {
        "mention": "@human",
        "role": "human",
        "cli_template": None,
        "role_file": None,
        "completion_markers": [],
    },
}

VALID_MENTIONS = set(AGENTS.keys())

# Collaborator associations that are allowed to trigger agents
TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
