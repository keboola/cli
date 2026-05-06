"""CLI tests for `kbagent project invite / member-* / invitation-*` (since v0.26.1)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import BulkInviteResult, MemberInviteRow, ProjectConfig

STACK_URL = "https://connection.us-east4.gcp.keboola.com"
PROJECT_ID = 5725
ALIAS = "cuesta-master"
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


class TestProjectInviteSingle:
    def test_json_happy_path(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)

        svc = MagicMock()
        svc.invite.return_value = {
            "status": "ok",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "email": "ottomansky.max@gmail.com",
            "role": "guest",
            "invitation_id": 1741,
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "ottomansky.max@gmail.com",
                    "--role",
                    "guest",
                ],
            )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert out["data"]["invitation_id"] == 1741
        svc.invite.assert_called_once()

    def test_missing_required_args_exits_2(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    # --email + --role missing
                ],
            )
        assert result.exit_code == 2

    def test_invalid_role_blocked_by_choice(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "developer",  # not on whitelist -> Click rejects with exit 2
                ],
            )
        assert result.exit_code == 2

    def test_dry_run_short_circuits(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.invite.return_value = {
            "status": "dry_run",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "email": "x@y.com",
            "role": "guest",
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "guest",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0
        assert json.loads(result.output)["data"]["status"] == "dry_run"

    def test_invalid_token_maps_to_exit_3(self, tmp_path: Path) -> None:
        """`map_error_to_exit_code` exclusively maps INVALID_TOKEN -> 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.invite.side_effect = KeboolaApiError(
            message="Invalid or expired token",
            status_code=401,
            error_code=ErrorCode.INVALID_TOKEN,
        )

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "admin",
                ],
            )
        assert result.exit_code == 3


class TestProjectInviteBulk:
    def test_json_bulk_summary(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.invite_bulk.return_value = BulkInviteResult(
            total=2,
            succeeded=1,
            noop=1,
            failed=0,
            rows=[
                MemberInviteRow(
                    email="a@b.com",
                    project=ALIAS,
                    project_id=PROJECT_ID,
                    role="guest",
                    status="ok",
                    invitation_id=1,
                ),
                MemberInviteRow(
                    email="c@d.com",
                    project=ALIAS,
                    project_id=PROJECT_ID,
                    role="guest",
                    status="noop",
                    note="already_invited",
                ),
            ],
        )
        csv_path = tmp_path / "bulk.csv"
        csv_path.write_text(
            "email,project,role\na@b.com,cuesta-master,guest\nc@d.com,cuesta-master,guest\n"
        )

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--from-csv",
                    str(csv_path),
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "ok"
        assert out["data"]["total"] == 2
        assert out["data"]["succeeded"] == 1
        assert out["data"]["noop"] == 1

    def test_from_csv_mutually_exclusive_with_project(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        csv_path = tmp_path / "bulk.csv"
        csv_path.write_text("email,project,role\na@b.com,cuesta-master,guest\n")

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invite",
                    "--from-csv",
                    str(csv_path),
                    "--project",
                    ALIAS,
                ],
            )
        assert result.exit_code == 2


class TestMemberList:
    def test_json_output(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_members.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "members": [
                {
                    "id": 216,
                    "email": "max.ottomansky@keboola.com",
                    "name": "Max",
                    "role": "admin",
                    "status": "active",
                    "mfa_enabled": True,
                }
            ],
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "member-list",
                    "--project",
                    ALIAS,
                ],
            )
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["data"]["members"][0]["role"] == "admin"
        svc.list_members.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, include_pending=False
        )

    def test_include_pending_flag_propagates(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.list_members.return_value = {
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "members": [],
            "pending_invitations": [],
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "member-list",
                    "--project",
                    ALIAS,
                    "--include-pending",
                ],
            )
        assert result.exit_code == 0
        svc.list_members.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, include_pending=True
        )


class TestInvitationCancel:
    def test_yes_skips_confirmation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.cancel_invitation.return_value = {
            "status": "cancelled",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "email": "x@y.com",
            "invitation_id": 99,
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "invitation-cancel",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        svc.cancel_invitation.assert_called_once()


class TestMemberRemove:
    def test_destructive_yes(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.remove_member.return_value = {
            "status": "removed",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "email": "ghost@example.com",
            "user_id": 999,
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "member-remove",
                    "--project",
                    ALIAS,
                    "--email",
                    "ghost@example.com",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        svc.remove_member.assert_called_once()


class TestMemberSetRole:
    def test_propagates_role(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        svc = MagicMock()
        svc.set_member_role.return_value = {
            "status": "updated",
            "alias": ALIAS,
            "project_id": PROJECT_ID,
            "email": "x@y.com",
            "user_id": 216,
            "role": "guest",
        }

        with (
            patch("keboola_agent_cli.cli.MemberService", return_value=svc),
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}),
        ):
            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "project",
                    "member-set-role",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "guest",
                ],
            )
        assert result.exit_code == 0, result.output
        svc.set_member_role.assert_called_once_with(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email="x@y.com", role="guest"
        )


class TestRegressions:
    """Iteration-2 reviewer findings encoded as CLI regression tests."""

    def test_hint_with_from_csv_emits_clear_error_not_silent_skip(self, tmp_path: Path) -> None:
        """Pre-fix: --hint + --from-csv silently fell through to the live
        path and prompted for the manage token. Now it exits 2 with a usage
        error explaining hints are single-shot only."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)
        csv_path = tmp_path / "bulk.csv"
        csv_path.write_text("email,project,role\na@b.com,cuesta-master,guest\n")

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "--hint",
                    "client",
                    "project",
                    "invite",
                    "--from-csv",
                    str(csv_path),
                ],
            )
        assert result.exit_code == 2, result.output
        # The error envelope is JSON; check the message content.
        out = json.loads(result.output)
        assert "from-csv" in out["error"]["message"].lower()


class TestHints:
    def test_invite_hint_client_renders(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--hint",
                    "client",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "admin",
                ],
            )
        assert result.exit_code == 0
        assert "ManageClient" in result.output
        assert "create_project_invitation" in result.output

    def test_invite_hint_service_renders(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _seed_store(config_dir)

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": MANAGE_TOKEN}):
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--hint",
                    "service",
                    "project",
                    "invite",
                    "--project",
                    ALIAS,
                    "--email",
                    "x@y.com",
                    "--role",
                    "admin",
                ],
            )
        assert result.exit_code == 0
        assert "MemberService" in result.output
        assert "invite" in result.output
