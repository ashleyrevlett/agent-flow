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
   state.db — durable pipeline state (runs, dedup, cycles, decompositions)
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
| `pull_request.opened` | PR opened | → ignore (handoff comes via issue comment) |
| `issues.closed` | Issue closed | → `state.satisfy_dependency()` — unblocks dependent issues, triggers queue drain |
| `workflow_run.completed` | CI finished | → log result |

## Lifecycle

```
Human creates issue (or via Telegram → gh issue create)
  → webhook: issues.opened → dispatch @claude
  → @claude planner chooses one of two modes:
      Mode A (direct):
        - post STATUS: PLAN_COMPLETE
        - end with "@implementer please implement the tasks above"
        - webhook: issue_comment → dispatch @implementer
      Mode B (decompose):
        - create child issues with explicit sequence/dependencies and parent link
        - post STATUS: DECOMPOSED on parent (no implementer handoff yet)
        - when ready, @claude posts handoff on the next child issue:
          "@implementer please implement issue #X"
  → @implementer creates branch, writes code, opens PR
  → @implementer posts issue comment ending with "@codex please review PR #N"
  → webhook: issue_comment → dispatch @codex
  → @codex reviews PR
      → approve: merge PR, post STATUS: APPROVED (no @mention — pipeline done)
      → request changes: "@implementer please address feedback" → cycle (max 3)
      → stuck: "@human please review"
  → merge to main → CI runs → issue auto-closes ("Closes #N" in PR)
```

Max 3 review cycles before escalating to @human.
Decomposition depth is capped (default: 1 level of child issues) to prevent recursive planning loops.

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
├── state.py             # SQLite pipeline state — runs, dedup, cycle, decomposition tracking
├── roles/
│   ├── planner.md       # System prompt for @claude (injected via --append-system-prompt-file)
│   ├── implementer.md   # System prompt for @implementer (injected via --append-system-prompt-file)
│   └── reviewer.md      # System prompt for @codex (injected via -c model_instructions_file=)
├── prompts/
│   ├── planner.py       # Builds task-specific prompt file for @claude
│   ├── implementer.py   # Builds task-specific prompt file for @implementer
│   └── reviewer.py      # Builds task-specific prompt file for @codex
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
- `MAX_DECOMPOSITION_DEPTH` — default 1
- `SQLITE_DB_PATH` — path to state DB
- `BOT_GITHUB_USERNAME` — to filter self-triggered webhooks

Agent definitions dict keyed by mention handle, containing: mention string, role name, CLI command template, prompt builder module ref, completion markers.

### 2. webhook.py

FastAPI app with `POST /webhook` endpoint.

- Verify `X-Hub-Signature-256` with HMAC
- Parse `X-GitHub-Event` header for event type
- Extract `action` from payload
- **Handoff parsing (strict)**: only extract @mentions from the **last non-empty line** of a comment body, and only if the comment contains the `<!-- agent:NAME -->` tag. This prevents misfires from quoted text, code blocks, or casual @mentions mid-comment. Regex: scan the final line for `@(claude|implementer|codex|human)\b`.
- For human-authored comments (no `<!-- agent:NAME -->` tag): also only parse @mentions from the last line, **but only if the comment author is a repo collaborator** (`payload["comment"]["author_association"]` is `OWNER`, `MEMBER`, or `COLLABORATOR`). Ignore comments from external users entirely. This prevents unauthorized agent invocation on public repos.
- Ignore @mentions inside markdown code blocks (`` ` `` or ``` ``` ```) and blockquotes (`>`).
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
    status TEXT NOT NULL,          -- queued, active, completed, failed, stuck
    prompt_file TEXT,              -- path to task prompt file (needed to spawn from queue)
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

-- Planner decomposition tracking
CREATE TABLE decompositions (
    parent_issue_number INTEGER NOT NULL,
    child_issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned', -- planned, in_progress, completed, blocked
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, child_issue_number)
);

-- Issue dependency tracking (planner sets these when decomposing)
CREATE TABLE dependencies (
    issue_number INTEGER NOT NULL,
    depends_on_issue INTEGER NOT NULL,
    repo TEXT NOT NULL,
    satisfied BOOLEAN NOT NULL DEFAULT FALSE,  -- set TRUE when depends_on_issue closes
    PRIMARY KEY (repo, issue_number, depends_on_issue)
);

