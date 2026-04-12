# Agent Flow — Autonomous Development Pipeline

## Context

We're building an autonomous development pipeline triggered by GitHub webhooks. A lightweight Python dispatcher parses @mentions in GitHub comments and spawns the appropriate AI coding agent in a tmux window. Agents self-direct handoffs by ending their GitHub comments with @mentions. No orchestrator LLM — the dispatcher is pure pattern matching.

All agents use subscription-plan CLI tools (not API billing). GitHub comments are the message bus.

## Architecture

```
GitHub Webhook (issues, comments, PRs, CI)
      │
   FastAPI receiver (webhook.py)
      │
   dispatch.py — parse @mention, fetch context, write prompt file
      │
      ├─ @claude       → tmux: `claude` (Opus, planner)
      ├─ @implementer  → tmux: `claude --model sonnet` (implementer)
      ├─ @codex        → tmux: `codex` (reviewer)
      ├─ @human        → Telegram notification
      │
   monitor.py — periodic tmux pane health checks
      │
   state.db — durable pipeline state (runs, dedup, cycles)
```

## Agents

| Handle | Role | CLI Command | Model | Billing |
|---|---|---|---|---|
| @claude | Planner | `claude` | Opus (default on Max) | CC subscription |
| @implementer | Implementer | `claude --model sonnet` | Sonnet | CC subscription |
| @codex | Reviewer | `codex` | Codex | Codex subscription |

## Event Routing

| GitHub Event | Action | Target |
|---|---|---|
| `issues.opened` | New issue created | → @claude (planner) |
| `issue_comment.created` | Comment with @mention | → mentioned agent |
| `pull_request.opened` | PR opened | → @codex (reviewer) |
| `pull_request_review_comment.created` | PR comment with @mention | → mentioned agent |
| `workflow_run.completed` | CI finished | → log result |

## Lifecycle

```
Human creates issue (or via Telegram → gh issue create)
  → webhook: issues.opened → dispatch @claude
  → @claude posts plan as issue comment, ends with "@implementer please implement"
  → webhook: issue_comment → dispatch @implementer
  → @implementer creates branch, writes code, opens PR, ends with "@codex please review"
  → webhook: pull_request.opened → dispatch @codex
  → @codex reviews PR
      → approve: "@claude implementation approved" → merge
      → request changes: "@implementer please address feedback" → cycle (max 3)
      → stuck: "@human please review"
  → merge to main → CI runs → issue auto-closes ("Closes #N" in PR)
```

Max 3 review cycles before escalating to @human.

## Project Structure

```
agent-flow/
├── main.py              # Entry point: FastAPI + monitor loop + Telegram bot
├── config.py            # Env vars, agent definitions, constants
├── webhook.py           # FastAPI routes, signature verification, event parsing
├── dispatch.py          # @mention parsing, context fetching, prompt building, tmux spawning
├── spawn.py             # tmux session/window management, send-keys, capture-pane
├── monitor.py           # Periodic pane health checks, stuck detection, Telegram alerts
├── telegram.py          # Bot for @human escalation + issue creation
├── state.py             # SQLite pipeline state — runs, dedup, cycle tracking
├── prompts/
│   ├── planner.py       # Builds prompt file for @claude
│   ├── implementer.py   # Builds prompt file for @implementer
│   └── reviewer.py      # Builds prompt file for @codex
├── requirements.txt
└── .env.example
```

## Component Details

### 1. config.py

Environment variables:
- `GITHUB_WEBHOOK_SECRET` — webhook signature verification
- `GITHUB_TOKEN` — PAT for gh CLI (set in agent shell env too)
- `GITHUB_REPO` — owner/repo
- `TELEGRAM_BOT_TOKEN` — bot token from @BotFather
- `TELEGRAM_CHAT_ID` — target chat for alerts
- `TMUX_SESSION_NAME` — e.g. "agent-flow"
- `REPO_LOCAL_PATH` — path to local repo clone
- `MONITOR_POLL_SECONDS` — default 30
- `IDLE_TIMEOUT_SECONDS` — default 300
- `MAX_REVIEW_CYCLES` — default 3
- `SQLITE_DB_PATH` — path to state DB
- `BOT_GITHUB_USERNAME` — to filter self-triggered webhooks

Agent definitions dict keyed by mention handle, containing: mention string, role name, CLI command template, prompt builder module ref, completion markers.

### 2. webhook.py

FastAPI app with `POST /webhook` endpoint.

