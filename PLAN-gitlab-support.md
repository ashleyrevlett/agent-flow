# Plan: Add GitLab support via provider abstraction

## Context

The pipeline is currently GitHub-only. Every git-platform interaction — webhook verification, payload parsing, CLI subprocess calls, URL construction, and prompt CLI templates — assumes GitHub. To support GitLab (and potentially other providers later), we need a clean abstraction that centralizes all platform-specific logic in one place, selected by a single env var.

**Design principle:** the state machine, stage transitions, mention/status parsing, tmux spawning, and monitor polling logic are all provider-agnostic. Only the "how do I talk to the git platform" part changes.

---

## Architecture

One `GitProvider` Protocol with two implementations. A `WebhookEvent` dataclass normalizes raw payloads so dispatch.py never sees provider-specific structure.

```
config.py  →  GIT_PROVIDER="github"|"gitlab"
                     ↓
provider.py  →  WebhookEvent dataclass + GitProvider Protocol + factory
                     ↓
providers/github.py  →  GitHubProvider (extracts existing code)
providers/gitlab.py  →  GitLabProvider (new logic)
```

Consumers (`webhook.py`, `dispatch.py`, `monitor.py`, `notifications.py`, `prompts/*.py`) call provider methods instead of shelling out to `gh` directly.

---

## New files

### `provider.py` — types + factory (~50 lines)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol

@dataclass(frozen=True, slots=True)
class WebhookEvent:
    kind: str               # "issue_opened" | "comment_created" | "issue_closed" | "workflow_completed"
    delivery_id: str        # GitHub header or GitLab composite hash (see dedup section)
    repo: str               # "owner/repo" or "group/project"
    issue_number: int       # GitHub issue number or GitLab issue iid (project-scoped)
    issue_title: str
    issue_body: str
    comment_body: str | None
    commenter: str | None
    is_trusted: bool        # OWNER/MEMBER/COLLABORATOR or verified via membership API
    is_bot: bool            # commenter == BOT_USERNAME
    is_agent_comment: bool  # body contains <!-- agent:... -->

class GitProvider(Protocol):
    # Webhook
    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> bool: ...
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent | None: ...

    # API (all return normalized shapes)
    def fetch_comments(self, repo: str, issue_number: int) -> list[dict]: ...
    def fetch_mr_context(self, repo: str, issue_number: int) -> tuple[int | None, str | None, str | None]: ...
    def fetch_mr_branch(self, repo: str, mr_iid: int) -> str | None: ...
    def check_completion(self, repo: str, issue_number: int, agent: str, since_iso: str) -> tuple[bool, str | None]: ...
    def create_issue(self, repo: str, title: str, body: str) -> str: ...
    def issue_url(self, repo: str, issue_number: int) -> str: ...

    # CLI templates for generated agent prompts
    def comment_cli(self, issue_number: int, repo: str) -> str: ...
    def mr_create_cli(self, repo: str) -> str: ...
    def mr_merge_cli(self, mr_iid: int, repo: str) -> str: ...
    def mr_checks_cli(self, mr_iid: int, repo: str) -> str: ...
    def issue_link_syntax(self, issue_number: int) -> str: ...

def get_provider() -> GitProvider:
    from config import GIT_PROVIDER
    if GIT_PROVIDER == "github":
        from providers.github import GitHubProvider
        return GitHubProvider()
    elif GIT_PROVIDER == "gitlab":
        from providers.gitlab import GitLabProvider
        return GitLabProvider()
    raise ValueError(f"Unknown GIT_PROVIDER: {GIT_PROVIDER!r}")