CREATE TABLE decomposition_meta (
    parent_issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    decomposition_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, parent_issue_number)
);
```

Functions:
- `enqueue_run(issue_number, repo, agent, prompt_file)` — insert new run with status `queued`, returns `run_id`. Called before any spawn attempt.
- `try_promote(agent)` — atomically: if no `active` run exists for this agent type, find the oldest `queued` run **whose issue has no unsatisfied dependencies** (`is_blocked() == False`), promote it to `active`, and return it. If an `active` run exists or all queued runs are blocked, return `None`. Uses `UPDATE ... WHERE` with subqueries to prevent races.
- `update_run_window(run_id, tmux_window)` — set window name after successful spawn
- `complete_run(run_id)` — mark `completed`, set `completed_at`, then call `try_promote(agent)` to auto-dequeue the next job
- `fail_run(run_id, status)` — mark `failed` or `stuck`, then call `try_promote(agent)` to auto-dequeue
- `get_active_runs()` — all runs with status `active`, for monitor to reconnect after restart
- `get_queue_depth(agent=None)` — count of `queued` runs, optionally filtered by agent type (for /status command)
- `is_duplicate(delivery_id)` — atomic `INSERT ... ON CONFLICT DO NOTHING`, returns `True` if 0 rows affected (already seen). No separate check step — single statement eliminates race window under concurrent webhook handling.
- `increment_cycle(issue_number, repo)` — bump count, return new count
- `get_cycle_count(issue_number, repo)` — current count
- `prune_deliveries(max_age_hours=24)` — cleanup old dedup entries. Default 24h is conservative; GitHub can redeliver webhooks for up to several hours after initial failure. Configurable via `DEDUP_TTL_HOURS` env var.
- `record_dependency(issue_number, depends_on_issue, repo)` — store dependency link
- `satisfy_dependency(closed_issue, repo)` — mark all dependencies on `closed_issue` as satisfied. Called when an issue closes (via webhook or PR merge). After satisfying, check if any queued runs for newly-unblocked issues can be promoted.
- `is_blocked(issue_number, repo)` — returns `True` if any unsatisfied dependencies exist for this issue
- `record_decomposition(parent_issue, child_issue, repo, sequence_index)` — store child issue mapping
- `mark_decomposition_done(parent_issue, repo)` — idempotency flag to prevent duplicate child creation on retries
- `is_decomposition_done(parent_issue, repo)` — check if parent already decomposed
- `list_children(parent_issue, repo)` — ordered child issues for sequencing

### 4. dispatch.py

Core routing function `handle_event(event_type, action, payload)`:

1. Check `state.is_duplicate(delivery_id)` — skip if seen
2. Determine target agent from event type + @mention
3. For reviewer handoffs: check `state.get_cycle_count()` — if >= MAX_REVIEW_CYCLES, route to @human instead
4. For reviewer → implementer handoffs: `state.increment_cycle()`
5. Fetch full context via `gh` CLI:
   - Issue thread: `gh api repos/{owner}/{repo}/issues/{number}/comments`
   - PR diff: `gh pr diff {number}`
   - Issue body: from payload
6. Call appropriate prompt builder → writes prompt file to `/tmp/agent-flow/prompts/{agent}-{issue_id}-{timestamp}.md`
7. **Enqueue**: `run_id = state.enqueue_run(issue_number, repo, agent, prompt_file)`
8. **Try to promote**: `run = state.try_promote(agent)` — if another run of this agent type is already active, returns `None` and the job stays queued
9. If promoted: call `spawn.create_agent_window(run.id, agent_name, issue_id, ...)`, then `state.update_run_window(run.id, tmux_window)`
10. If not promoted: job waits in queue. It will be auto-spawned when the current active run of this agent type completes (via `complete_run` or `fail_run`).

**Queue drain function** `drain_queue(agent)`:
Called by `state.complete_run()` and `state.fail_run()` after marking a run done. Also called by monitor on startup to resume any orphaned queued runs.
1. `run = state.try_promote(agent)`
2. If `run`: spawn tmux window, update run with window name
3. If not: no queued work for this agent type, nothing to do

**Dependency parsing** (on `issues.opened`):
- Parse issue body for `Depends-on: #X` lines (regex: `Depends-on:\s*#(\d+)`)
- For each dependency found: `state.record_dependency(issue_number, depends_on_issue, repo)`
- The run will be enqueued normally but `try_promote` will skip it until all dependencies are satisfied

