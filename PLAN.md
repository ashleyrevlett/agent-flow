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
        - end with "@codex please review this plan"
        - webhook: issue_comment → dispatch @codex (plan review)
      Mode B (decompose):
        - create child issues with explicit sequence/dependencies and parent link
        - post STATUS: DECOMPOSED on parent (no further handoff on parent)
        - child issues trigger issues.opened → each enters pipeline independently
  → @codex reviews the plan:
      → approve: post STATUS: PLAN_APPROVED, end with "@implementer please implement"
      → request changes: "@claude please revise the plan" → re-plan cycle (max 3)
      → stuck: "@human please review"
  → @implementer creates branch, writes code, opens PR
  → @implementer posts issue comment ending with "@codex please review PR #N"
  → webhook: issue_comment → dispatch @codex (code review)
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
    worktree_path TEXT,            -- path to worktree (for cleanup on completion)
    pr_branch TEXT,                -- branch name (needed for reviewer worktree creation)
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Issue-level state machine — enforces valid stage transitions
CREATE TABLE issue_stages (
    issue_number INTEGER NOT NULL,
    repo TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'open',
    -- Valid stages: open → planning → plan_review → implementing → code_review → approved → closed
    -- Also: decomposed (terminal for parent issues)
    -- Re-plan cycles: plan_review → planning (via CHANGES_REQUESTED)
    -- Code review cycles: code_review → implementing (via CHANGES_REQUESTED)
    plan_review_count INTEGER DEFAULT 0,
    code_review_count INTEGER DEFAULT 0,
    escalated BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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

-- Circuit breaker per agent type (rate limit / auth failure protection)
CREATE TABLE breakers (
    agent TEXT PRIMARY KEY,            -- claude, implementer, codex
    tripped_at TIMESTAMP,
    resume_at TIMESTAMP,               -- when to auto-reset
    backoff_seconds INTEGER DEFAULT 300 -- current backoff (doubles on consecutive trips, max 3600)
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
- `complete_run(run_id)` — mark `completed`, set `completed_at`, call `spawn.cleanup_worktree()` if `worktree_path` is set, then call `try_promote(agent)` to auto-dequeue the next job
- `fail_run(run_id, status)` — mark `failed` or `stuck`, call `spawn.cleanup_worktree()` if `worktree_path` is set, then call `try_promote(agent)` to auto-dequeue
- `get_active_runs()` — all runs with status `active`, for monitor to reconnect after restart
- `get_queue_depth(agent=None)` — count of `queued` runs, optionally filtered by agent type (for /status command)
- `is_duplicate(delivery_id)` — atomic `INSERT ... ON CONFLICT DO NOTHING`, returns `True` if 0 rows affected (already seen). No separate check step — single statement eliminates race window under concurrent webhook handling.

**Issue stage transitions:**
- `get_stage(issue_number, repo)` — returns current stage, creates `open` row if missing
- `transition(issue_number, repo, expected_stage, new_stage)` — atomic `UPDATE ... WHERE stage = expected_stage`. Returns `True` if transition succeeded, `False` if current stage didn't match (invalid transition). This is the guard that prevents out-of-sequence agent invocations.
- Valid transitions:
  ```
  open → planning                (issues.opened → dispatch @claude)
  planning → plan_review         (planner posts PLAN_COMPLETE → @codex)
  planning → decomposed          (planner posts DECOMPOSED — terminal)
  plan_review → planning         (reviewer requests plan changes → @claude)
  plan_review → implementing     (reviewer approves plan → @implementer)
  implementing → code_review     (implementer posts IMPLEMENTATION_COMPLETE → @codex)
  code_review → implementing     (reviewer requests code changes → @implementer)
  code_review → approved         (reviewer approves + merges — terminal)
- `escalate(issue_number, repo)` — separate function, not `transition()`. Sets stage to `escalated` regardless of current stage. This avoids conflicting with the strict expected-stage contract of `transition()`.
  ```
- `get_review_count(issue_number, repo, review_type)` — returns plan_review_count or code_review_count
- `increment_review_count(issue_number, repo, review_type)` — bump the appropriate counter
- `prune_deliveries(max_age_hours=24)` — cleanup old dedup entries. Default 24h is conservative; GitHub can redeliver webhooks for up to several hours after initial failure. Configurable via `DEDUP_TTL_HOURS` env var.
- `trip_breaker(agent)` — set breaker with current backoff, double backoff (cap at 3600s). The run that triggered it stays `failed`.
- `is_breaker_tripped(agent)` — returns `True` if breaker is tripped and `resume_at` is in the future
- `reset_breaker(agent)` — clear breaker, reset backoff to 300s. Called by monitor when `resume_at` has passed.
- `try_promote(agent)` updated: also checks `is_breaker_tripped(agent)` — returns `None` if tripped.
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
3. **Stage validation**: determine the expected transition and attempt it atomically:
   - `issues.opened` + @claude: `transition(issue, "open", "planning")` — reject if not `open`
   - PLAN_COMPLETE + @codex: `transition(issue, "planning", "plan_review")` — reject if not `planning`
   - PLAN_APPROVED + @implementer: `transition(issue, "plan_review", "implementing")` — reject if not `plan_review`
   - PLAN CHANGES_REQUESTED + @claude: `transition(issue, "plan_review", "planning")` — reject if not `plan_review`
   - IMPLEMENTATION_COMPLETE + @codex: `transition(issue, "implementing", "code_review")` — reject if not `implementing`
   - CODE CHANGES_REQUESTED + @implementer: `transition(issue, "code_review", "implementing")` — reject if not `code_review`
   - APPROVED: `transition(issue, "code_review", "approved")` — reject if not `code_review`
   - If transition returns `False`: log warning, skip dispatch (stale/duplicate/out-of-sequence mention)
4. **Review cycle limits**: check `state.get_review_count(issue, review_type)` — if >= MAX_REVIEW_CYCLES, route to @human, transition to `escalated`
5. For review → re-plan/re-implement handoffs: `state.increment_review_count(issue, review_type)`
6. Fetch context via `gh` CLI (conditional by stage):
   - Always: issue thread via `gh api repos/{owner}/{repo}/issues/{number}/comments`, issue body from payload
   - Only for `code_review` / `implementing` stages: PR diff via `gh pr diff {number}`, PR metadata
7. Call appropriate prompt builder → writes prompt file to `/tmp/agent-flow/prompts/{agent}-{issue_id}-{timestamp}.md`
8. **Enqueue**: `run_id = state.enqueue_run(issue_number, repo, agent, prompt_file)`
9. **Try to promote**: `run = state.try_promote(agent)` — if another run of this agent type is already active, returns `None` and the job stays queued
10. If promoted: call `spawn.create_agent_window(run.id, agent_name, issue_id, ...)`, then `state.update_run_window(run.id, tmux_window)`
11. If not promoted: job waits in queue. Auto-spawned when the current active run completes.

**Queue drain function** `drain_queue(agent)`:
Called by `state.complete_run()` and `state.fail_run()` after marking a run done. Also called by monitor on startup to resume any orphaned queued runs.
1. `run = state.try_promote(agent)`
2. If `run`: spawn tmux window, update run with window name
3. If not: no queued work for this agent type, nothing to do

**Dependency parsing** (on `issues.opened`):
- Parse issue body for `Depends-on: #X` lines (regex: `Depends-on:\s*#(\d+)`)
- For each dependency found: `state.record_dependency(issue_number, depends_on_issue, repo)`
- The run will be enqueued normally but `try_promote` will skip it until all dependencies are satisfied

**Decomposition depth enforcement** (on `issues.opened`):
- Parse issue body for `Parent: #X` line
- If found: look up `state.decomposition_meta` for parent issue, get its `depth`
- Set child's depth to `parent_depth + 1`
- Record in `state.decomposition_meta(child_issue, depth=parent_depth+1)`
- Pass depth to planner prompt builder. If `depth >= MAX_DECOMPOSITION_DEPTH`, planner prompt includes: "You MUST NOT decompose this issue further. Use Mode A (direct plan) only."
- If planner posts STATUS: DECOMPOSED at max depth, dispatcher rejects it: posts an error comment and hands off to `@human`

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
  2. Send `cd {repo_path}` via send-keys (this is the worktree path, not the main repo)
  3. Send CLI command via send-keys with role injection:
     - @claude (planner): `claude -w plan-{issue_id}-{run_id} --dangerously-skip-permissions --append-system-prompt-file /path/to/agent-flow/roles/planner.md`
     - @implementer: `claude -w feature-{issue_id}-{run_id} --model sonnet --dangerously-skip-permissions --append-system-prompt-file /path/to/agent-flow/roles/implementer.md`
     - @codex (reviewer): `codex -c model_instructions_file=/path/to/agent-flow/roles/reviewer.md` (operates in a manually-created worktree, see below)
  4. Wait briefly for CLI to initialize
  5. Send single-line instruction: `"Read and execute the task in {prompt_file_path}"`
- `create_reviewer_worktree(issue_id, run_id, pr_branch)` — for @codex **code review** only, since Codex has no native `-w` flag:
  1. `git worktree add /tmp/agent-flow/worktrees/review-{issue_id}-{run_id} {pr_branch}`
  2. Returns the worktree path, which is used as the `repo_path` for `create_agent_window`
  3. **Not called for plan reviews** — plan review uses the main repo path since no PR branch exists
- `cleanup_worktree(worktree_path)` — called after a run completes or fails:
  1. `git worktree remove {worktree_path} --force`
  2. Only for manually-created worktrees (reviewer). Claude Code's `-w` handles its own cleanup.
- `capture_pane(window_name, lines=500)` — `tmux capture-pane -p -S -{lines}`
- `kill_window(window_name)` — cleanup
- `list_windows()` — for monitor

**Two-layer prompt architecture**:
- **Layer 1 — Role (system prompt)**: Injected via CLI flag at spawn time. Contains agent identity, protocol rules, output contracts, handoff format, failure escalation rules. Lives in `roles/*.md`. Persistent for the entire session. Does NOT change between tasks. The target repo's own CLAUDE.md / AGENTS.md is loaded automatically by the CLI and coexists with our role file — no conflict.
- **Layer 2 — Task (user prompt)**: Written to a temp file per invocation. Contains issue context, comment thread, specific instructions. Delivered via send-keys ("Read and execute the task in {path}"). Changes every time an agent is spawned.

**Worktree isolation**:
- **@claude (planner)**: Uses Claude Code's native `-w plan-{issue_id}-{run_id}` flag. Worktree created at `<repo>/.claude/worktrees/plan-{issue_id}-{run_id}/`. Auto-cleaned up by Claude Code on exit.
- **@implementer**: Uses Claude Code's native `-w feature-{issue_id}-{run_id}` flag. Same auto-cleanup behavior. Branch is `worktree-feature-{issue_id}-{run_id}`.
- **@codex (reviewer, code review)**: Worktree created manually by spawn.py via `git worktree add` on the PR branch. Codex operates in this directory so it can read the actual files on the correct branch. Cleaned up by `spawn.cleanup_worktree()` after run completes.
- **@codex (reviewer, plan review)**: No code changes exist yet, but to maintain the "main checkout never modified" invariant, Codex still operates from a read-only worktree. spawn.py creates a disposable worktree on the default branch: `git worktree add /tmp/agent-flow/worktrees/planreview-{issue_id}-{run_id} HEAD --detach`. This is read-only in practice (plan review only reads issue thread context, not files), and is cleaned up normally.
- The main repo checkout is never modified by agents. All work happens in worktrees.

### 6. roles/planner.md (static system prompt)

Contains the persistent role identity and protocol rules for @claude:
- Role: "You are the PLANNER. Analyze issues and create detailed implementation plans."
- Dual-mode output contracts:
  - `STATUS: PLAN_COMPLETE` for direct implementation
  - `STATUS: DECOMPOSED` when parent issue is split into child issues
- Handoff rule:
  - Direct mode: end comment with `@codex please review this plan`
  - Decompose mode: no handoff on parent; child issues enter pipeline independently via webhook
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
@codex please review this plan.
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
- If `git push` fails due to merge conflicts: post `STATUS: CONFLICTS` with the error, end with `@codex please review` (reviewer will request changes, implementer rebases in next cycle). If `git push` fails for other reasons (permissions, network): post `STATUS: BLOCKED`, end with `@human`.
- If CI fails on the PR: post `STATUS: CI_FAILING` with relevant logs, end with `@codex please review`
- Never silently exit. Always post a GitHub comment with a STATUS line.

### 10. roles/reviewer.md (static system prompt)

Contains the persistent role identity and protocol rules for @codex:
- Role: "You are the REVIEWER. You perform both plan reviews and code reviews."
- **Two review modes** (determined by context in the task prompt):
  - **Plan review**: evaluate the planner's technical approach, completeness, and feasibility. No PR exists yet.
    - Approve plan → `STATUS: PLAN_APPROVED`, end with `@implementer please implement`
    - Request changes → `STATUS: PLAN_CHANGES_REQUESTED`, end with `@claude please revise the plan`
  - **Code review**: evaluate the PR for correctness, quality, and completeness.
    - Approve code → run `gh pr checks --required --watch`, then `gh pr merge --squash --delete-branch`. Post `STATUS: APPROVED` (no @mention — pipeline done)
    - Request changes → `STATUS: CHANGES_REQUESTED`, end with `@implementer please address the feedback`
    - If CI checks fail → `STATUS: CI_FAILING`, end with `@implementer please fix CI failures`
- **All handoff @mentions must be posted as issue comments via `gh issue comment`, never as PR comments.** This is critical for the webhook routing to work.
- Comment tag: `<!-- agent:codex -->`
- Failure escalation rules

### 11. prompts/reviewer.py (dynamic task builder)

Branches by review mode:

**Plan review mode** (stage is `plan_review`):
- Issue context: title, body, full comment thread (includes planner's plan comment)
- Issue number and repo for `gh` commands
- Mode flag: `review_mode: plan`

**Code review mode** (stage is `code_review`):
- PR diff and description
- Issue number, PR number, repo for `gh` commands
- Mode flag: `review_mode: code`

Note: output contract templates, approval action, and failure rules for the reviewer are defined in `roles/reviewer.md` (see section 10 above).

### 12. monitor.py

Async loop running alongside FastAPI.

Every `MONITOR_POLL_SECONDS`:
1. Query `state.get_active_runs()` — reconnects to existing windows after restart
2. For each active run: capture pane, hash content, compare to previous
3. If unchanged for `IDLE_TIMEOUT_SECONDS`: `state.update_run(id, "stuck")`, send Telegram alert
4. Scan for error patterns: `Error:`, `Traceback`, `FATAL`, `rate limit`, `authentication failed`
5. On error:
   - **Rate limit / auth errors**: trigger **circuit breaker** for the agent type. Set `state.trip_breaker(agent)` with a backoff timestamp (5 min initially, doubling up to 1 hour). While tripped, `try_promote(agent)` returns `None` even if a slot is free. Monitor checks breaker expiry each poll and resets it. Telegram alert: "Circuit breaker tripped for @{agent} — rate limited. Resuming at {time}."
   - **Other errors**: `state.fail_run(id, "failed")`, Telegram alert with pane excerpt. Queue drains normally.

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
14. **Worktree isolation**: Verify each agent operates in its own worktree, main repo checkout is untouched.
15. **Worktree cleanup**: Verify reviewer worktrees are removed after run completes/fails.
16. **Plan review step**: Verify planner hands off to @codex for plan review before implementation starts.
17. **Stage guard**: Send an @implementer mention on an issue in `planning` stage, verify it's rejected.
18. **Circuit breaker**: Trigger rate limit error, verify queue pauses for that agent type, resumes after backoff.
19. **CI gate**: Verify reviewer waits for required checks before merging. Verify CI failure routes to @implementer.

## Risks

| Risk | Mitigation |
|---|---|
| Claude Code alt-screen makes pane capture unreliable | Poll GitHub for new comments as primary completion signal |
| Webhook storms from agent comments | Atomic dedup in state.db, strict last-line-only @mention parsing, `<!-- agent:X -->` tags |
| Double-trigger on PR open + handoff comment | `pull_request.opened` ignored; reviewer only triggered via @mention in issue comment |
| tmux window name collision on retry | Window names include run_id from state.db for uniqueness |
| Agents hang on permission prompts | Use `--dangerously-skip-permissions` |
| tmux session disappears | Monitor checks session existence, recreates if needed |
| Review cycle loops | Hard max of 3 cycles per review type (plan + code), then @human escalation (persisted in state.db) |
| Rate limit cascade-fails queue | Circuit breaker per agent type: 5min→10min→20min→1hr backoff. Queue pauses, not fails. |
| Out-of-sequence agent invocation | Issue-level state machine rejects invalid transitions atomically. Stale/duplicate mentions are no-ops. |
| Reviewer merges with failing CI | Reviewer must run `gh pr checks --required --watch` before merge. CI failure → @implementer, not merge. |
| Recursive decomposition loops | Enforce `MAX_DECOMPOSITION_DEPTH` and idempotent `decomposition_done` flag per parent issue |
| Concurrent agents on same repo | All agents work in isolated worktrees; main checkout never modified. Max 1 active run per agent type serializes work further. |
| Merge conflicts between PRs | Queue serializes implementer runs, reducing conflicts. When they occur: at push time, implementer posts STATUS: CONFLICTS → @codex; at merge time, reviewer detects via `gh pr merge` failure → posts CHANGES_REQUESTED → @implementer rebases. Both count as review cycles. |
| Codex worktree bugs | We create reviewer worktrees manually (`git worktree add`) and point Codex at them via `cd`, avoiding Codex's known worktree issues with session-based creation. |
| Queue starvation from blocked deps | Monitor reports blocked issues in /status; dependencies only block specific issues, not the whole queue |
| Restart loses track of active agents | state.db persists runs; monitor reconciles against tmux on startup |
