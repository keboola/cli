"""Pin the bulk-prompt-once contract for the Manage-token resolver (since 0.28.0).

Padak's review on PR #238 explicitly required: when a single command
invocation processes N projects (e.g. `kbagent project refresh --all-projects`),
the manage-token TTY prompt must fire **at most once**, never per-project.

Since #955 the token is resolved lazily: `project refresh` hands the service a
provider, and the service calls it once -- after checking every project and
before the first write -- and only when a token must be minted. These tests
run the real OrgService (mocked HTTP clients) so a refactor that moves the
provider call into the per-project loop fails loudly.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner, Result

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.org_service import OrgService

runner = CliRunner()

TEST_STORAGE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
MINTED_TOKEN = "901-77777-mintedTestTokenDoNotUseXXXXXX"


def _make_store_n_projects(tmp_path: Path, n: int) -> ConfigStore:
    """Create a ConfigStore with N registered projects on different stacks."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    projects = {}
    for i in range(n):
        # Spread across multiple stacks to make the bulk-once contract
        # meaningful: a per-project loop would naively prompt N times,
        # potentially with different stack URLs visible in the prompt.
        stack = ["us-east4.gcp", "eu-central-1", "north-europe.azure"][i % 3]
        projects[f"proj-{i}"] = ProjectConfig(
            stack_url=f"https://connection.{stack}.keboola.com",
            token=TEST_STORAGE_TOKEN,
            project_id=i + 1,
        )
    store.save(AppConfig(projects=projects))
    return store


def _run(store: ConfigStore, *, old_token_valid: bool) -> tuple[Result, MagicMock]:
    """Run `project refresh --all-projects` on the real service; return (result, resolver)."""

    def storage_factory(url: str, token: str) -> MagicMock:
        client = MagicMock()
        if token == TEST_STORAGE_TOKEN and not old_token_valid:
            client.verify_token.side_effect = KeboolaApiError(
                message="Invalid token", status_code=401, error_code=ErrorCode.INVALID_TOKEN
            )
        else:
            client.verify_token.return_value = TokenVerifyResponse(
                token_id="t", token_description="d", project_id=1, project_name="P", owner_name=""
            )
        return client

    manage = MagicMock()
    manage.verify_token.return_value = {"user": {"email": "admin@test.com"}}
    manage.create_project_token.return_value = {"id": "x", "token": MINTED_TOKEN}

    def build(config_store: ConfigStore) -> OrgService:
        return OrgService(
            config_store=config_store,
            manage_client_factory=lambda url, token: manage,
            storage_client_factory=storage_factory,
        )

    resolver_mock = MagicMock(return_value="bulk-resolved-token")
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.OrgService", build),
        patch("keboola_agent_cli.commands.project.manage_token_if_available", resolver_mock),
    ):
        result = runner.invoke(app, ["--json", "project", "refresh", "--all-projects", "--yes"])
    return result, resolver_mock


class TestBulkPromptOnce:
    def test_project_refresh_all_resolves_token_once_for_n_projects(self, tmp_path: Path) -> None:
        """5 projects on 3 stacks, every token dead: ``manage_token_if_available`` is
        called exactly once and all 5 tokens are minted with its token."""
        store = _make_store_n_projects(tmp_path, 5)

        result, resolver_mock = _run(store, old_token_valid=False)

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert resolver_mock.call_count == 1, (
            f"Expected manage_token_if_available to be called once across 5 projects, got "
            f"{resolver_mock.call_count} calls -- per-project resolution regression."
        )
        config = store.load()
        assert all(p.token == MINTED_TOKEN for p in config.projects.values())

    def test_project_refresh_with_nothing_to_mint_never_resolves_the_token(
        self, tmp_path: Path
    ) -> None:
        """Every token valid: no prompt at all (lazy contract, #955)."""
        store = _make_store_n_projects(tmp_path, 5)

        result, resolver_mock = _run(store, old_token_valid=True)

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        resolver_mock.assert_not_called()
