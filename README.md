# Agent Flow

Autonomous development pipeline driven by GitHub webhooks. A lightweight Python dispatcher routes work to AI coding agents via @mentions in GitHub comments. Agents run as interactive CLI sessions in tmux and hand off to each other automatically.

No orchestrator LLM. No API billing. Pure pattern matching + subscription-plan CLIs.

## How It Works

1. A GitHub issue is created
2. The webhook dispatcher spawns **@claude** (planner) in a tmux window
3. @claude posts a plan comment ending with `STATUS: PLAN_COMPLETE` and `@codex please review`
4. **@codex** reviews the plan — approves (`STATUS: PLAN_APPROVED @implementer`) or requests changes
5. @implementer writes code, opens a PR, and posts `STATUS: IMPLEMENTATION_COMPLETE @codex`
6. **@codex** reviews the code — approves and merges, or requests changes (up to 3 cycles per review phase)
7. PR merges → issue auto-closes

Large issues can be decomposed: @claude posts `STATUS: DECOMPOSED` and creates child issues that run the pipeline independently.

Humans can intervene at any point via `@human` mentions (triggers Telegram notification).

## Agents

| Handle | Role | CLI | Model |
|---|---|---|---|
| `@claude` | Planner | Claude Code | Opus |
| `@implementer` | Implementer | Claude Code | Sonnet |
| `@codex` | Reviewer | Codex CLI | Codex |

All use subscription-plan billing, not API keys.

## Architecture

```
GitHub Webhook → FastAPI (webhook.py)
                    │
                    ├── dispatch.py   — comment routing, stage machine calls
                    ├── state.py      — SQLite stage/run/circuit-breaker state
                    ├── spawn.py      — tmux window + git worktree management
                    ├── monitor.py    — async health-check + completion polling
                    └── notifications.py — Telegram alerts + /status bot
```

GitHub comments are the message bus. Agents self-direct handoffs via @mentions.

## Setup

### Prerequisites

- Python 3.11+
- tmux
- [gh CLI](https://cli.github.com/) (authenticated)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (logged in with subscription)
- [Codex CLI](https://github.com/openai/codex) (logged in with subscription)
- ngrok or Cloudflare tunnel (for local webhook delivery)

### Install

```sh
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens and config
```

### Run

```sh
python main.py
```

Starts the webhook server on port 8000, the tmux monitor loop, and the Telegram bot.

### Configure GitHub Webhook

Point your repo's webhook at your server:

- **URL**: `https://your-tunnel.ngrok.io/webhook`
- **Content type**: `application/json`
- **Secret**: match your `GITHUB_WEBHOOK_SECRET`
- **Events**: Issues, Issue comments, Pull requests, Pull request reviews, Workflow runs

## License

MIT
