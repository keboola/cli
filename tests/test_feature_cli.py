"""CLI tests for `kbagent feature` command group (super-admin Manage API).

Exercises the thin Typer layer in ``commands/feature.py`` via ``CliRunner``.
The ``FeatureService`` is mocked by patching ``keboola_agent_cli.cli.FeatureService``
so the instance stored in ``ctx.obj["feature_service"]`` is a ``MagicMock``.

The manage token is sourced through ``resolve_manage_token``: passing the
top-level ``--allow-env-manage-token`` flag plus ``KBC_MANAGE_API_TOKEN`` in the
environment bypasses the interactive prompt (same pattern as test_member_cli.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

STACK_URL = "https://connection.us-east4.gcp.keboola.com"
PROJECT_ID = 5725
ALIAS = "cuesta-master"
EMAIL = "user@example.com"
FEATURE = "queue-v2"
MANAGE_TOKEN = "manage-12345-abcdefghijklmnopqrstuvwxyz0123456789"

runner = CliRunner()


def _seed_store(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        ALIAS,
        ProjectConfig(
            stack_url=STACK_URL,
            token="901-fake-storage-token-1234567890",
            project_name="[Cuesta training] - Master",
            project_id=PROJECT_ID,
        ),
    )
    return store


def _invoke(config_dir: Path, svc: MagicMock, args: list[str], input_text: str | None = None):
    """Invoke the CLI with a mocked FeatureService and env-provided manage token."""
    with (
        patch("keboola_agent_cli.cli.FeatureService", return_value=svc),
        patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
    ):
        return runner.invoke(
            app,
            [
                "--allow-env-manage-token",
                "--config-dir",
                str(config_dir),
                *args,
            ],
            input=input_text,
        )


class TestFeatureList:
    def test_json_happy_path(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_stack_features.return_value = {
            "stack_url": STACK_URL,
            "features": [
                {"name": FEATURE, "title": "Queue v2", "type": "project", "description": "desc"}
            ],
        }

        result = _invoke(config_dir, svc, ["--json", "feature", "list", "--project", ALIAS])

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert out["data"]["features"][0]["name"] == FEATURE
        svc.list_stack_features.assert_called_once_with(manage_token=MANAGE_TOKEN, alias=ALIAS)

    def test_human_mode_smoke(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_stack_features.return_value = {
            "stack_url": STACK_URL,
            "features": [
                {"name": FEATURE, "title": "Queue v2", "type": "project", "description": "desc"}
            ],
        }

        result = _invoke(config_dir, svc, ["feature", "list", "--project", ALIAS])

        assert result.exit_code == 0, result.output
        assert FEATURE in result.output

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_stack_features.side_effect = ConfigError("unknown alias")

        result = _invoke(config_dir, svc, ["--json", "feature", "list", "--project", ALIAS])

        assert result.exit_code == 5, result.output
        out = json.loads(result.output)
        assert out["error"]["code"] == ErrorCode.CONFIG_ERROR

    def test_api_error_maps_to_exit_3(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_stack_features.side_effect = KeboolaApiError(
            message="Invalid or expired token",
            status_code=401,
            error_code=ErrorCode.INVALID_TOKEN,
        )

        result = _invoke(config_dir, svc, ["--json", "feature", "list", "--project", ALIAS])

        assert result.exit_code == 3, result.output


class TestFeatureProjectShow:
    def test_json_happy_path(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_project_features.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "features": [{"name": FEATURE, "title": "Queue v2", "description": "desc"}],
        }

        result = _invoke(config_dir, svc, ["--json", "feature", "project-show", "--project", ALIAS])

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["features"][0]["name"] == FEATURE
        svc.list_project_features.assert_called_once_with(manage_token=MANAGE_TOKEN, alias=ALIAS)

    def test_human_mode_smoke(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_project_features.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "features": [],
        }

        result = _invoke(config_dir, svc, ["feature", "project-show", "--project", ALIAS])

        assert result.exit_code == 0, result.output

    def test_human_mode_omits_empty_optional_columns(self, tmp_path: Path) -> None:
        """Bare-string project features (name-only) drop the Title/Description columns."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_project_features.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            # Mirrors the normalised bare-string shape: name set, rest empty.
            "features": [
                {"name": "queuev2", "title": "", "description": "", "type": ""},
                {"name": "storage-types", "title": "", "description": "", "type": ""},
            ],
        }

        result = _invoke(config_dir, svc, ["feature", "project-show", "--project", ALIAS])

        assert result.exit_code == 0, result.output
        assert "queuev2" in result.output
        # No optional column header should be rendered when every value is empty.
        assert "Title" not in result.output
        assert "Description" not in result.output

    def test_human_mode_keeps_populated_optional_columns(self, tmp_path: Path) -> None:
        """When a feature carries a title, the Title column is shown."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_project_features.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "features": [{"name": "queuev2", "title": "Queue v2", "description": "", "type": ""}],
        }

        result = _invoke(config_dir, svc, ["feature", "project-show", "--project", ALIAS])

        assert result.exit_code == 0, result.output
        assert "Title" in result.output
        # Description is still empty across the board, so its column stays hidden.
        assert "Description" not in result.output


class TestFeatureProjectAdd:
    def test_dry_run_skips_confirmation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.add_project_feature.return_value = {
            "status": "dry_run",
            "action": "add",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "feature": FEATURE,
        }

        # No --yes, no --json, no input: dry-run must short-circuit the prompt.
        result = _invoke(
            config_dir,
            svc,
            ["feature", "project-add", "--project", ALIAS, "--feature", FEATURE, "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        svc.add_project_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature=FEATURE, dry_run=True
        )

    def test_confirm_abort_does_not_call_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()

        result = _invoke(
            config_dir,
            svc,
            ["feature", "project-add", "--project", ALIAS, "--feature", FEATURE],
            input_text="n\n",
        )

        assert result.exit_code == 0, result.output
        assert "Aborted." in result.output
        svc.add_project_feature.assert_not_called()

    def test_yes_skips_confirmation_and_adds(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.add_project_feature.return_value = {
            "status": "added",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            ["--json", "feature", "project-add", "--project", ALIAS, "--feature", FEATURE, "--yes"],
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["status"] == "added"
        svc.add_project_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature=FEATURE, dry_run=False
        )

    def test_api_error_maps_to_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.add_project_feature.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code=ErrorCode.API_ERROR,
        )

        result = _invoke(
            config_dir,
            svc,
            ["--json", "feature", "project-add", "--project", ALIAS, "--feature", FEATURE, "--yes"],
        )

        assert result.exit_code == 1, result.output


class TestFeatureProjectRemove:
    def test_dry_run_skips_confirmation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.remove_project_feature.return_value = {
            "status": "dry_run",
            "action": "remove",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            ["feature", "project-remove", "--project", ALIAS, "--feature", FEATURE, "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        svc.remove_project_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature=FEATURE, dry_run=True
        )

    def test_confirm_abort_does_not_call_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()

        result = _invoke(
            config_dir,
            svc,
            ["feature", "project-remove", "--project", ALIAS, "--feature", FEATURE],
            input_text="n\n",
        )

        assert result.exit_code == 0, result.output
        assert "Aborted." in result.output
        svc.remove_project_feature.assert_not_called()

    def test_yes_skips_confirmation_and_removes(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.remove_project_feature.return_value = {
            "status": "removed",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "feature",
                "project-remove",
                "--project",
                ALIAS,
                "--feature",
                FEATURE,
                "--yes",
            ],
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["status"] == "removed"
        svc.remove_project_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, feature=FEATURE, dry_run=False
        )


class TestFeatureUserShow:
    def test_json_happy_path(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_user_features.return_value = {
            "email": EMAIL,
            "features": [{"name": FEATURE, "title": "Queue v2", "description": "desc"}],
        }

        result = _invoke(
            config_dir,
            svc,
            ["--json", "feature", "user-show", "--project", ALIAS, "--email", EMAIL],
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["features"][0]["name"] == FEATURE
        svc.list_user_features.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL
        )

    def test_human_mode_smoke(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_user_features.return_value = {"email": EMAIL, "features": []}

        result = _invoke(
            config_dir,
            svc,
            ["feature", "user-show", "--project", ALIAS, "--email", EMAIL],
        )

        assert result.exit_code == 0, result.output


class TestFeatureUserAdd:
    def test_dry_run_skips_confirmation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.add_user_feature.return_value = {
            "status": "dry_run",
            "action": "add",
            "email": EMAIL,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            [
                "feature",
                "user-add",
                "--project",
                ALIAS,
                "--email",
                EMAIL,
                "--feature",
                FEATURE,
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        svc.add_user_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature=FEATURE, dry_run=True
        )

    def test_confirm_abort_does_not_call_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()

        result = _invoke(
            config_dir,
            svc,
            ["feature", "user-add", "--project", ALIAS, "--email", EMAIL, "--feature", FEATURE],
            input_text="n\n",
        )

        assert result.exit_code == 0, result.output
        assert "Aborted." in result.output
        svc.add_user_feature.assert_not_called()

    def test_yes_skips_confirmation_and_adds(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.add_user_feature.return_value = {
            "status": "added",
            "email": EMAIL,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "feature",
                "user-add",
                "--project",
                ALIAS,
                "--email",
                EMAIL,
                "--feature",
                FEATURE,
                "--yes",
            ],
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["status"] == "added"
        svc.add_user_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature=FEATURE, dry_run=False
        )


class TestFeatureUserRemove:
    def test_dry_run_skips_confirmation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.remove_user_feature.return_value = {
            "status": "dry_run",
            "action": "remove",
            "email": EMAIL,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            [
                "feature",
                "user-remove",
                "--project",
                ALIAS,
                "--email",
                EMAIL,
                "--feature",
                FEATURE,
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        svc.remove_user_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature=FEATURE, dry_run=True
        )

    def test_confirm_abort_does_not_call_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()

        result = _invoke(
            config_dir,
            svc,
            ["feature", "user-remove", "--project", ALIAS, "--email", EMAIL, "--feature", FEATURE],
            input_text="n\n",
        )

        assert result.exit_code == 0, result.output
        assert "Aborted." in result.output
        svc.remove_user_feature.assert_not_called()

    def test_yes_skips_confirmation_and_removes(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.remove_user_feature.return_value = {
            "status": "removed",
            "email": EMAIL,
            "feature": FEATURE,
        }

        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "feature",
                "user-remove",
                "--project",
                ALIAS,
                "--email",
                EMAIL,
                "--feature",
                FEATURE,
                "--yes",
            ],
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["status"] == "removed"
        svc.remove_user_feature.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email=EMAIL, feature=FEATURE, dry_run=False
        )