**Dependency satisfaction** (on `issues.closed`):
- Call `state.satisfy_dependency(closed_issue, repo)` — marks all dependencies on this issue as satisfied
- Then drain the queue for all agent types — newly unblocked issues may now be promotable

**Planner gating** (handled after planner run completes, not during dispatch):
- If planner emits `STATUS: DECOMPOSED`, dispatcher records decomposition state. Child issues created by the planner trigger `issues.opened` webhooks, which enter the queue normally.
- Decomposition is idempotent: if `state.is_decomposition_done(parent_issue, repo)` is true, skip creating duplicate children.

### 5. spawn.py

tmux management via `subprocess.run(["tmux", ...])` (not libtmux — more reliable for automation).

Functions:
- `ensure_session()` — create master tmux session if not exists
- `create_agent_window(run_id, agent_name, issue_id, cli_command, prompt_file_path, repo_path)`:
  1. Create named window: `tmux new-window -t SESSION -n "{agent}-{issue_id}-{run_id}"` (run_id from state.db ensures uniqueness across retries)
  2. Send `cd {repo_path}` via send-keys
  3. Send CLI command via send-keys with role injection:
     - @claude: `claude --dangerously-skip-permissions --append-system-prompt-file /path/to/agent-flow/roles/planner.md`
     - @implementer: `claude --dangerously-skip-permissions --model sonnet --append-system-prompt-file /path/to/agent-flow/roles/implementer.md`
     - @codex: `codex -c model_instructions_file=/path/to/agent-flow/roles/reviewer.md`
  4. Wait briefly for CLI to initialize
  5. Send single-line instruction: `"Read and execute the task in {prompt_file_path}"`
- `capture_pane(window_name, lines=500)` — `tmux capture-pane -p -S -{lines}`
- `kill_window(window_name)` — cleanup
- `list_windows()` — for monitor

**Two-layer prompt architecture**:
- **Layer 1 — Role (system prompt)**: Injected via CLI flag at spawn time. Contains agent identity, protocol rules, output contracts, handoff format, failure escalation rules. Lives in `roles/*.md`. Persistent for the entire session. Does NOT change between tasks. The target repo's own CLAUDE.md / AGENTS.md is loaded automatically by the CLI and coexists with our role file — no conflict.
- **Layer 2 — Task (user prompt)**: Written to a temp file per invocation. Contains issue context, comment thread, specific instructions. Delivered via send-keys ("Read and execute the task in {path}"). Changes every time an agent is spawned.

### 6. roles/planner.md (static system prompt)

Contains the persistent role identity and protocol rules for @claude:
- Role: "You are the PLANNER. Analyze issues and create detailed implementation plans."
- Dual-mode output contracts:
  - `STATUS: PLAN_COMPLETE` for direct implementation
  - `STATUS: DECOMPOSED` when parent issue is split into child issues
- Handoff rule:
  - Direct mode: end comment with `@implementer please implement the tasks above`
  - Decompose mode: no implementer handoff on parent; handoff happens on chosen child issue
- Comment format: must start with `<!-- agent:claude -->`
- Exact `gh` command template for posting
- Failure escalation rules (blocked, failed → @human)

### 7. prompts/planner.py (dynamic task builder)

Writes a per-invocation task file containing:
- Issue context: title, body, full comment thread
- Issue number and repo for `gh` commands

**Output contract** — planner comment must follow this structure:
```
<!-- agent:claude -->
## Plan for: {issue_title}
[plan content — task breakdown, files to modify, approach]
---
STATUS: PLAN_COMPLETE
@implementer please implement the tasks above.
```

