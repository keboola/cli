"""CLI tests for ``kbagent config clone`` (issue #587).

Scope:
- --set / --secret PATH=VALUE parsing, including values containing '='.
- Both output modes actually render. The human renderer is the reason this
  file exists: the service-layer suite passes with a broken renderer, because
  it never goes through OutputFormatter. A first live run crashed on exactly
  that (a human_formatter taking one argument instead of two), which no
  service test could have caught.
- Error propagation: a refused cross-project clone surfaces as exit 5.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner, Result

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Production",
            project_id=1234,
        ),
    )
    return store


def _clone_result(**overrides: object) -> dict:
    result = {
        "id": "clone-1",
        "mode": "same-project",
        "source_project": "prod",
        "target_project": "prod",
        "component_id": "keboola.wr-db-snowflake",
        "source_config_id": "src-1",
        "source_version": 7,
        "encrypted_paths": [],
        "copied_rows": [],
    }
    result.update(overrides)
    return result


def _invoke(
    args: list[str],
    *,
    config_dir: Path,
    service_mock: MagicMock | None = None,
) -> Result:
    store = _setup_config(config_dir)
    svc = service_mock or MagicMock()
    if service_mock is None:
        svc.clone_config.return_value = _clone_result()

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.ConfigService") as MockConfigService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockConfigService.return_value = svc
        return runner.invoke(app, args)


BASE_ARGS = [
    "config",
    "clone",
    "--project",
    "prod",
    "--component-id",
    "keboola.wr-db-snowflake",
    "--config-id",
    "src-1",
    "--name",
    "Clone",
]


class TestCloneOptionParsing:
    def test_set_and_secret_pairs_reach_the_service(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.clone_config.return_value = _clone_result()

        result = _invoke(
            [*BASE_ARGS, "--set", "parameters.db.host=h2", "--secret", "parameters.db.#password=p"],
            config_dir=tmp_path,
            service_mock=svc,
        )

        assert result.exit_code == 0, result.output
        kwargs = svc.clone_config.call_args.kwargs
        assert kwargs["set_overrides"] == {"parameters.db.host": "h2"}
        assert kwargs["secret_overrides"] == {"parameters.db.#password": "p"}

    def test_value_containing_equals_is_preserved(self, tmp_path: Path) -> None:
        """Split on the FIRST '=' only -- base64 padding and connection
        strings routinely contain more.
        """
        svc = MagicMock()
        svc.clone_config.return_value = _clone_result()

        result = _invoke(
            [*BASE_ARGS, "--secret", "parameters.#token=abc==def="],
            config_dir=tmp_path,
            service_mock=svc,
        )

        assert result.exit_code == 0, result.output
        assert svc.clone_config.call_args.kwargs["secret_overrides"] == {
            "parameters.#token": "abc==def="
        }

    def test_malformed_pair_exits_2(self, tmp_path: Path) -> None:
        result = _invoke([*BASE_ARGS, "--set", "no-equals-sign"], config_dir=tmp_path)

        assert result.exit_code == 2, result.output
        assert "Expected PATH=VALUE" in result.output

    def test_target_project_is_forwarded(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.clone_config.return_value = _clone_result(mode="cross-project", target_project="dev")

        result = _invoke(
            [*BASE_ARGS, "--target-project", "dev"], config_dir=tmp_path, service_mock=svc
        )

        assert result.exit_code == 0, result.output
        assert svc.clone_config.call_args.kwargs["target_alias"] == "dev"


class TestCloneOutput:
    """Both renderers must survive a real OutputFormatter round-trip."""

    def test_human_output_reports_the_new_id(self, tmp_path: Path) -> None:
        result = _invoke(BASE_ARGS, config_dir=tmp_path)

        assert result.exit_code == 0, result.output
        assert "clone-1" in result.output

    def test_human_dry_run_lists_missing_secrets(self, tmp_path: Path) -> None:
        """--dry-run is how a caller learns which --secret values to gather,
        so the paths must actually appear in human output.
        """
        svc = MagicMock()
        svc.clone_config.return_value = {
            "dry_run": True,
            "mode": "cross-project",
            "source_project": "prod",
            "target_project": "dev",
            "component_id": "keboola.wr-db-snowflake",
            "source_config_id": "src-1",
            "source_version": 7,
            "name": "Clone",
            "row_count": 2,
            "encrypted_paths": ["parameters.db.#password"],
            "missing_secrets": ["parameters.db.#password"],
        }

        result = _invoke(
            [*BASE_ARGS, "--target-project", "dev", "--dry-run"],
            config_dir=tmp_path,
            service_mock=svc,
        )

        assert result.exit_code == 0, result.output
        assert "parameters.db.#password" in result.output
        assert "--secret" in result.output

    def test_cross_project_human_output_warns_about_bucket_mapping(self, tmp_path: Path) -> None:
        """Storage mappings are copied verbatim; the operator has to be told."""
        svc = MagicMock()
        svc.clone_config.return_value = _clone_result(
            mode="cross-project",
            target_project="dev",
            copied_rows=[{"source_row_id": "r1", "id": "n1"}],
        )

        result = _invoke(
            [*BASE_ARGS, "--target-project", "dev"], config_dir=tmp_path, service_mock=svc
        )

        assert result.exit_code == 0, result.output
        assert "NOT remapped" in result.output

    def test_json_output_is_the_service_envelope(self, tmp_path: Path) -> None:
        import json

        result = _invoke(["--json", *BASE_ARGS], config_dir=tmp_path)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["id"] == "clone-1"
        assert payload["data"]["mode"] == "same-project"


class TestCloneErrors:
    def test_refused_clone_exits_5(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.clone_config.side_effect = ConfigError(
            "Cannot clone into project 'dev': 1 encrypted value(s)"
        )

        result = _invoke(
            [*BASE_ARGS, "--target-project", "dev"], config_dir=tmp_path, service_mock=svc
        )

        assert result.exit_code == 5, result.output
        assert "encrypted value" in result.output
