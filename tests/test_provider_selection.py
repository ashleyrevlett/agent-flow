"""
Tests for provider selection and config env alias fallback behavior.
"""

import os
import importlib
import pytest

os.environ.setdefault("WEBHOOK_SECRET", "test")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test")
os.environ.setdefault("API_TOKEN", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("GIT_REPO", "owner/repo")
os.environ.setdefault("GITHUB_REPO", "owner/repo")
os.environ.setdefault("SQLITE_DB_PATH", "/tmp/test-selection.db")


class TestProviderFactory:
    def test_github_returns_github_provider(self):
        import config
        config.GIT_PROVIDER = "github"
        from provider import get_provider
        from providers.github import GitHubProvider
        p = get_provider()
        assert isinstance(p, GitHubProvider)
        config.GIT_PROVIDER = os.environ.get("GIT_PROVIDER", "github")

    def test_gitlab_returns_gitlab_provider(self):
        import config
        config.GIT_PROVIDER = "gitlab"
        from provider import get_provider
        from providers.gitlab import GitLabProvider
        p = get_provider()
        assert isinstance(p, GitLabProvider)
        config.GIT_PROVIDER = os.environ.get("GIT_PROVIDER", "github")

    def test_unknown_provider_raises(self):
        import config
        config.GIT_PROVIDER = "bitbucket"
        from provider import get_provider
        with pytest.raises(ValueError, match="Unknown GIT_PROVIDER"):
            get_provider()
        config.GIT_PROVIDER = os.environ.get("GIT_PROVIDER", "github")


class TestConfigEnvAliases:
    """Verify that legacy GITHUB_* env vars resolve to generic names."""

    def test_webhook_secret_falls_back_to_github_name(self):
        import config
        # Both should resolve to the same value
        assert config.WEBHOOK_SECRET == config.GITHUB_WEBHOOK_SECRET

    def test_git_repo_falls_back_to_github_name(self):
        import config
        assert config.GIT_REPO == config.GITHUB_REPO

    def test_api_token_falls_back_to_github_name(self):
        import config
        assert config.API_TOKEN == config.GITHUB_TOKEN

    def test_bot_username_falls_back_to_github_name(self):
        import config
        assert config.BOT_USERNAME == config.BOT_GITHUB_USERNAME