**Decomposition contract** — when issue is too vague or too large:
```
<!-- agent:claude -->
## Decomposition for: {issue_title}
[why decomposition is needed]

Created child issues:
- #{child_1} — [title] (sequence 1/N)
- #{child_2} — [title] (sequence 2/N)
...
---
STATUS: DECOMPOSED
```

Decompose when any of the following are true:
- acceptance criteria are missing or ambiguous
- estimated work is larger than one focused coding session
- multiple independent workstreams require ordering
- cross-cutting architectural decisions must be sequenced

Child issue requirements:
- include `Parent: #{parent_issue_number}` in issue body
- include `Sequence: {k}/{N}` in issue body
- include `Depends-on: #X` in issue body where needed — the dispatcher parses this from the issue body on `issues.opened` and records it in `state.dependencies`. The dependent issue will stay queued until all `Depends-on` issues are closed.
- keep decomposition depth within `MAX_DECOMPOSITION_DEPTH`

**Failure rules** — included in every prompt:
- If the issue lacks enough context to plan: post a comment asking for clarification, end with `@human please provide more detail`
- If issue should be decomposed: create child issues, post `STATUS: DECOMPOSED` on parent, and do not hand off to implementer on parent
- If you encounter a permissions error or cannot access the repo: post `STATUS: BLOCKED` with the error, end with `@human`
- If `gh` CLI fails: retry once, then post `STATUS: FAILED` with the error, end with `@human`
- Never silently exit. Always post a GitHub comment with a STATUS line.

### 8. roles/implementer.md (static system prompt)

Contains the persistent role identity and protocol rules for @implementer:
- Role: "You are the IMPLEMENTER. Write code to fulfill the plan."
- Output contract template (STATUS: IMPLEMENTATION_COMPLETE format)
- Handoff: after opening PR, post a **separate issue comment** (not PR comment). This ensures the handoff triggers via `issue_comment` webhook only — no double-trigger from `pull_request.opened`.
- **All handoff @mentions must be posted as issue comments via `gh issue comment`, never as PR comments.** This is critical for the webhook routing to work.
- Comment tag: `<!-- agent:implementer -->`
- Branch naming, commit format, PR body rules
- Failure escalation rules (blocked, CI failing, tests failing → appropriate handler)

### 9. prompts/implementer.py (dynamic task builder)

Writes a per-invocation task file containing:
- Plan text from planner's comment
- Issue number, repo, and PR context for `gh` commands

**Output contract** — implementer issue comment must follow this structure:
```
<!-- agent:implementer -->
## Implementation for: {issue_title}
[summary of changes — files modified, approach taken]
PR: #{pr_number}
---
STATUS: IMPLEMENTATION_COMPLETE
@codex please review PR #{pr_number}.
```

