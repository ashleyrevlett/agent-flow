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
      ├─ @claude    → tmux: `claude` (Opus, planner)
      ├─ @implementer → tmux: `claude --model sonnet` (implementer)
      ├─ @codex     → tmux: `codex` (reviewer)
      ├─ @human     → Telegram notification
      │
   monitor.py — periodic tmux pane health checks
      │
   hindsight.py — on issue close, extract lessons to SQLite
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
| `issues.closed` | Issue closed | → hindsight.py |

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
  → webhook: issues.closed → hindsight.py extracts lessons
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
├── hindsight.py         # Post-close lesson extraction via Ollama or local model
├── telegram.py          # Bot for @human escalation + issue creation
├── prompts/
│   ├── planner.py       # Builds prompt file for @claude
│   ├── implementer.py   # Builds prompt file for @implementer
│   └── reviewer.py      # Builds prompt file for @codex
├── memory/
│   └── store.py         # SQLite + FTS5 for lessons/tribal knowledge
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
- `SQLITE_DB_PATH` — path to lessons DB
- `BOT_GITHUB_USERNAME` — to filter self-triggered webhooks

Agent definitions dict keyed by mention handle, containing: mention string, role name, CLI command template, prompt builder module ref, completion markers.

### 2. webhook.py

FastAPI app with `POST /webhook` endpoint.

