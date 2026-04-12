# Implementation Review Findings

## Findings

1. **High: `API_TOKEN` is not mapped to provider CLI auth env vars**
- If users only set `API_TOKEN` (as shown in `.env.example`), `gh`/`glab` subprocess calls can run unauthenticated unless CLI login state already exists.
- Evidence:
- [config.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/config.py:16)
- [providers/github.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/github.py:32)
- [providers/gitlab.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/gitlab.py:37)
- Recommended fix:
- In provider `_cli_env()`, inject auth env vars from `API_TOKEN` (GitHub: `GH_TOKEN`/`GITHUB_TOKEN`; GitLab: appropriate `glab` token env).

2. **High: webhook auth can fail open when `WEBHOOK_SECRET` is empty**
- GitLab token compare can pass when both header token and secret are empty.
- GitHub signature check still runs, but an empty secret is insecure/defaultable.
- Evidence:
- [providers/github.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/github.py:42)
- [providers/gitlab.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/gitlab.py:114)
- Recommended fix:
- Explicitly reject webhook verification when secret is unset/blank.

3. **Medium: GitLab paginated JSON handling is likely fragile**
- `glab ... --paginate` output is parsed with a single `json.loads`, which can fail on concatenated page output.
- Evidence:
- [providers/gitlab.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/gitlab.py:215)
- [providers/gitlab.py](/Users/ashleyrevlett1/Documents/apps/agent-flow/providers/gitlab.py:309)
- Recommended fix:
- Use robust multi-page parsing (similar to GitHub provider comment parsing strategy).

4. **Medium: provider-specific test coverage is missing**
- Plan called for dedicated provider tests, but repo only has state/dispatch tests.
- Evidence:
- [tests](/Users/ashleyrevlett1/Documents/apps/agent-flow/tests)
- Recommended fix:
- Add:
- `tests/test_provider_selection.py`
- `tests/test_provider_github.py`
- `tests/test_provider_gitlab.py`

## Notes

- I updated docs structure per request:
- User-facing high-level guide: [README.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/README.md)
- Developer-focused project context and technical decisions: [SPEC.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/SPEC.md)

