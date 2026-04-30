"""Tests for ConfigService.set_default_bucket and the CLI command.

Covers the read-modify-write of configuration.storage.output.default_bucket.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import Result
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _detail_with(default_bucket: str | None, *, with_other_storage: bool = True) -> dict:
    """Build a SAMPLE config detail with the given default_bucket value.

    *None* means the key is absent.  *with_other_storage* adds a
    sibling storage.input.tables and storage.output.tables to verify they
    survive read-modify-write.
    """
    output: dict = {}
    if default_bucket is not None:
        output["default_bucket"] = default_bucket
    if with_other_storage:
        output["tables"] = [{"source": "x.csv", "destination": "in.c-x.x"}]

    storage: dict = {"output": output}
    if with_other_storage:
        storage["input"] = {"tables": [{"source": "in.c-y.y"}]}

    return {
        "id": "cfg-001",
        "name": "My Config",
        "description": "desc",
        "configuration": {
            "parameters": {"user": "admin"},
            "storage": storage,
        },
    }


def _make_service(
    tmp_config_dir: Path,
    detail: dict | None = None,
) -> tuple[ConfigService, MagicMock]:
    """Create a ConfigService with a mock client + an injected config detail."""
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_detail.return_value = detail or _detail_with(None)
    mock_client.update_config.return_value = {
        "id": "cfg-001",
        "name": "My Config",
        "componentId": "keboola.ex-db-snowflake",
    }
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestConfigServiceSetDefaultBucket:
    """Tests for ConfigService.set_default_bucket."""

    def test_set_on_config_without_default(self, tmp_config_dir: Path) -> None:
        """Adds default_bucket where none was set before."""
        service, client = _make_service(tmp_config_dir, _detail_with(None))

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-preferred",
        )

        client.update_config.assert_called_once()
        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-preferred"

    def test_set_overwrites_existing(self, tmp_config_dir: Path) -> None:
        """Replaces an existing value; sibling output keys preserved."""
        service, client = _make_service(tmp_config_dir, _detail_with("in.c-old"))

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-new",
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-new"
        # storage.output.tables sibling preserved
        assert cfg["storage"]["output"]["tables"] == [
            {"source": "x.csv", "destination": "in.c-x.x"}
        ]

    def test_set_preserves_other_storage_branches(self, tmp_config_dir: Path) -> None:
        """storage.input is untouched."""
        service, client = _make_service(tmp_config_dir, _detail_with(None))

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-preferred",
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["input"] == {"tables": [{"source": "in.c-y.y"}]}
        assert cfg["parameters"] == {"user": "admin"}

    def test_clear_removes_key(self, tmp_config_dir: Path) -> None:
        """clear=True pops default_bucket but keeps storage.output dict."""
        service, client = _make_service(tmp_config_dir, _detail_with("in.c-old"))

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket=None,
            clear=True,
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert "default_bucket" not in cfg["storage"]["output"]
        assert "tables" in cfg["storage"]["output"]

    def test_clear_when_absent_is_noop(self, tmp_config_dir: Path) -> None:
        """No write when there's nothing to clear."""
        service, client = _make_service(tmp_config_dir, _detail_with(None))

        result = service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket=None,
            clear=True,
        )

        assert result == {
            "changed": False,
            "project_alias": "prod",
            "component_id": "keboola.ex-db-snowflake",
            "config_id": "cfg-001",
            "branch_id": None,
            "default_bucket": None,
        }
        client.update_config.assert_not_called()

    def test_set_tolerates_storage_none(self, tmp_config_dir: Path) -> None:
        """Raw-mode edits can leave storage=None; service should not crash."""
        detail = {
            "id": "cfg-001",
            "name": "n",
            "configuration": {"parameters": {"x": 1}, "storage": None},
        }
        service, client = _make_service(tmp_config_dir, detail)

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-x",
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-x"

    def test_set_tolerates_output_none(self, tmp_config_dir: Path) -> None:
        """Same for storage.output=None."""
        detail = {
            "id": "cfg-001",
            "name": "n",
            "configuration": {"parameters": {"x": 1}, "storage": {"output": None}},
        }
        service, client = _make_service(tmp_config_dir, detail)

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-x",
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-x"

    def test_clear_tolerates_storage_none(self, tmp_config_dir: Path) -> None:
        """Clearing on storage=None is a no-op (nothing to remove, no write)."""
        detail = {
            "id": "cfg-001",
            "name": "n",
            "configuration": {"parameters": {"x": 1}, "storage": None},
        }
        service, client = _make_service(tmp_config_dir, detail)

        result = service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket=None,
            clear=True,
        )

        assert result["changed"] is False
        client.update_config.assert_not_called()

    def test_set_handles_json_string_configuration(self, tmp_config_dir: Path) -> None:
        """Storage API sometimes returns 'configuration' as a JSON string for
        legacy configs; the service decodes it before mutating."""
        detail = {
            "id": "cfg-001",
            "name": "n",
            "configuration": json.dumps({"parameters": {"x": 1}, "storage": {}}),
        }
        service, client = _make_service(tmp_config_dir, detail)

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-from-string",
        )

        cfg = client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-from-string"
        assert cfg["parameters"] == {"x": 1}

    def test_set_same_value_is_noop(self, tmp_config_dir: Path) -> None:
        """Setting the current value short-circuits."""
        service, client = _make_service(tmp_config_dir, _detail_with("in.c-same"))

        result = service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-same",
        )

        assert result["changed"] is False
        assert result["default_bucket"] == "in.c-same"
        client.update_config.assert_not_called()

    def test_dry_run_does_not_write(self, tmp_config_dir: Path) -> None:
        """dry_run returns a diff payload and skips the API."""
        service, client = _make_service(tmp_config_dir, _detail_with("in.c-old"))

        result = service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-new",
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert any("default_bucket" in c for c in result["changes"])
        assert result["new_configuration"]["storage"]["output"]["default_bucket"] == "in.c-new"
        client.update_config.assert_not_called()

    def test_validation_both_flags(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError, match="exactly one"):
            service.set_default_bucket(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                bucket="in.c-x",
                clear=True,
            )

    def test_validation_neither_flag(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError, match="--bucket"):
            service.set_default_bucket(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                bucket=None,
                clear=False,
            )

    def test_validation_empty_bucket(self, tmp_config_dir: Path) -> None:
        service, _ = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError, match="cannot be empty"):
            service.set_default_bucket(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                bucket="   ",
            )

    def test_branch_id_propagated(self, tmp_config_dir: Path) -> None:
        """Explicit branch_id reaches the client."""
        service, client = _make_service(tmp_config_dir, _detail_with(None))

        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-x",
            branch_id=42,
        )

        assert client.get_config_detail.call_args.kwargs["branch_id"] == 42
        assert client.update_config.call_args.kwargs["branch_id"] == 42

    def test_client_closed_on_success(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, _detail_with(None))
        service.set_default_bucket(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            bucket="in.c-x",
        )
        client.close.assert_called_once()

    def test_client_closed_on_error(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, _detail_with(None))
        client.update_config.side_effect = KeboolaApiError(
            status_code=500, error_code="SERVER_ERROR", message="boom"
        )
        with pytest.raises(KeboolaApiError):
            service.set_default_bucket(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                bucket="in.c-x",
            )
        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestConfigSetDefaultBucketCli:
    """CLI-level tests for the set-default-bucket command."""

    @staticmethod
    def _invoke(tmp_config_dir: Path, args: list[str], json_mode: bool = True) -> Result:
        base = ["--config-dir", str(tmp_config_dir)]
        if json_mode:
            base = ["--json", *base]
        return runner.invoke(app, [*base, "config", "set-default-bucket", *args])

    @staticmethod
    def _patch_service(mp: pytest.MonkeyPatch, store, mock_client: MagicMock) -> None:
        mp.setattr(
            "keboola_agent_cli.commands.config.get_service",
            lambda ctx, name: ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            ),
        )

    def test_set_human_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _detail_with(None)
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
        }

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--bucket",
                    "in.c-preferred",
                ],
                json_mode=False,
            )

        assert result.exit_code == 0, result.output
        assert "Set default_bucket" in result.output
        cfg = mock_client.update_config.call_args.kwargs["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == "in.c-preferred"

    def test_set_json_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _detail_with(None)
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "My Config",
        }

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--bucket",
                    "in.c-preferred",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["default_bucket"] == "in.c-preferred"
        assert payload["data"]["project_alias"] == "prod"

    def test_clear_json_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _detail_with("in.c-old")
        mock_client.update_config.return_value = {"id": "cfg-001", "name": "My Config"}

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--clear",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["default_bucket"] is None

    def test_dry_run_json_output(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = _detail_with("in.c-old")

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, store, mock_client)
            result = self._invoke(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--bucket",
                    "in.c-new",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["dry_run"] is True
        assert any("default_bucket" in c for c in payload["data"]["changes"])
        mock_client.update_config.assert_not_called()

    def test_mutual_exclusion(self, tmp_config_dir: Path) -> None:
        result = self._invoke(
            tmp_config_dir,
            [
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-db-snowflake",
                "--config-id",
                "cfg-001",
                "--bucket",
                "in.c-x",
                "--clear",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "VALIDATION_ERROR" in result.output

    def test_missing_action(self, tmp_config_dir: Path) -> None:
        result = self._invoke(
            tmp_config_dir,
            [
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-db-snowflake",
                "--config-id",
                "cfg-001",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "VALIDATION_ERROR" in result.output

    def test_unknown_project(self, tmp_config_dir: Path) -> None:
        setup_single_project(tmp_config_dir)
        result = self._invoke(
            tmp_config_dir,
            [
                "--project",
                "ghost",
                "--component-id",
                "keboola.ex-db-snowflake",
                "--config-id",
                "cfg-001",
                "--bucket",
                "in.c-x",
            ],
        )
        assert result.exit_code == 5, result.output
        assert "CONFIG_ERROR" in result.output