**Failure rules:**
- If the plan is unclear or contradictory: post `STATUS: BLOCKED` with specific questions, end with `@claude please clarify`
- If tests fail after implementation: post `STATUS: TESTS_FAILING` with test output, end with `@codex please review PR #{pr_number}` (reviewer decides if it's a real issue)
- If `git push` fails (e.g. conflicts): post `STATUS: BLOCKED` with the error, end with `@human`
- If CI fails on the PR: post `STATUS: CI_FAILING` with relevant logs, end with `@codex please review`
- Never silently exit. Always post a GitHub comment with a STATUS line.

### 10. roles/reviewer.md (static system prompt)

Contains the persistent role identity and protocol rules for @codex:
- Role: "You are the REVIEWER. Review PRs for correctness, quality, and completeness."
- Output contract templates (APPROVED / CHANGES_REQUESTED / BLOCKED formats)
- Instructions: post review via `gh pr review`, then post handoff as **issue comment** via `gh issue comment`
- **All handoff @mentions must be posted as issue comments via `gh issue comment`, never as PR comments.** This is critical for the webhook routing to work.
- Comment tag: `<!-- agent:codex -->`
- Approval action: reviewer merges via `gh pr merge --squash --delete-branch`
- Failure escalation rules

### 11. prompts/reviewer.py (dynamic task builder)

Writes a per-invocation task file containing:
- PR diff and description
- Issue number, PR number, repo for `gh` commands

Note: output contract templates, approval action, and failure rules for the reviewer are defined in `roles/reviewer.md` (see section 10 above).

### 12. monitor.py

Async loop running alongside FastAPI.

Every `MONITOR_POLL_SECONDS`:
1. Query `state.get_active_runs()` — reconnects to existing windows after restart
2. For each active run: capture pane, hash content, compare to previous
3. If unchanged for `IDLE_TIMEOUT_SECONDS`: `state.update_run(id, "stuck")`, send Telegram alert
4. Scan for error patterns: `Error:`, `Traceback`, `FATAL`, `rate limit`, `authentication failed`
5. On error: `state.update_run(id, "failed")`, Telegram alert with pane excerpt

For Claude Code (alt-screen buffer): also poll GitHub for new comments tagged with `<!-- agent:X -->` as primary completion signal.

On startup: check `state.get_active_runs()` against actual tmux windows. Mark orphaned runs as failed via `state.fail_run()` (which auto-drains the queue for that agent type).

On run completion detected (agent posted to GitHub / pane exited): call `state.complete_run(run_id)` which auto-promotes the next queued job.

### 13. telegram.py

Using `python-telegram-bot`:
- `send_notification(message, issue_url)` — on @human mention
- `send_stuck_alert(agent, window, excerpt)` — from monitor
- `/create_issue <title>` command — human creates issue via Telegram
- `/status` command — show active agent windows + queue depth per agent type + blocked issues (from state.db)
- `/kill <window>` command — kill stuck agent, mark run as failed

### 14. CLAUDE.md / AGENTS.md for target repos (optional)

Target repos may have their own CLAUDE.md or AGENTS.md with project-specific conventions (code style, test frameworks, etc.). These are loaded automatically by Claude Code / Codex CLI and coexist with our injected role files. No conflict — our role files add pipeline protocol, the repo's files add project conventions.

If a target repo wants to add pipeline-aware instructions, it can include these in its own CLAUDE.md / AGENTS.md, but this is optional — the role files in `roles/` are the authoritative source for pipeline protocol.

### 15. main.py

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
- `roles/planner.md`, `roles/implementer.md`, `roles/reviewer.md` — static system prompts
- `prompts/planner.py`, `implementer.py`, `reviewer.py` — dynamic task builders
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
9. **Decomposition mode**: Create oversized/vague issue, verify planner creates child issues, posts `STATUS: DECOMPOSED`, and does not trigger implementer on parent
10. **Decomposition idempotency**: Redeliver same decomposition-triggering webhook, verify no duplicate child issues are created
11. **Concurrency limit**: Create two issues simultaneously, verify only one @claude runs at a time, second is queued
12. **Queue drain**: When active run completes, verify next queued run auto-promotes and spawns
13. **Dependency blocking**: Create issue with `Depends-on: #X` where #X is open, verify it stays queued. Close #X, verify dependent issue promotes.

## Risks

| Risk | Mitigation |
|---|---|
| Claude Code alt-screen makes pane capture unreliable | Poll GitHub for new comments as primary completion signal |
| Webhook storms from agent comments | Atomic dedup in state.db, strict last-line-only @mention parsing, `<!-- agent:X -->` tags |
| Double-trigger on PR open + handoff comment | `pull_request.opened` ignored; reviewer only triggered via @mention in issue comment |
| tmux window name collision on retry | Window names include run_id from state.db for uniqueness |
| Agents hang on permission prompts | Use `--dangerously-skip-permissions` |
| tmux session disappears | Monitor checks session existence, recreates if needed |
| Review cycle loops | Hard max of 3 cycles, then @human escalation (persisted in state.db) |
| Recursive decomposition loops | Enforce `MAX_DECOMPOSITION_DEPTH` and idempotent `decomposition_done` flag per parent issue |
| Concurrent agents on same repo | Max 1 active run per agent type; queue serializes work. Each issue gets its own branch. |
| Queue starvation from blocked deps | Monitor reports blocked issues in /status; dependencies only block specific issues, not the whole queue |
| Restart loses track of active agents | state.db persists runs; monitor reconciles against tmux on startup |
