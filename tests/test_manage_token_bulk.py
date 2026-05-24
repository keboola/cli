"""Pin the bulk-prompt-once contract for resolve_manage_token (since 0.28.0).

Padak's review on PR #238 explicitly required: when a single command
invocation processes N projects (e.g. `kbagent project refresh --all`),
the manage-token TTY prompt must fire **exactly once** at command entry,
never per-project. That contract holds today by construction: the
resolver lives at command entry, before any per-project loop. This test
pins it so a future refactor that pushes resolution into the service
loop fails loudly.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig

runner = CliRunner()

TEST_STORAGE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


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
        )
    store.save(AppConfig(projects=projects))
    return store


class TestBulkPromptOnce:
    def test_project_refresh_all_resolves_token_once_for_n_projects(self, tmp_path: Path) -> None:
        """`project refresh --all` with 5 projects on 3 stacks must call
        ``resolve_manage_token`` exactly once. The resolver lives at command
        entry; pushing it into the per-project loop would prompt N times
        (or N times with different stack URLs). This test fails loudly if
        a future refactor moves the call into a loop.

        Patching ``resolve_manage_token`` directly (rather than the inner
        TTY prompt) is the right granularity: it tests the command-layer
        contract independently of how the resolver decides between env
        and TTY internally."""
        store = _make_store_n_projects(tmp_path, 5)
        resolver_mock = MagicMock(return_value="bulk-resolved-token")
        mock_org = MagicMock()
        mock_org.refresh_tokens.return_value = {
            "status": "ok",
            "refreshed": [],
            "errors": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                resolver_mock,
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_org
            result = runner.invoke(app, ["--json", "project", "refresh", "--all", "--yes"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # The contract: exactly one resolver call regardless of project count.
        assert resolver_mock.call_count == 1, (
            f"Expected resolve_manage_token to be called once for --all "
            f"across 5 projects, got {resolver_mock.call_count} calls -- "
            f"per-project resolution regression. Manage-token resolution "
            f"must live at command entry, never in a per-project loop."
        )
        # The service receives the resolved token and fans out internally.
        assert mock_org.refresh_tokens.call_count == 1
        assert mock_org.refresh_tokens.call_args.kwargs["manage_token"] == "bulk-resolved-token"
