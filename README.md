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

## Docs

- Developer spec and architecture: `SPEC.md`
- Example configuration: `.env.example`

## License

MIT