- Verify `X-Hub-Signature-256` with HMAC
- Parse `X-GitHub-Event` header for event type
- Extract `action` from payload
- For comment events: extract `payload["comment"]["body"]`, regex scan for `@(claude|implementer|codex|human)\b`
- Self-mention guard: agent comments are tagged with `<!-- agent:NAME -->` — when the comment author is the bot user AND the comment has this tag, extract the trailing @mention as a handoff signal (don't treat it as a new task)
- Deduplication via `X-GitHub-Delivery` header checked against `state.db`
- Dispatch in `BackgroundTasks` so webhook returns 200 immediately

### 3. state.py

SQLite database for durable pipeline state. Survives restarts.

```sql
-- Active agent runs
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    agent TEXT NOT NULL,           -- claude, implementer, codex
    tmux_window TEXT,              -- window name for monitoring
    status TEXT NOT NULL,          -- active, completed, failed, stuck
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Review cycle tracking
CREATE TABLE cycles (
    issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    cycle_count INTEGER DEFAULT 0,
    escalated BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (issue_number, repo)
);

-- Webhook deduplication
CREATE TABLE deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Functions:
- `record_run(issue_number, repo, agent, tmux_window)` — insert new run
- `update_run(run_id, status)` — mark completed/failed/stuck
- `get_active_runs()` — for monitor to reconnect after restart
- `is_duplicate(delivery_id)` — check + insert, return bool
- `increment_cycle(issue_number, repo)` — bump count, return new count
- `get_cycle_count(issue_number, repo)` — current count
- `prune_deliveries(max_age_hours=1)` — cleanup old dedup entries

### 4. dispatch.py

Core routing function `handle_event(event_type, action, payload)`:

1. Check `state.is_duplicate(delivery_id)` — skip if seen
2. Determine target agent from event type + @mention
3. Fetch full context via `gh` CLI:
   - Issue thread: `gh api repos/{owner}/{repo}/issues/{number}/comments`
   - PR diff: `gh pr diff {number}`
   - Issue body: from payload
4. For reviewer handoffs: check `state.get_cycle_count()` — if >= MAX_REVIEW_CYCLES, route to @human instead
5. Call appropriate prompt builder → writes prompt file to `/tmp/agent-flow/prompts/{agent}-{issue_id}.md`
6. Call `spawn.create_agent_window()` with agent config and prompt file path
7. Record run in `state.record_run()`
8. For reviewer → implementer handoffs: `state.increment_cycle()`

### 5. spawn.py

tmux management via `subprocess.run(["tmux", ...])` (not libtmux — more reliable for automation).

Functions:
- `ensure_session()` — create master tmux session if not exists
- `create_agent_window(agent_name, issue_id, cli_command, prompt_file_path, repo_path)`:
  1. Create named window: `tmux new-window -t SESSION -n "{agent}-{issue_id}"`
  2. Send `cd {repo_path}` via send-keys
  3. Send CLI command via send-keys (e.g. `claude --dangerously-skip-permissions`)
  4. Wait briefly for CLI to initialize
  5. Send single-line instruction: `"Read and execute the task in {prompt_file_path}"`
- `capture_pane(window_name, lines=500)` — `tmux capture-pane -p -S -{lines}`
- `kill_window(window_name)` — cleanup
- `list_windows()` — for monitor

**Prompt delivery**: Write full multi-line prompt to a file on disk. Send a single-line instruction via send-keys telling the agent to read that file. This avoids multi-line send-keys issues.

### 6. prompts/planner.py

Writes a prompt file containing:
- Role: "You are the PLANNER. Analyze issues and create detailed implementation plans."
- Issue context: title, body, full comment thread
- Instructions: post plan as GitHub comment via `gh issue comment`
- Handoff rule: end comment with `@implementer please implement the tasks above`
- Comment format: must start with `<!-- agent:claude -->`
- Exact `gh` command template for posting

### 7. prompts/implementer.py

Same structure but:
- Role: "You are the IMPLEMENTER. Write code to fulfill the plan."
- Includes plan text from planner's comment
- Instructions: create feature branch, implement, commit, push, open PR with `Closes #{issue_number}`
- Handoff: end PR description/comment with `@codex please review`
- Comment tag: `<!-- agent:implementer -->`

### 8. prompts/reviewer.py

Same structure but:
- Role: "You are the REVIEWER. Review PRs for correctness, quality, and completeness."
- Includes PR diff and description
- Instructions: post review via `gh pr review`
- Handoff options:
  - Approve → `@claude implementation approved`
  - Request changes → `@implementer please address the feedback above`
  - Stuck → `@human please review`
- Comment tag: `<!-- agent:codex -->`

### 9. monitor.py

Async loop running alongside FastAPI.

Every `MONITOR_POLL_SECONDS`:
1. Query `state.get_active_runs()` — reconnects to existing windows after restart
2. For each active run: capture pane, hash content, compare to previous
3. If unchanged for `IDLE_TIMEOUT_SECONDS`: `state.update_run(id, "stuck")`, send Telegram alert
4. Scan for error patterns: `Error:`, `Traceback`, `FATAL`, `rate limit`, `authentication failed`
5. On error: `state.update_run(id, "failed")`, Telegram alert with pane excerpt

For Claude Code (alt-screen buffer): also poll GitHub for new comments tagged with `<!-- agent:X -->` as primary completion signal.

On startup: check `state.get_active_runs()` against actual tmux windows. Mark orphaned runs as failed.

### 10. telegram.py

Using `python-telegram-bot`:
- `send_notification(message, issue_url)` — on @human mention
- `send_stuck_alert(agent, window, excerpt)` — from monitor
- `/create_issue <title>` command — human creates issue via Telegram
- `/status` command — show active agent windows (from state.db)
- `/kill <window>` command — kill stuck agent, mark run as failed

### 11. CLAUDE.md for target repo

Placed in the repo agents work on. Instructs Claude Code on the pipeline protocol:
- Always post results via `gh issue comment` or `gh pr comment`
- Always tag comments with `<!-- agent:ROLE -->`
- Always end with exactly one @mention handoff
- Branch naming: `feature/{issue_number}-{short_desc}`
- Commit format: conventional commits referencing issue
- PR body: include `Closes #{issue_number}`

### 12. main.py

Entry point:
- Create tmux session
- Initialize state.db (create tables if not exist)
- Start FastAPI (uvicorn)
- Start monitor loop (asyncio task)
- Start Telegram bot (asyncio task)
- Expose on port 8000, use ngrok/cloudflare tunnel for webhook delivery

## Dependencies

```
fastapi
uvicorn
httpx
pydantic-settings
python-telegram-bot
python-dotenv
```

System: `tmux`, `gh` CLI, `claude` CLI, `codex` CLI, `ngrok` (dev)

## Implementation Phases

### Phase 1 — Foundation
- `config.py` — env vars, agent definitions
- `state.py` — SQLite schema, runs/dedup/cycles functions
- `spawn.py` — tmux management, send-keys, capture-pane

### Phase 2 — Core Pipeline
- `prompts/planner.py`, `implementer.py`, `reviewer.py`
- `dispatch.py` — routing, context fetching, prompt assembly, state tracking
- `webhook.py` — FastAPI endpoint, signature verification, dedup via state.db

### Phase 3 — Monitoring & Communication
- `monitor.py` — pane polling, completion/stuck detection, restart recovery
- `telegram.py` — notifications, commands

### Phase 4 — Integration
- `main.py` — startup orchestration
- CLAUDE.md for target repo
- End-to-end test with a real issue
- ngrok tunnel setup

## Verification

1. **Webhook delivery**: Create a test issue, verify FastAPI receives the event
2. **Dispatch + spawn**: Verify tmux window opens with correct CLI and prompt
3. **Agent handoff**: Verify agent posts comment with @mention, next webhook fires, next agent spawns
4. **Review cycle**: Verify reviewer can request changes and implementer responds (up to 3 cycles)
5. **Escalation**: Verify @human triggers Telegram notification after 3 cycles
6. **Dedup**: Send same webhook twice, verify only one agent spawns
7. **Restart recovery**: Kill main.py, restart, verify monitor reconnects to active tmux windows
8. **Completion**: Verify merge → CI → issue closes

## Risks

| Risk | Mitigation |
|---|---|
| Claude Code alt-screen makes pane capture unreliable | Poll GitHub for new comments as primary completion signal |
| Webhook storms from agent comments | Dedup by delivery ID in state.db, filter by `<!-- agent:X -->` tags |
| Agents hang on permission prompts | Use `--dangerously-skip-permissions` |
| tmux session disappears | Monitor checks session existence, recreates if needed |
| Review cycle loops | Hard max of 3 cycles, then @human escalation (persisted in state.db) |
| Concurrent agents on same repo | Each agent works on a separate branch per issue |
| Restart loses track of active agents | state.db persists runs; monitor reconciles against tmux on startup |