```

### `providers/__init__.py` — empty

### `providers/github.py` — GitHubProvider (~150 lines)

Pure extraction of existing code. Every function body already exists somewhere:

| Method | Extracted from | Notes |
|--------|---------------|-------|
| `verify_webhook` | `webhook.py:_verify_signature` | HMAC-SHA256 via X-Hub-Signature-256 |
| `parse_webhook` | `dispatch.py:_route` lines 58-184 | Payload destructuring + trust check |
| `fetch_comments` | `dispatch.py:_fetch_comments` | `gh api repos/{repo}/issues/{n}/comments --paginate` |
| `fetch_mr_context` | `dispatch.py:_fetch_pr_context` | `gh pr list --search "Closes #N"` + `gh pr diff` |
| `fetch_mr_branch` | `dispatch.py:_fetch_pr_branch` | `gh pr view --json headRefName` |
| `check_completion` | `monitor.py:_check_github_completion` | `gh api` with JQ agent/timestamp filter |
| `create_issue` | `notifications.py:_cmd_create_issue` | `gh issue create` |
| `issue_url` | inline in dispatch/monitor | `https://github.com/{repo}/issues/{n}` |
| `comment_cli` | `prompts/planner.py:62` | `gh issue comment {n} --repo {repo} --body "..."` |
| `mr_create_cli` | `prompts/implementer.py:84` | `gh pr create --repo {repo} ...` |
| `mr_merge_cli` | `prompts/reviewer.py:152` | `gh pr merge {n} --repo {repo} --squash --delete-branch` |
| `mr_checks_cli` | `prompts/reviewer.py:151` | `gh pr checks {n} --repo {repo} --required --watch` |
| `issue_link_syntax` | `prompts/implementer.py:84` | `Closes #{n}` |

GitHub uses `number` for both issues and PRs, which maps 1:1 to the protocol's `issue_number` and `mr_iid`. `issue_url` uses `GIT_BASE_URL` (defaults to `https://github.com`).

### `providers/gitlab.py` — GitLabProvider (~180 lines)

New implementation using `glab` CLI. Key differences from GitHub:

| Aspect | GitHub | GitLab |
|--------|--------|--------|
| Webhook verification | HMAC-SHA256 (`X-Hub-Signature-256`) | Plain token compare (`X-Gitlab-Token`) |
| Event type header | `X-GitHub-Event` | `X-Gitlab-Event` |
| Comment event | `issue_comment` + `action: created` | `Note Hook` + `noteable_type: Issue` |
| Payload: comment body | `payload["comment"]["body"]` | `payload["object_attributes"]["note"]` |
| Payload: commenter | `payload["comment"]["user"]["login"]` | `payload["user"]["username"]` |
| Payload: trust | `author_association` string | Membership API lookup (see below) |
| Identifiers | `number` (repo-scoped) | `iid` (project-scoped) for issues and MRs |
| Fetch comments | `gh api repos/.../comments` | `glab api projects/:id/issues/:iid/notes` |
| PR to MR branch | `headRefName` | `source_branch` |
| Issue URL | `{base_url}/{repo}/issues/{n}` | `{base_url}/{repo}/-/issues/{n}` |
| CLI: comment | `gh issue comment` | `glab issue note` |
| CLI: MR create | `gh pr create` | `glab mr create` |
| CLI: merge | `gh pr merge` | `glab mr merge` |
| Issue-to-MR link | `gh pr list --search "Closes #N"` | `GET /projects/:id/merge_requests?search=Closes+%23N` + `closing_references` API |

---

## Key design decisions (addresses review findings)

### Finding 1: GitLab trust model — membership API with cache

GitLab webhook payloads do not reliably include `access_level` on note events. Relying on a potentially-absent field risks failing open (trusting everyone) or failing closed (blocking legitimate handoffs).

**Solution: BOT_USERNAME allowlist + direct membership lookup by user_id with TTL cache.**

GitLab webhook payloads always include `user.id` (numeric). The Members API supports direct lookup by user_id: `GET /projects/:id/members/all/:user_id`. This avoids scanning/paginating the full member list and eliminates username-in-jq escaping concerns.

```python
# Inside GitLabProvider
_trust_cache: dict[tuple[str, int], tuple[bool, float]] = {}  # (repo, user_id) -> (trusted, expires_at)
TRUST_CACHE_TTL = 300  # 5 minutes

def _is_trusted(self, repo: str, username: str, user_id: int) -> bool:
    # Bot is always trusted
    if username == self._bot_username:
        return True

    # Check cache (keyed by user_id — stable, unlike username)
    cache_key = (repo, user_id)
    cached = self._trust_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    # Direct lookup: GET /projects/:id/members/all/:user_id
    # Returns 200 with access_level if member, 404 if not a member
    encoded_repo = urllib.parse.quote_plus(repo)
    try:
        result = subprocess.run(
            ["glab", "api", f"projects/{encoded_repo}/members/all/{user_id}"],
            capture_output=True, text=True, check=True, env=self._cli_env(),
        )
        member = json.loads(result.stdout)
        trusted = member.get("access_level", 0) >= 30  # 30 = Developer
    except subprocess.CalledProcessError:
        # 404 (not a member) or network error — fail closed
        trusted = False
    except (json.JSONDecodeError, TypeError):
        trusted = False

    self._trust_cache[cache_key] = (trusted, time.time() + self.TRUST_CACHE_TTL)
    return trusted
```

