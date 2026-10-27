"""Tests for the `kbagent oauth` command layer via CliRunner."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services._oauth_tools import PasswordCredential
from keboola_agent_cli.services.oauth_service import RotationPlan, stack_hosts

runner = CliRunner()
SERVICE = "keboola_agent_cli.commands.oauth._service"
ROTATE = [
    "oauth",
    "rotate-azure-secret",
    "--stack",
    "connection.keboola.com",
    "--component",
    "kds-team.wr-powerbi",
    "--app-id",
    "app-1",
    "--op-vault",
    "vault",
]


def _plan() -> RotationPlan:
    return RotationPlan(
        hosts=stack_hosts("keboola.com"),
        component_id="kds-team.wr-powerbi",
        app_id="app-1",
        app_name="Power BI",
        tenant_id="tenant-1",
        az_user="l1@example.com",
        op_vault_id="vault-id",
        op_vault_name="vault",
        end_date="2028-10-26",
        current_for_component=[PasswordCredential("old-key", "kds-team.wr-powerbi", "2026-11-01")],
        other_credentials=1,
        secret_fingerprint="abc",
        token="123-secret-token",
    )


def _invoke(tmp_config_dir, args: list[str], **kwargs):
    return runner.invoke(app, ["--config-dir", str(tmp_config_dir), *args], **kwargs)


def test_identity_add_refuses_without_terminal(tmp_config_dir) -> None:
    with patch(SERVICE) as factory:
        r = _invoke(
            tmp_config_dir,
            ["oauth", "identity", "add", "--stack", "keboola.com"],
            input="123-tok\n",
        )
    assert r.exit_code == 6, r.output
    factory.assert_not_called()


def test_identity_add_has_no_token_option(tmp_config_dir) -> None:
    r = _invoke(tmp_config_dir, ["oauth", "identity", "add", "--stack", "x.com", "--token", "t"])
    assert r.exit_code == 2


def test_rotate_refuses_without_terminal_before_any_read(tmp_config_dir) -> None:
    with patch(SERVICE) as factory:
        r = _invoke(tmp_config_dir, ROTATE)
    assert r.exit_code == 6, r.output
    factory.assert_not_called()


def test_rotate_has_no_yes_flag(tmp_config_dir) -> None:
    r = _invoke(tmp_config_dir, [*ROTATE, "--yes"])
    assert r.exit_code == 2


def test_rotate_dry_run_prints_plan_without_token(tmp_config_dir) -> None:
    service = MagicMock()
    service.prepare_rotation.return_value = _plan()
    with patch(SERVICE, return_value=service):
        r = _invoke(tmp_config_dir, ["--json", *ROTATE, "--dry-run"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)["data"]
    assert data["status"] == "dry_run"
    assert [w["target"] for w in data["writes"]] == ["entra", "1password", "broker"]
    assert "123-secret-token" not in r.stdout
    service.apply_rotation.assert_not_called()


def test_rotate_wrong_code_writes_nothing(tmp_config_dir) -> None:
    service = MagicMock()
    service.prepare_rotation.return_value = _plan()
    with (
        patch(SERVICE, return_value=service),
        patch("keboola_agent_cli.commands._helpers.sys.stdin.isatty", return_value=True),
        patch("keboola_agent_cli.commands.oauth.assert_tty_for_write"),
    ):
        r = _invoke(tmp_config_dir, ROTATE, input="wrong-code\n")
    assert r.exit_code == 6, r.output
    service.apply_rotation.assert_not_called()


def test_rotate_right_code_applies(tmp_config_dir) -> None:
    service = MagicMock()
    service.prepare_rotation.return_value = _plan()
    service.apply_rotation.return_value = {
        "status": "rotated",
        "stack": "oauth.keboola.com",
        "component_id": "kds-team.wr-powerbi",
        "app_id": "app-1",
        "new_key_id": "new-key",
        "new_secret_expires": "2028-10-26",
        "op_vault": "vault",
        "op_item_id": "item-1",
        "broker_secret_changed": True,
        "broker_app_key_unchanged": True,
        "entra_secret_check": {"ok": True, "detail": "ok"},
        "old_secrets_still_valid": ["old-key"],
    }
    with (
        patch(SERVICE, return_value=service),
        patch("keboola_agent_cli.commands.oauth.assert_tty_for_write"),
        patch("keboola_agent_cli.commands.oauth.require_random_code_confirmation") as confirm,
    ):
        r = _invoke(tmp_config_dir, ["--json", *ROTATE])
    assert r.exit_code == 0, r.output
    confirm.assert_called_once()
    service.apply_rotation.assert_called_once()
    assert json.loads(r.stdout)["data"]["new_key_id"] == "new-key"


def test_rotate_unverified_exits_1(tmp_config_dir) -> None:
    service = MagicMock()
    service.prepare_rotation.return_value = _plan()
    service.apply_rotation.return_value = {
        **dict.fromkeys(
            [
                "stack",
                "component_id",
                "app_id",
                "new_key_id",
                "new_secret_expires",
                "op_vault",
                "op_item_id",
            ],
            "x",
        ),
        "status": "rotated_unverified",
        "broker_secret_changed": True,
        "broker_app_key_unchanged": True,
        "entra_secret_check": {"ok": False, "detail": "HTTP 401"},
        "old_secrets_still_valid": [],
    }
    with (
        patch(SERVICE, return_value=service),
        patch("keboola_agent_cli.commands.oauth.assert_tty_for_write"),
        patch("keboola_agent_cli.commands.oauth.require_random_code_confirmation"),
    ):
        r = _invoke(tmp_config_dir, ["--json", *ROTATE])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["exit_code"] == 1


def test_rotate_incomplete_reports_details(tmp_config_dir) -> None:
    service = MagicMock()
    service.prepare_rotation.return_value = _plan()
    service.apply_rotation.side_effect = KeboolaApiError(
        "broker failed",
        error_code=ErrorCode.OAUTH_ROTATION_INCOMPLETE,
        details={"stage": "broker", "op_item_id": "item-1"},
    )
    with (
        patch(SERVICE, return_value=service),
        patch("keboola_agent_cli.commands.oauth.assert_tty_for_write"),
        patch("keboola_agent_cli.commands.oauth.require_random_code_confirmation"),
    ):
        r = _invoke(tmp_config_dir, ["--json", *ROTATE])
    assert r.exit_code == 1
    error = json.loads(r.stdout)["error"]
    assert error["code"] == "OAUTH_ROTATION_INCOMPLETE"
    assert error["details"]["stage"] == "broker"


def test_consumer_get_json(tmp_config_dir) -> None:
    service = MagicMock()
    service.get_consumer.return_value = {
        "stack": "oauth.keboola.com",
        "consumer": {"componentId": "c", "appKey": "k", "appSecret": "<encrypted>"},
    }
    with patch(SERVICE, return_value=service):
        r = _invoke(
            tmp_config_dir,
            ["--json", "oauth", "consumer", "get", "--stack", "keboola.com", "--component", "c"],
        )
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["data"]["consumer"]["appSecret"] == "<encrypted>"


def test_identity_verify_too_broad_exits_6(tmp_config_dir) -> None:
    service = MagicMock()
    service.verify_identity.side_effect = KeboolaApiError(
        "too broad", error_code=ErrorCode.OAUTH_TOKEN_TOO_BROAD
    )
    with patch(SERVICE, return_value=service):
        r = _invoke(
            tmp_config_dir, ["--json", "oauth", "identity", "verify", "--stack", "keboola.com"]
        )
    assert r.exit_code == 6
    assert json.loads(r.stdout)["error"]["code"] == "OAUTH_TOKEN_TOO_BROAD"
