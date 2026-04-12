# Review Findings: `PLAN-gitlab-support.md`

## Findings

1. **High: GitLab trust model is likely based on a field that may not exist on note webhooks**
- The plan assumes `is_trusted` can be computed from GitLab `access_level >= 30` during webhook parsing.
- References: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L83), [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L121)
- Risk: if `access_level` is absent in issue-note payloads, trusted-comment gating can fail open or fail closed.
- Recommendation: define explicit fallback now:
- Option A: strict allowlist (`BOT_USERNAME` + maintainers list).
- Option B: membership lookup via API with caching.

2. **High: `id` vs `iid` semantics are underspecified and can break MR operations**
- The plan uses generic `mr_id` in interface and command templates, but GitLab commonly distinguishes global `id` from project-local `iid`.
- References: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L58), [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L67)
- Risk: wrong identifier choice causes `glab` MR commands and API calls to fail.
- Recommendation: define one canonical internal identifier now (recommended: MR `iid`) and rename interface fields accordingly (`mr_iid`).

3. **High: plan hardcodes `gitlab.com`, excluding self-managed GitLab**
- `issue_url` is defined as `https://gitlab.com/{repo}/-/issues/{n}`.
- Reference: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L124)
- Risk: broken links and API routing for enterprise/self-hosted GitLab users.
- Recommendation: add `GITLAB_BASE_URL` (or generic `GIT_BASE_URL`) and use it for URL generation and provider API routing.

4. **Medium: GitLab dedup strategy is weakly specified**
- Plan proposes `sha256(body)[:16]` for delivery ID on GitLab.
- Reference: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L86)
- Risk: avoidable collision risk over long runtime; same payload body across distinct events can be conflated.
- Recommendation: hash a normalized key tuple (event type + repo/project + object id/iid + action + timestamp), and prefer full digest.

5. **Medium: implementation order can create temporary runtime breakpoints**
- Consumer rewires are scheduled before all provider implementations are in place.
- References: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L198), [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L203)
- Risk: startup/import failures in intermediate commits if provider resolution occurs early.
- Recommendation: land `provider.py` + both provider implementations first, then rewire `webhook.py`, `dispatch.py`, `monitor.py`, prompts, notifications.

6. **Medium: test plan misses compatibility/migration coverage**
- Tests currently focus on dispatch updates and provider payload parsing.
- Reference: [PLAN-gitlab-support.md](/Users/ashleyrevlett1/Documents/apps/agent-flow/PLAN-gitlab-support.md#L174)
- Risk: regressions in env-var migration and provider selection can slip through.
- Recommendation: add tests for:
- `GIT_PROVIDER` selection behavior.
- Backward-compatible env aliases (`GITHUB_*` -> generic vars).
- Provider URL generation correctness per provider/base URL.

## Overall

The abstraction itself is appropriately scoped and not over-engineered. The main gaps are trust/auth correctness for GitLab events, identifier normalization (`id` vs `iid`), and self-managed GitLab support.