`parse_webhook` extracts `user_id = payload["user"]["id"]` from the webhook and passes it to `_is_trusted`. The `WebhookEvent` does not carry `user_id` — it's consumed entirely within the provider during parsing.

This approach:
- Single O(1) API call per user, no pagination or filtering
- No username interpolation in jq (no escaping risk)
- Keyed by `user_id` (stable) not username (can be renamed)
- 404 = not a member = untrusted (fail closed)
- Caches results for 5 minutes to avoid API spam on rapid comment bursts
- Bot username is always trusted without an API call

### Finding 2: `id` vs `iid` — canonical identifier is `iid`

GitLab has two identifiers for issues and MRs:
- `id`: globally unique across the instance
- `iid`: project-scoped, sequential (what users see in URLs and CLI commands)

All `glab` CLI commands and user-facing URLs use `iid`. The internal protocol uses `iid` consistently:

- `WebhookEvent.issue_number` stores the `iid` (GitLab webhook payloads include both; we extract `iid`)
- `fetch_mr_branch(mr_iid)` — parameter renamed from `mr_id` to `mr_iid`
- `mr_merge_cli(mr_iid)` / `mr_checks_cli(mr_iid)` — same
- `fetch_mr_context` returns `(mr_iid, diff, description)` — the `iid`, not the global `id`

For GitHub, `number` is already project-scoped, so it maps directly to `iid` semantics. No behavioral change for the GitHub provider.

### Finding 3: Self-managed instances — configurable host for URLs AND CLI/API

Issue URLs and CLI/API host must both work for self-hosted instances. `GIT_BASE_URL` alone only fixes links — `gh` and `glab` commands will still target the wrong host.

**Config:**
```python
# config.py
GIT_BASE_URL: str = os.getenv("GIT_BASE_URL", "")  # e.g. "https://gitlab.company.com"
# If unset, providers use their default: github.com or gitlab.com
```

**Provider usage — URLs:**
```python
# GitHubProvider
def issue_url(self, repo, issue_number):
    base = self._base_url or "https://github.com"
    return f"{base}/{repo}/issues/{issue_number}"

# GitLabProvider
def issue_url(self, repo, issue_number):
    base = self._base_url or "https://gitlab.com"
    return f"{base}/{repo}/-/issues/{issue_number}"
```

**Provider usage — CLI host targeting:**

Both `gh` and `glab` support environment variables for non-default hosts:
- **GitHub Enterprise:** `GH_HOST` env var (e.g., `GH_HOST=github.company.com`). All `gh api` and `gh pr` commands respect it.
- **GitLab self-managed:** `GITLAB_HOST` env var (e.g., `GITLAB_HOST=https://gitlab.company.com`). All `glab api` and `glab mr` commands respect it.

Providers set the appropriate env var in their `subprocess.run` calls when `GIT_BASE_URL` is configured:

```python
# GitHubProvider
def _cli_env(self) -> dict[str, str] | None:
    if not self._base_url:
        return None  # use system default
    host = self._base_url.replace("https://", "").replace("http://", "")
    return {**os.environ, "GH_HOST": host}

# GitLabProvider
def _cli_env(self) -> dict[str, str] | None:
    if not self._base_url:
        return None
    return {**os.environ, "GITLAB_HOST": self._base_url}
```

Every `subprocess.run(["gh", ...])` / `subprocess.run(["glab", ...])` call passes `env=self._cli_env()`. This ensures API calls, PR/MR operations, and issue creation all target the correct host.

For CLI template strings embedded in agent prompts (which agents execute themselves), the provider sets the env var in the shell command:

```python
# GitLabProvider
def comment_cli(self, issue_number, repo):
    prefix = f"GITLAB_HOST={self._base_url} " if self._base_url else ""
    return f'{prefix}glab issue note {issue_number} --repo {repo} -m "..."'
```

This covers gitlab.com, self-managed GitLab, and GitHub Enterprise.

