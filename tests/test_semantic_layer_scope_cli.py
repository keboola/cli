"""CLI-layer tests for ``semantic-layer scope`` and the ``--scope``/
``--target-project`` flags on ``model create`` / ``add <kind>`` (PSGO-140).

Mirrors the test_semantic_layer_cli.py pattern: patch cli.py's service
factory so the runner sees a MagicMock SemanticLayerService, plus a REAL
ProjectService (built from the test ConfigStore) since ``resolve_scope_targets``
reads project aliases through it for the interactive-picker fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict]) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    for alias, info in projects.items():
        store.add_project(
            alias,
            ProjectConfig(
                stack_url=info.get("stack_url", "https://connection.keboola.com"),
                token=info["token"],
                project_name=info.get("project_name", alias),
                project_id=info.get("project_id", 1234),
            ),
        )
    return store


def _invoke(args: list[str], *, store: ConfigStore, sl_mock: MagicMock):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.SemanticLayerService") as MockSL,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockSL.return_value = sl_mock
        return runner.invoke(app, args)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def store(cfg_dir: Path) -> ConfigStore:
    return _setup_config(
        cfg_dir,
        {
            "prod": {"token": TEST_TOKEN, "project_id": 5725},
            "analytics": {"token": TEST_TOKEN, "project_id": 1234},
        },
    )


# ---------------------------------------------------------------------------
# --scope / --target-project on model create / add <kind>
# ---------------------------------------------------------------------------


class TestModelCreateScope:
    def test_default_scope_is_project_and_unrequested(self, store: ConfigStore) -> None:
        """No --scope passed: create_model still gets scope='project', no picker touched."""
        mock = MagicMock()
        mock.create_model.return_value = {"project": "prod", "model": {"id": "u", "attributes": {}}}
        result = _invoke(
            ["--json", "semantic-layer", "model", "create", "--project", "prod", "--name", "n"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock.create_model.call_args
        assert kwargs["scope"] == "project"
        assert kwargs["target_project_ids"] is None

    def test_targeted_scope_with_explicit_target_project(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.create_model.return_value = {"project": "prod", "model": {"id": "u", "attributes": {}}}
        mock.resolve_target_project_ids.return_value = [1234]
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "n",
                "--scope",
                "targeted",
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.resolve_target_project_ids.assert_called_once_with(["analytics"])
        _args, kwargs = mock.create_model.call_args
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_project_ids"] == [1234]

    def test_targeted_scope_without_target_project_fails_fast_non_tty(
        self, store: ConfigStore
    ) -> None:
        """No --target-project + CliRunner's non-TTY stdin/stdout -> hard fail, never a hang."""
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "n",
                "--scope",
                "targeted",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_model.assert_not_called()
        body = json.loads(result.output)
        assert body["error"]["code"] == "INVALID_ARGUMENT"

    def test_organization_scope_ignores_absent_target_project(self, store: ConfigStore) -> None:
        """--scope organization never needs --target-project; no picker, no failure."""
        mock = MagicMock()
        mock.create_model.return_value = {"project": "prod", "model": {"id": "u", "attributes": {}}}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "n",
                "--scope",
                "organization",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock.create_model.call_args
        assert kwargs["scope"] == "organization"
        assert kwargs["target_project_ids"] is None
        mock.resolve_target_project_ids.assert_not_called()


class TestAddMetricScope:
    def test_add_metric_targeted_scope(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_metric.return_value = {"id": "m1", "attributes": {"name": "rev"}}
        mock.resolve_target_project_ids.return_value = [1234]
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--sql",
                "SUM(x)",
                "--dataset",
                "out.c-foo.bar",
                "--scope",
                "targeted",
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock.add_metric.call_args
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_project_ids"] == [1234]


# ---------------------------------------------------------------------------
# semantic-layer scope <status|grant|request-elevation|withdraw-elevation|elevate|pending>
# ---------------------------------------------------------------------------


class TestScopeStatus:
    def test_status_success(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_status.return_value = {
            "id": "d1",
            "scope": "targeted",
            "target_project_ids": [1234],
            "scope_elevation_requested_at": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "status",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_status.assert_called_once_with(alias="prod", kind="dataset", item_id="d1")
        assert json.loads(result.output)["data"]["scope"] == "targeted"


class TestScopeGrant:
    def test_grant_add_default_merge(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_grant.return_value = {"scope": "targeted", "target_project_ids": [1234]}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "grant",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_grant.assert_called_once_with(
            alias="prod",
            kind="dataset",
            item_id="d1",
            add=["analytics"],
            remove=None,
            replace=None,
        )

    def test_grant_replace(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_grant.return_value = {"scope": "targeted", "target_project_ids": [1234]}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "grant",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
                "--target-project",
                "analytics",
                "--replace",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock.scope_grant.call_args
        assert kwargs["replace"] == ["analytics"]

    def test_grant_clear(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_grant.return_value = {"scope": "targeted", "target_project_ids": []}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "grant",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
                "--clear",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock.scope_grant.call_args
        assert kwargs["replace"] == []

    def test_clear_combined_with_target_project_is_usage_error(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "grant",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
                "--clear",
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.scope_grant.assert_not_called()


class TestScopeElevation:
    def test_request_elevation(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_request_elevation.return_value = {"scope_elevation_requested_at": "t"}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "request-elevation",
                "--project",
                "prod",
                "--type",
                "metric",
                "--id",
                "m1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_request_elevation.assert_called_once_with(
            alias="prod", kind="metric", item_id="m1"
        )

    def test_withdraw_elevation(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_withdraw_elevation.return_value = {"scope_elevation_requested_at": None}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "withdraw-elevation",
                "--project",
                "prod",
                "--type",
                "metric",
                "--id",
                "m1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_withdraw_elevation.assert_called_once_with(
            alias="prod", kind="metric", item_id="m1"
        )

    def test_elevate_with_yes_skips_prompt(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_elevate.return_value = {"scope": "organization"}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "elevate",
                "--project",
                "prod",
                "--type",
                "metric",
                "--id",
                "m1",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_elevate.assert_called_once_with(alias="prod", kind="metric", item_id="m1")

    def test_pending(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_pending.return_value = [
            {"id": "d1", "name": "x", "scope_elevation_requested_at": "t"}
        ]
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "scope",
                "pending",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--limit",
                "5",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_pending.assert_called_once_with(
            alias="prod", kind="dataset", limit=5, offset=None
        )


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------


class TestScopePermissions:
    def test_deny_writes_blocks_grant(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "scope",
                "grant",
                "--project",
                "prod",
                "--type",
                "dataset",
                "--id",
                "d1",
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.scope_grant.assert_not_called()

    def test_deny_destructive_blocks_elevate(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-destructive",
                "--json",
                "semantic-layer",
                "scope",
                "elevate",
                "--project",
                "prod",
                "--type",
                "metric",
                "--id",
                "m1",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.scope_elevate.assert_not_called()

    def test_deny_writes_does_not_block_status(self, store: ConfigStore) -> None:
        """--deny-writes must not block the read-only `scope status` leaf."""
        mock = MagicMock()
        mock.scope_status.return_value = {"scope": "project"}
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "scope",
                "status",
                "--project",
                "prod",
                "--type",
                "metric",
                "--id",
                "m1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.scope_status.assert_called_once()
