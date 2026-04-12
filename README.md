# Agent Flow

`agent-flow` is an automated issue-to-merge pipeline for GitHub or GitLab.

It routes work across three agents:
- `@claude` plans
- `@implementer` implements
- `@codex` reviews and approves/requests changes

The system runs locally with CLI tools (`claude`, `codex`, `gh`/`glab`) and tmux sessions, so usage stays on subscription plans rather than API orchestration billing.

## What You Get

- End-to-end workflow from issue creation to merged PR/MR
- Automatic handoffs using `STATUS:` lines and `@mentions` in issue comments
- Retry loops for plan/code review with escalation limits
- Queueing, dependency handling, and crash/restart recovery
- Optional Telegram alerts for escalations and stuck runs
- Provider selection via `GIT_PROVIDER=github|gitlab`

## Quick Start

1. Install dependencies:
```sh
pip install -r requirements.txt
```
2. Configure env:
```sh
cp .env.example .env
```
3. Set at least:
- `GIT_PROVIDER` (`github` or `gitlab`)
- `WEBHOOK_SECRET`
- `GIT_REPO`
- `REPO_LOCAL_PATH`
4. Run:
```sh
python main.py
```
5. Point your provider webhook to:
- `https://<your-tunnel-or-host>/webhook`

## Requirements

- Python 3.11+
- tmux
- Claude Code CLI
- Codex CLI
- GitHub CLI (`gh`) when `GIT_PROVIDER=github`
- GitLab CLI (`glab`) when `GIT_PROVIDER=gitlab`

## How to Use

### Start a task

Create an issue in your repo. That's it — the webhook fires, `@claude` picks it up and begins planning.

No special labels or formatting required. Write the issue like you'd write it for a human developer: describe what you want done, include context, acceptance criteria, etc.

### Pipeline flow

1. **Issue created** — `@claude` (planner) reads the issue and posts a plan as a comment
2. **Plan review** — `@codex` (reviewer) evaluates the plan, either approves or requests changes
3. **Implementation** — `@implementer` writes code, opens a PR/MR, posts a handoff comment
4. **Code review** — `@codex` reviews the diff, either approves+merges or requests changes
5. Each review phase retries up to `MAX_REVIEW_CYCLES` (default 3) before escalating

### Intervene at any point

Post a comment on the issue with `@human` on the last line to escalate. The pipeline pauses and sends a Telegram alert (if configured).

### Declare dependencies between issues

Add `Depends-on: #N` in the issue body. The dependent issue won't start until issue #N is closed.

```
Implement the search API.

Depends-on: #12
```

### Decompose large issues

If `@claude` decides an issue is too large, it can decompose it into child issues (each with `Parent: #N` in the body) and post `STATUS: DECOMPOSED`. The children run the pipeline independently.

### Handoff protocol

Agents communicate via issue comments using two conventions:
- **`STATUS: TOKEN`** — signals the current state (e.g., `STATUS: PLAN_COMPLETE`, `STATUS: IMPLEMENTATION_COMPLETE`, `STATUS: APPROVED`)
- **`@agent`** — routes to the next agent (e.g., `@codex please review`, `@implementer please implement`)

Both must appear in the same comment. The `STATUS:` line can go anywhere; the `@mention` must be on the last non-empty line.

### Watch progress

- **Telegram**: `/status` shows active runs, queue depth, and breaker state
- **HTTP**: `GET /status` returns the same info as JSON
- **tmux**: `tmux attach -t agent-flow` to watch agent sessions live

## Scripts

### `scripts/status`

Print a digest of the current pipeline state: agent activity, active issues, tmux windows, and recent runs.

```sh
python scripts/status
```

### `scripts/reset-breaker`

Clear circuit breaker(s) when an agent is rate-limited or stuck in a tripped state.

```sh
python scripts/reset-breaker claude      # reset one agent
python scripts/reset-breaker codex       # reset another
python scripts/reset-breaker --all       # reset all agents
```

## Docs

- Developer spec and architecture: `SPEC.md`
- Example configuration: `.env.example`

## License

MIT