**Implementation note — glab host env var format:**
The exact format `glab` expects for `GITLAB_HOST` must be verified before implementation. `gh` expects hostname-only for `GH_HOST` (e.g., `github.example.com`), but `glab` may expect a full URL (`https://gitlab.example.com`) or hostname-only. Add a test case during implementation:
```bash
# Verify which format glab accepts:
GITLAB_HOST=gitlab.example.com glab api version         # hostname-only
GITLAB_HOST=https://gitlab.example.com glab api version  # full URL
```
The provider's `_cli_env()` method must produce whichever format `glab` requires. If `glab` expects hostname-only (like `gh`), strip the scheme from `GIT_BASE_URL`. Document the verified format in a code comment next to `_cli_env()`.

### GitLab issue-to-MR resolution strategy

GitHub's `fetch_mr_context` uses `gh pr list --search "Closes #N"` to find the PR linked to an issue. GitLab needs an equivalent strategy.

**Primary method: GitLab's closing references API.**

GitLab tracks which MRs will close an issue when merged (via `Closes #N` in MR descriptions, same syntax). This is queryable:

```
GET /projects/:id/issues/:iid/closed_by
```

This returns an array of MRs that reference the issue with closing keywords. It is the most reliable method because it matches GitLab's own merge-closes-issue behavior.

**Implementation:**

```python
def fetch_mr_context(self, repo: str, issue_number: int) -> tuple[int | None, str | None, str | None]:
    encoded = urllib.parse.quote_plus(repo)

    # Step 1: Find MR(s) that will close this issue
    try:
        result = subprocess.run(
            ["glab", "api", f"projects/{encoded}/issues/{issue_number}/closed_by"],
            capture_output=True, text=True, check=True, env=self._cli_env(),
        )
        mrs = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None, None, None

    if not mrs:
        return None, None, None

    # Take the most recently updated open MR. The closed_by API does not
    # guarantee ordering, so sort explicitly. Parse timestamps to datetimes
    # rather than relying on lexicographic order (which breaks on varying
    # timezone offsets or fractional-second precision).
    from datetime import datetime, timezone

    def _parse_ts(m: dict) -> datetime:
        ts = m.get("updated_at", "")
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    open_mrs = sorted(
        [m for m in mrs if m.get("state") == "opened"],
        key=_parse_ts, reverse=True,
    )
    mr = open_mrs[0] if open_mrs else sorted(mrs, key=_parse_ts, reverse=True)[0]
    mr_iid = mr["iid"]
    description = mr.get("description", "")

    # Step 2: Fetch diff
    try:
        diff_result = subprocess.run(
            ["glab", "mr", "diff", str(mr_iid), "--repo", repo],
            capture_output=True, text=True, check=True, env=self._cli_env(),
        )
        diff = diff_result.stdout
    except subprocess.CalledProcessError:
        diff = None

    return mr_iid, diff, description
```

**Fallback behavior:**
- If `closed_by` returns empty (no MR references the issue): return `(None, None, None)`. The caller (`_dispatch_agent`) already handles this — code-review dispatch fails fast when `mr_iid` is None.
- If multiple MRs reference the issue: prefer the most recently updated open MR (explicit `updated_at` sort — the `closed_by` API does not guarantee ordering). This matches the expected workflow where one MR is active per issue.
- If the diff fetch fails: return `(mr_iid, None, description)`. The reviewer can still work with the MR number and description.

### Finding 4: GitLab dedup — per-event canonical fields

Plain `sha256(body)` risks collating distinct events. The dedup key must use fields that are:
1. Always present for the event type
2. Unique per discrete event occurrence
3. Stable across webhook retries (same event = same key)

**Per-event canonical fields:**

| Event type | Canonical key fields | Notes |
|---|---|---|
| `Note Hook` (issue note) | `note` + `project.id` + `object_attributes.id` | Note `id` is globally unique and immutable |
| `Issue Hook` (opened) | `issue` + `project.id` + `object_attributes.iid` + `object_attributes.action` | `iid` + action uniquely identifies the state change |
| `Issue Hook` (closed) | `issue` + `project.id` + `object_attributes.iid` + `object_attributes.action` + `object_attributes.closed_at` | `closed_at` disambiguates reopen/reclose |
| `Pipeline Hook` | `pipeline` + `project.id` + `object_attributes.id` | Pipeline `id` is globally unique |

**Implementation:**

