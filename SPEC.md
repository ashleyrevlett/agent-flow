# SPEC

## Purpose

`agent-flow` is a webhook-driven automation service that coordinates three AI agents to move work from issue intake to merged code changes.

Supported git providers:
- GitHub
- GitLab

Provider is selected via `GIT_PROVIDER` and abstracted behind `GitProvider`.

## Core Workflow

1. Issue opened
- Planner (`@claude`) is dispatched in `planning` stage.
2. Plan review
- Reviewer (`@codex`) approves or requests plan changes.
3. Implementation
- Implementer (`@implementer`) writes code and opens PR/MR.
4. Code review
- Reviewer (`@codex`) approves/merges or requests changes.
5. Completion
- Approved code transitions issue to `approved` stage.

Large issues can be decomposed by planner into child issues with dependency metadata.

## Pipeline Contracts

Agent comments on issues are the message bus.

Required conventions:
- Agent comment tag: `<!-- agent:<name> -->`
- Status line: `STATUS: <TOKEN>`
- Handoff mention on last non-empty non-code line (except terminal approvals):
- `@codex`
- `@implementer`
- `@claude`
- `@human`

Parser behavior:
- Mentions in fenced blocks, blockquotes, or inline backticks do not route.
- Dispatch routes by `(status, mention)` transition table.

## Stage Machine

Stored in `state.py` (`issue_stages` table).

Stages:
- `open`
- `planning`
- `plan_review`
- `implementing`
- `code_review`
- `approved`
- `decomposed`
- `escalated`

Valid transitions:
- `open -> planning`
- `planning -> plan_review | decomposed`
- `plan_review -> implementing | planning`
- `implementing -> code_review | planning`
- `code_review -> approved | implementing`

Escalation to `escalated` is out-of-band via `state.escalate()`.

## Run Orchestration

Runs are persisted in SQLite (`runs` table):
- statuses: `queued`, `active`, `completed`, `failed`, `stuck`, `cancelled`
- one active run per agent type at a time

Promotion:
- `try_promote(agent)` atomically promotes oldest eligible queued run
- blocked by unsatisfied dependencies or tripped circuit breaker

Dispatch:
- `dispatch.handle_event()` deduplicates by delivery id then routes
- code-review runs resolve MR/PR branch before enqueue to avoid promotion race

Spawning:
- `spawn.py` creates tmux windows and launches agent CLI
- reviewer (`@codex`) uses git worktrees
- plan/code reviewer worktrees are cleaned up on terminal statuses and recovery paths

## Resilience and Idempotency

Dedup:
- `deliveries` table stores processed webhook delivery IDs with TTL pruning.

Idempotent run completion/failure:
- `complete_run()` only updates `active -> completed`
- `fail_run()` only updates `active -> failed/stuck`
- `cancel_queued_run()` handles pre-promotion cancellation

Recovery:
- startup reconciliation marks orphaned active runs failed
- queued work is drained after restart

Stall/error handling:
- pane idle timeout marks run `stuck`
- error pattern detection marks run failed
- rate-limit/auth errors trip per-agent circuit breaker with exponential backoff

## Provider Abstraction

`provider.py` defines:
- `WebhookEvent` (normalized event shape)
- `GitProvider` protocol
- `get_provider()` factory

Provider responsibilities:
- webhook verification/parsing
- trust determination (`is_trusted`, `is_bot`, `is_agent_comment`)
- comments/MR context/branch fetch
- completion polling
- issue creation
- issue URL generation
- CLI command templates injected into prompt files

### GitHub provider

Uses `gh` CLI and GitHub webhook headers/events.

### GitLab provider

Uses `glab` CLI and GitLab webhook headers/events.

Key decisions:
- issue/MR identity normalized to project-scoped `iid`
- trust via members API lookup (`/projects/:id/members/all/:user_id`) with TTL cache
- delivery IDs synthesized from stable event fields (no provider delivery header)

## Security Model

Webhook auth:
- GitHub: HMAC signature verification (`X-Hub-Signature-256`)
- GitLab: token comparison (`X-Gitlab-Token`)

Comment trust:
- Agent-tagged comments are accepted only when provider marks sender trusted.
- Non-agent comments require trusted sender and explicit mention.

## Configuration

Primary env vars:
- `GIT_PROVIDER`
- `WEBHOOK_SECRET`
- `API_TOKEN`
- `GIT_REPO`
- `BOT_USERNAME`
- `GIT_BASE_URL` (optional; enterprise/self-managed host)
- `REPO_LOCAL_PATH`
- `TMUX_SESSION_NAME`
- `MONITOR_POLL_SECONDS`
- `IDLE_TIMEOUT_SECONDS`
- `MAX_REVIEW_CYCLES`
- `MAX_DECOMPOSITION_DEPTH`
- `SQLITE_DB_PATH`
- `PROMPT_DIR`
- `WORKTREE_DIR`
- `DEDUP_TTL_HOURS`

Backward-compatible aliases are supported for GitHub-prefixed env vars.

## Module Map

- `main.py`: process startup and task orchestration
- `webhook.py`: FastAPI receiver
- `dispatch.py`: routing, transition enforcement, dispatch
- `state.py`: SQLite state, transitions, queue, breakers, deps
- `monitor.py`: active-run health monitoring and completion detection
- `spawn.py`: tmux and worktree lifecycle
- `notifications.py`: Telegram integration and commands
- `provider.py`: provider contract/factory
- `providers/github.py`: GitHub provider impl
- `providers/gitlab.py`: GitLab provider impl
- `prompts/*.py`: per-run prompt file builders
- `roles/*.md`: persistent role instructions for each agent type

## Operational Notes

- Agents run in tmux windows under one session.
- Reviewer worktrees are created under `WORKTREE_DIR` and removed after run end.
- Queue draining occurs after completion/failure/dependency satisfaction.
- Monitor loop is the primary liveness/completion mechanism; pane exit is not trusted.

## Testing

Automated tests cover:
- State machine and queue behavior (`test_state.py`)
- Routing, mention/status parsing, auth filtering (`test_dispatch_routing.py`)
- GitHub provider: webhook verification, payload parsing, trust, URLs, CLI env (`test_provider_github.py`)
- GitLab provider: token verification, Note Hook parsing, iid extraction, dedup, URLs, paginated JSON (`test_provider_gitlab.py`)
- Provider selection and config env alias fallback (`test_provider_selection.py`)
