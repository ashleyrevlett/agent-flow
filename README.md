# Agent Flow

Autonomous development pipeline driven by GitHub webhooks. A lightweight Python dispatcher routes work to AI coding agents via @mentions in GitHub comments. Agents run as interactive CLI sessions in tmux and hand off to each other automatically.

No orchestrator LLM. No API billing. Pure pattern matching + subscription-plan CLIs.

## How It Works

1. A GitHub issue is created
2. The webhook dispatcher spawns **@claude** (planner) in a tmux window
3. @claude posts a plan as an issue comment, ending with `@implementer please implement`
4. The dispatcher catches the @mention and spawns **@implementer**
5. @implementer writes code, opens a PR, ending with `@codex please review`
6. **@codex** reviews — approves or requests changes (up to 3 cycles)
7. PR merges → CI runs → issue auto-closes
8. Hindsight extracts lessons from the closed issue into memory

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
GitHub Webhook → FastAPI → dispatch.py → tmux agent sessions
                                       → monitor.py (health checks)
                                       → hindsight.py (lesson extraction)
                                       → telegram.py (human escalation)
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