```python
def _make_delivery_id(self, payload: dict, event_type: str) -> str:
    """Generate a stable dedup key from event-identifying fields."""
    obj = payload.get("object_attributes", {})
    project_id = str(payload.get("project", {}).get("id", ""))

    # object_attributes.id is present and unique for all event types we handle
    # (note id, issue id, MR id, pipeline id). It's the strongest anchor.
    obj_id = str(obj.get("id", ""))

    # action distinguishes open/close/update on the same object
    action = obj.get("action", "")

    # For close events, include closed_at to handle reopen/reclose.
    # For other events, omit timestamps to keep retries stable.
    disambiguator = ""
    if action == "close":
        disambiguator = obj.get("closed_at", "")

    parts = [event_type, project_id, obj_id, action, disambiguator]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
```

**Why this is safe:**
- `object_attributes.id` is always present — it's the primary key of the note/issue/MR/pipeline row in GitLab's database.
- For notes (the primary event we handle), `id` alone is globally unique. The other fields are defense in depth.
- Timestamps are only used for close events (where `closed_at` disambiguates reopen-then-reclose). They are NOT used for notes or opens, where they could vary between retries on some GitLab versions.
- Full SHA-256 digest (no truncation).

### Finding 5: Implementation order — providers first, then rewire

Revised order ensures no intermediate commit breaks startup:

1. `provider.py` (types + factory) — no consumers yet
2. `providers/__init__.py` + `providers/github.py` + `providers/gitlab.py` — both implementations, no consumers yet
3. `config.py` (add `GIT_PROVIDER`, `GIT_BASE_URL`, generic aliases) — backward-compatible
4. `webhook.py` (delegate to provider)
5. `dispatch.py` (consume WebhookEvent, use provider methods)
6. `monitor.py` (use `provider.check_completion`)
7. `notifications.py` (use `provider.create_issue`)
8. `prompts/*.py` (pass provider for CLI templates)
9. `.env.example` update
10. Tests

Steps 1-3 are additive only (new files + new config with aliases). Steps 4-8 rewire consumers. Every intermediate commit should be runnable.

### Finding 6: Test coverage — env vars, provider selection, URLs

Add these test categories:

**`tests/test_provider_selection.py`:**
- `GIT_PROVIDER=github` returns `GitHubProvider`
- `GIT_PROVIDER=gitlab` returns `GitLabProvider`
- `GIT_PROVIDER=unknown` raises `ValueError`
- Backward-compat: `GITHUB_WEBHOOK_SECRET` env var resolves to `WEBHOOK_SECRET` when generic name is unset
- Backward-compat: `GITHUB_REPO` env var resolves to `GIT_REPO` when generic name is unset

**`tests/test_provider_github.py`:**
- `parse_webhook` produces correct `WebhookEvent` from sample `issue_comment.created` payload
- `parse_webhook` produces correct `WebhookEvent` from sample `issues.opened` payload
- `parse_webhook` returns `None` for unhandled event types
- `issue_url` returns `https://github.com/{repo}/issues/{n}` with default base
- `issue_url` returns `https://github.example.com/{repo}/issues/{n}` with custom `GIT_BASE_URL`
- Trust: `author_association=OWNER` sets `is_trusted=True`
- Trust: `author_association=NONE` sets `is_trusted=False`

**`tests/test_provider_gitlab.py`:**
- `parse_webhook` produces correct `WebhookEvent` from sample `Note Hook` payload (issue note)
- `parse_webhook` extracts `iid` not `id` for `issue_number`
- `parse_webhook` returns `None` for MR notes (we only route issue notes)
- `verify_webhook` accepts matching `X-Gitlab-Token`, rejects mismatches
- `issue_url` returns `https://gitlab.com/{repo}/-/issues/{n}` with default base
- `issue_url` returns `https://gitlab.company.com/{repo}/-/issues/{n}` with custom base
- Dedup: delivery_id is stable for identical payloads, differs for distinct events

---

## Modified files

### `config.py`
- Add `GIT_PROVIDER = os.getenv("GIT_PROVIDER", "github")`
- Add `GIT_BASE_URL = os.getenv("GIT_BASE_URL", "")` — optional, providers fall back to their defaults
- Add generic aliases with backward-compat fallbacks:
  - `WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", os.environ.get("GITHUB_WEBHOOK_SECRET", ""))`
  - `API_TOKEN = os.environ.get("API_TOKEN", os.environ.get("GITHUB_TOKEN", ""))`
  - `GIT_REPO = os.environ.get("GIT_REPO", os.environ.get("GITHUB_REPO", ""))`
  - `BOT_USERNAME = os.environ.get("BOT_USERNAME", os.environ.get("BOT_GITHUB_USERNAME", ""))`
- Keep old names available for any code that hasn't migrated yet