- Verify `X-Hub-Signature-256` with HMAC
- Parse `X-GitHub-Event` header for event type
- Extract `action` from payload
- For comment events: extract `payload["comment"]["body"]`, regex scan for `@(claude|implementer|codex|human)\b`
- Self-mention guard: agent comments are tagged with `<!-- agent:NAME -->` — when the comment author is the bot user AND the comment has this tag, extract the trailing @mention as a handoff signal (don't treat it as a new task)
- Deduplication via `X-GitHub-Delivery` header (in-memory set of recent IDs)
- Dispatch in `BackgroundTasks` so webhook returns 200 immediately

### 3. dispatch.py

Core routing function `handle_event(event_type, action, payload)`:

1. Determine target agent from event type + @mention
2. Fetch full context via `gh` CLI:
   - Issue thread: `gh api repos/{owner}/{repo}/issues/{number}/comments`
   - PR diff: `gh pr diff {number}`
   - Issue body: from payload
3. Query `memory.store.search()` for relevant lessons
4. Call appropriate prompt builder → writes prompt file to `/tmp/agent-flow/prompts/{agent}-{issue_id}.md`
5. Call `spawn.create_agent_window()` with agent config and prompt file path
6. Track review cycle count per issue (in-memory dict or SQLite)
7. If cycle count > MAX_REVIEW_CYCLES: route to @human instead

### 4. spawn.py

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

### 5. prompts/planner.py

Writes a prompt file containing:
- Role: "You are the PLANNER. Analyze issues and create detailed implementation plans."
- Issue context: title, body, full comment thread
- Relevant memory snippets from lessons DB
- Instructions: post plan as GitHub comment via `gh issue comment`
- Handoff rule: end comment with `@implementer please implement the tasks above`
- Comment format: must start with `<!-- agent:claude -->`
- Exact `gh` command template for posting

### 6. prompts/implementer.py

Same structure but:
- Role: "You are the IMPLEMENTER. Write code to fulfill the plan."
- Includes plan text from planner's comment
- Instructions: create feature branch, implement, commit, push, open PR with `Closes #{issue_number}`
- Handoff: end PR description/comment with `@codex please review`
- Comment tag: `<!-- agent:implementer -->`

### 7. prompts/reviewer.py

Same structure but:
- Role: "You are the REVIEWER. Review PRs for correctness, quality, and completeness."
- Includes PR diff and description
- Instructions: post review via `gh pr review`
- Handoff options:
  - Approve → `@claude implementation approved`
  - Request changes → `@implementer please address the feedback above`
  - Stuck → `@human please review`
- Comment tag: `<!-- agent:codex -->`

### 8. monitor.py

Async loop running alongside FastAPI.

Every `MONITOR_POLL_SECONDS`:
1. List active agent windows
2. For each: capture pane, hash content, compare to previous
3. If unchanged for `IDLE_TIMEOUT_SECONDS`: mark stuck, send Telegram alert
4. Scan for error patterns: `Error:`, `Traceback`, `FATAL`, `rate limit`, `authentication failed`
5. On error: Telegram alert with pane excerpt

For Claude Code (alt-screen buffer): also poll GitHub for new comments tagged with `<!-- agent:X -->` as primary completion signal.

### 9. telegram.py

Using `python-telegram-bot`:
- `send_notification(message, issue_url)` — on @human mention
- `send_stuck_alert(agent, window, excerpt)` — from monitor
- `/create_issue <title>` command — human creates issue via Telegram
- `/status` command — show active agent windows
- `/kill <window>` command — kill stuck agent

### 10. memory/store.py

SQLite with FTS5:
```sql
CREATE TABLE lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER,
    repo TEXT,
    category TEXT,  -- bug_pattern, architecture, process, tool_usage
    summary TEXT,
    detail TEXT,
    tags TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE lessons_fts USING fts5(summary, detail, tags);
```

Functions: `store_lesson()`, `search(query, limit=5)`, `get_recent(n=10)`

### 11. hindsight.py

Triggered by `issues.closed` webhook:
1. Fetch full issue thread via `gh api`
2. Fetch associated PRs and diffs
3. Feed through a local model (or Claude via tmux one-shot) to extract lessons as structured JSON
4. Parse and store via `memory.store.store_lesson()`

Note: hindsight extraction model TBD — could use Ollama/Qwen locally when available, or a lightweight extraction without LLM (regex + heuristics for v1).

### 12. CLAUDE.md for target repo

Placed in the repo agents work on. Instructs Claude Code on the pipeline protocol:
- Always post results via `gh issue comment` or `gh pr comment`
- Always tag comments with `<!-- agent:ROLE -->`
- Always end with exactly one @mention handoff
- Branch naming: `feature/{issue_number}-{short_desc}`
- Commit format: conventional commits referencing issue
- PR body: include `Closes #{issue_number}`

### 13. main.py

Entry point:
- Create tmux session
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
- `memory/store.py` — SQLite schema, insert/search
- `spawn.py` — tmux management, send-keys, capture-pane

### Phase 2 — Core Pipeline
- `prompts/planner.py`, `implementer.py`, `reviewer.py`
- `dispatch.py` — routing, context fetching, prompt assembly
- `webhook.py` — FastAPI endpoint, signature verification

### Phase 3 — Monitoring & Communication
- `monitor.py` — pane polling, completion/stuck detection
- `telegram.py` — notifications, commands

### Phase 4 — Learning Loop
- `hindsight.py` — lesson extraction
- Wire memory injection into prompt builders

### Phase 5 — Integration
- `main.py` — startup orchestration
- CLAUDE.md for target repo
- End-to-end test with a real issue
- ngrok tunnel setup

## Verification

1. **Webhook delivery**: Create a test issue, verify FastAPI receives the event
2. **Dispatch + spawn**: Verify tmux window opens with correct CLI and prompt
3. **Agent handoff**: Verify agent posts comment with @mention, next webhook fires, next agent spawns
4. **Review cycle**: Verify reviewer can request changes and implementer responds (up to 3 cycles)
5. **Escalation**: Verify @human triggers Telegram notification
6. **Completion**: Verify merge → CI → issue closes → hindsight runs
7. **Memory**: Verify lessons stored and injected into subsequent prompts

## Risks

| Risk | Mitigation |
|---|---|
| Claude Code alt-screen makes pane capture unreliable | Poll GitHub for new comments as primary completion signal |
| Webhook storms from agent comments | Dedup by delivery ID, filter by `<!-- agent:X -->` tags |
| Agents hang on permission prompts | Use `--dangerously-skip-permissions` |
| tmux session disappears | Monitor checks session existence, recreates if needed |
| Review cycle loops | Hard max of 3 cycles, then @human escalation |
| Concurrent agents on same repo | Each agent works on a separate branch per issue |