### `webhook.py`
- Remove `_verify_signature()` function and `GITHUB_WEBHOOK_SECRET` import
- Remove all header-name references (`X-Hub-Signature-256`, `X-GitHub-Event`, `X-GitHub-Delivery`)
- Delegate to `provider.verify_webhook(body, headers)` and `provider.parse_webhook(body, headers)`
- `handle_event` receives `WebhookEvent` instead of 4 separate args

### `dispatch.py`
- Remove: `_fetch_comments()`, `_fetch_pr_context()`, `_fetch_pr_branch()`, `import subprocess`
- Remove: all payload destructuring from `_route()` — replace with `event.kind`, `event.issue_number`, etc.
- `handle_event(event: WebhookEvent)` replaces `handle_event(event_type, action, payload, delivery_id)`
- `_route(event: WebhookEvent)` replaces `_route(event_type, action, payload)`
- Trust checks in comment_created branch use `event.is_trusted`, `event.is_bot`, `event.is_agent_comment`
- `_dispatch_agent` calls `_provider.fetch_comments()`, `_provider.fetch_mr_context()`, `_provider.fetch_mr_branch()`
- Issue URLs use `_provider.issue_url(repo, n)`
- Remove `TRUSTED_AUTHOR_ASSOCIATIONS` and `BOT_GITHUB_USERNAME` imports (handled inside provider)

### `monitor.py`
- Remove `_check_github_completion()` function and its subprocess/json imports
- Replace with `_provider.check_completion(repo, issue_number, agent, iso_started_at)`
- `_sqlite_ts_to_iso()` stays (DB concern, not provider concern)
- Issue URLs in notifications use `_provider.issue_url()`

### `notifications.py`
- Replace `gh issue create` subprocess call with `_provider.create_issue()`
- Replace `GITHUB_REPO` import with `GIT_REPO`

### `prompts/planner.py`, `prompts/implementer.py`, `prompts/reviewer.py`
- `build()` gains a `provider` parameter
- Replace hardcoded `gh` CLI strings with `provider.comment_cli()`, `provider.mr_create_cli()`, etc.
- `_format_thread()` and `_extract_agent_comment()` stay unchanged — `fetch_comments()` normalizes to the same dict shape

### `.env.example`
- Add `GIT_PROVIDER=github` with comment explaining `github` or `gitlab`
- Add `GIT_BASE_URL=` with comment explaining self-hosted use
- Add generic env var names alongside old GitHub-specific ones

---

## What does NOT change

- `state.py` — pure SQLite, no git platform knowledge
- `spawn.py` — pure tmux + git worktree management
- `main.py` — startup orchestration
- `roles/*.md` — static agent role files (the per-task prompt file has the correct CLI commands)
- `_parse_mention()`, `_parse_status()` — operate on plain text, provider-agnostic
- `_handle_comment()` transitions table — routing logic is the same regardless of platform
- `_MENTION_RE`, `_STATUS_RE`, `_DEPENDS_RE`, `_PARENT_RE` — text conventions, not platform features

---

## Implementation order

1. `provider.py` (types + factory)
2. `providers/__init__.py` + `providers/github.py` + `providers/gitlab.py` (both providers, no consumers yet)
3. `config.py` (add `GIT_PROVIDER`, `GIT_BASE_URL`, generic aliases — backward-compatible)
4. `webhook.py` (delegate to provider)
5. `dispatch.py` (consume WebhookEvent, use provider methods)
6. `monitor.py` (use `provider.check_completion`)
7. `notifications.py` (use `provider.create_issue`)
8. `prompts/*.py` (pass provider for CLI templates)
9. `.env.example` update
10. Tests

Steps 1-3 are additive only — no existing code is broken. Steps 4-8 rewire consumers one at a time. Every intermediate commit is runnable.

---

## Verification

```bash
# All existing tests should pass (GitHub is the default provider)
python -m pytest tests/ -v

# Provider selection
GIT_PROVIDER=github python -c "from provider import get_provider; print(type(get_provider()))"
GIT_PROVIDER=gitlab python -c "from provider import get_provider; print(type(get_provider()))"

# Manual: set GIT_PROVIDER=github in .env, start the server, send a test webhook
# Manual: set GIT_PROVIDER=gitlab in .env, verify startup and webhook parsing
```

For GitLab testing, webhook payloads can be verified with `glab` CLI against a test GitLab project, or with curl using sample payloads from GitLab's webhook documentation.
