"""Tests for MemberService - project member & invitation lifecycle (since v0.26.1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.member_service import MemberService

STACK_URL = "https://connection.us-east4.gcp.keboola.com"
MANAGE_TOKEN = "manage-12345-abcdefghijklmnopqrstuvwxyz0123456789"
PROJECT_ID = 5725
ALIAS = "cuesta-master"


def _make_member(uid: int, email: str, role: str = "admin") -> dict:
    return {
        "id": uid,
        "email": email,
        "name": email.split("@")[0],
        "role": role,
        "status": "active",
        "mfaEnabled": False,
        "features": [],
    }


def _make_invitation(inv_id: int, email: str, role: str = "guest") -> dict:
    return {
        "id": inv_id,
        "created": "2026-05-01T19:04:35+0200",
        "expires": None,
        "reason": "",
        "role": role,
        "user": {"id": None, "email": email, "name": ""},
        "creator": {"id": 216, "email": "max.ottomansky@keboola.com", "name": "Max"},
    }


@pytest.fixture
def store_with_master(tmp_config_dir: Path) -> ConfigStore:
    """ConfigStore with the master cuesta project pre-registered."""
    store = ConfigStore(config_dir=tmp_config_dir)
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


@pytest.fixture
def manage_client_factory():
    """Factory returning a single shared MagicMock manage client."""
    mock = MagicMock()
    mock._stack_url = STACK_URL
    factory = MagicMock(return_value=mock)
    return factory, mock


# ──────────────────────────────────────────────────────────────────────
# invite (single)
# ──────────────────────────────────────────────────────────────────────


class TestInviteSingle:
    def test_happy_path(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.return_value = _make_invitation(
            1741, "ottomansky.max@gmail.com"
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email="ottomansky.max@gmail.com",
            role="guest",
            reason="hi",
        )

        assert result["status"] == "ok"
        assert result["invitation_id"] == 1741
        mock_client.create_project_invitation.assert_called_once_with(
            project_id=PROJECT_ID,
            email="ottomansky.max@gmail.com",
            role="guest",
            reason="hi",
        )
        mock_client.close.assert_called_once()

    def test_dry_run_makes_no_client_call(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email="x@y.com",
            role="admin",
            dry_run=True,
        )

        assert result["status"] == "dry_run"
        factory.assert_not_called()
        mock_client.create_project_invitation.assert_not_called()

    def test_already_invited_returns_noop(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.side_effect = KeboolaApiError(
            message="API error 400 from ...: This user has already been invited to this project.",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite(manage_token=MANAGE_TOKEN, alias=ALIAS, email="x@y.com", role="admin")

        assert result["status"] == "noop"
        assert result["note"] == "already_invited"
        mock_client.close.assert_called_once()

    def test_already_member_returns_noop(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.side_effect = KeboolaApiError(
            message="API error 400 from ...: This user is already a member of this project.",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite(manage_token=MANAGE_TOKEN, alias=ALIAS, email="x@y.com", role="admin")

        assert result["status"] == "noop"
        assert result["note"] == "already_member"

    def test_other_400_re_raises(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.side_effect = KeboolaApiError(
            message="API error 400 from ...: completely unrelated rejection",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(KeboolaApiError):
            svc.invite(manage_token=MANAGE_TOKEN, alias=ALIAS, email="x@y.com", role="admin")
        # close() must still fire even on raise
        mock_client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="not registered"):
            svc.invite(
                manage_token=MANAGE_TOKEN,
                alias="does-not-exist",
                email="x@y.com",
                role="admin",
            )

    def test_invalid_role_raises_value_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ValueError, match="Invalid role"):
            svc.invite(
                manage_token=MANAGE_TOKEN,
                alias=ALIAS,
                email="x@y.com",
                role="developer",  # not on the whitelist
            )


# ──────────────────────────────────────────────────────────────────────
# invite (bulk via --from-csv)
# ──────────────────────────────────────────────────────────────────────


def _write_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestInviteBulk:
    def test_partial_success(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, mock_client = manage_client_factory

        def _create_invitation(*, project_id, email, role, reason):
            if email == "fail@example.com":
                raise KeboolaApiError(
                    message="API error 400: nope",
                    status_code=400,
                    error_code=ErrorCode.API_ERROR,
                )
            if email == "dup@example.com":
                raise KeboolaApiError(
                    message="API error 400: This user has already been invited to this project.",
                    status_code=400,
                    error_code=ErrorCode.API_ERROR,
                )
            return _make_invitation(1700 + len(email), email, role)

        mock_client.create_project_invitation.side_effect = _create_invitation

        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project,role\n"
            "ok@example.com,cuesta-master,guest\n"
            "dup@example.com,cuesta-master,guest\n"
            "fail@example.com,cuesta-master,guest\n",
        )

        svc = MemberService(store_with_master, manage_client_factory=factory)
        result = svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, workers=1)

        assert result.total == 3
        assert result.succeeded == 1
        assert result.noop == 1
        assert result.failed == 1
        assert {r.email for r in result.rows} == {
            "ok@example.com",
            "dup@example.com",
            "fail@example.com",
        }

    def test_dry_run_makes_no_client_call(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, mock_client = manage_client_factory
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project,role\nok@example.com,cuesta-master,guest\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, dry_run=True)

        assert result.dry_run is True
        assert result.total == 1
        assert result.succeeded == 1
        factory.assert_not_called()
        mock_client.create_project_invitation.assert_not_called()

    def test_default_role_fills_missing_column(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.return_value = _make_invitation(1, "x@y.com", "admin")
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project\nx@y.com,cuesta-master\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite_bulk(
            manage_token=MANAGE_TOKEN,
            csv_path=csv_path,
            default_role="admin",
            workers=1,
        )

        assert result.succeeded == 1
        assert result.rows[0].role == "admin"

    def test_no_role_column_no_default_role_raises(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, _ = manage_client_factory
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project\nx@y.com,cuesta-master\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="role"):
            svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path)

    def test_missing_email_column_raises(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, _ = manage_client_factory
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "user,project,role\nx@y.com,cuesta-master,admin\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="missing an 'email' column"):
            svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path)

    def test_missing_file_raises(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, _ = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="not found"):
            svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=tmp_path / "missing.csv")

    def test_unknown_alias_in_csv_row_is_per_row_failure_not_global_abort(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        """One bad row never aborts the rest -- mirror OrgService.refresh_tokens.

        Regression: pre-fix, the upfront `_stack_for_row` set comprehension
        would raise ConfigError on the first unregistered alias and the entire
        bulk batch would abort. Now the bad row appears as `status="failed"`
        and the remaining rows still execute.
        """
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.return_value = _make_invitation(42, "ok@example.com")
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project,role\n"
            "ok@example.com,cuesta-master,guest\n"
            "bad@example.com,unknown-alias,guest\n",
        )

        svc = MemberService(store_with_master, manage_client_factory=factory)
        result = svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, workers=1)

        assert result.total == 2
        assert result.succeeded == 1
        assert result.failed == 1
        by_email = {r.email: r for r in result.rows}
        assert by_email["ok@example.com"].status == "ok"
        assert by_email["bad@example.com"].status == "failed"
        assert "unknown-alias" in by_email["bad@example.com"].note
        # The good row still hit the API
        mock_client.create_project_invitation.assert_called_once()

    def test_numeric_project_id_resolves(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.return_value = _make_invitation(1, "x@y.com", "guest")
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            f"email,project_id,role\nx@y.com,{PROJECT_ID},guest\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, workers=1)
        assert result.succeeded == 1
        assert result.rows[0].project_id == PROJECT_ID


# ──────────────────────────────────────────────────────────────────────
# member-list, invitation-list, invitation-cancel
# ──────────────────────────────────────────────────────────────────────


class TestBulkRegressions:
    """Iteration-2 reviewer findings encoded as regression tests."""

    def test_dry_run_rejects_multi_stack_csv(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        """Dry-run preview must enforce the same single-stack invariant the
        live path enforces -- otherwise users get a 'preview said ok' surprise
        on the real run."""
        factory, _ = manage_client_factory
        store_with_master.add_project(
            "other-stack",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-fake-other-stack-token",
                project_id=1,
                project_name="Other",
            ),
        )
        csv_path = _write_csv(
            tmp_path / "bulk.csv",
            "email,project,role\na@b.com,cuesta-master,guest\nc@d.com,other-stack,guest\n",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(ConfigError, match="multiple stack URLs"):
            svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, dry_run=True)

    def test_csv_with_utf8_bom_parses(
        self,
        tmp_path: Path,
        store_with_master: ConfigStore,
        manage_client_factory,
    ) -> None:
        """Excel-exported CSVs prepend a UTF-8 BOM; the parser must strip it
        so the first header reads as 'email', not '﻿email'."""
        factory, mock_client = manage_client_factory
        mock_client.create_project_invitation.return_value = _make_invitation(1, "x@y.com")
        csv_path = tmp_path / "bom.csv"
        # ﻿ = UTF-8 BOM
        csv_path.write_text(
            "﻿email,project,role\nx@y.com,cuesta-master,guest\n",
            encoding="utf-8",
        )
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.invite_bulk(manage_token=MANAGE_TOKEN, csv_path=csv_path, workers=1)
        assert result.succeeded == 1


class TestListMembers:
    def test_active_only(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_members.return_value = [
            _make_member(216, "max.ottomansky@keboola.com", "admin"),
        ]
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.list_members(manage_token=MANAGE_TOKEN, alias=ALIAS)

        assert result["alias"] == ALIAS
        assert result["project_id"] == PROJECT_ID
        assert result["members"][0]["email"] == "max.ottomansky@keboola.com"
        assert "pending_invitations" not in result
        mock_client.list_project_invitations.assert_not_called()

    def test_include_pending(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_members.return_value = [
            _make_member(216, "max.ottomansky@keboola.com")
        ]
        mock_client.list_project_invitations.return_value = [
            _make_invitation(1515, "marcusscwong@gmail.com", "admin")
        ]
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.list_members(manage_token=MANAGE_TOKEN, alias=ALIAS, include_pending=True)

        assert len(result["pending_invitations"]) == 1
        assert result["pending_invitations"][0]["user"]["email"] == "marcusscwong@gmail.com"


class TestCancelInvitation:
    def test_resolves_id_from_email(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_invitations.return_value = [
            _make_invitation(1515, "marcusscwong@gmail.com")
        ]
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.cancel_invitation(
            manage_token=MANAGE_TOKEN, alias=ALIAS, email="marcusscwong@gmail.com"
        )

        assert result["invitation_id"] == 1515
        mock_client.cancel_project_invitation.assert_called_once_with(PROJECT_ID, 1515)

    def test_explicit_id_skips_lookup(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.cancel_invitation(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email="x@y.com",
            invitation_id=9999,
        )

        assert result["invitation_id"] == 9999
        mock_client.list_project_invitations.assert_not_called()
        mock_client.cancel_project_invitation.assert_called_once_with(PROJECT_ID, 9999)

    def test_email_not_found_raises_404(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_invitations.return_value = [
            _make_invitation(1, "someone-else@example.com")
        ]
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(KeboolaApiError) as exc_info:
            svc.cancel_invitation(
                manage_token=MANAGE_TOKEN, alias=ALIAS, email="missing@example.com"
            )
        assert exc_info.value.status_code == 404


class TestRemoveMember:
    def test_resolves_user_id_from_email(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_members.return_value = [
            _make_member(4241, "mfiser@cuestapartners.com", "admin")
        ]
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.remove_member(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email="MFiser@CuestaPartners.com",  # case-insensitive
        )

        assert result["user_id"] == 4241
        mock_client.remove_project_member.assert_called_once_with(PROJECT_ID, 4241)

    def test_email_not_found_raises_404(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_members.return_value = []
        svc = MemberService(store_with_master, manage_client_factory=factory)

        with pytest.raises(KeboolaApiError) as exc_info:
            svc.remove_member(manage_token=MANAGE_TOKEN, alias=ALIAS, email="ghost@example.com")
        assert exc_info.value.status_code == 404
        mock_client.remove_project_member.assert_not_called()


class TestSetMemberRole:
    def test_propagates_role_via_patch(self, store_with_master, manage_client_factory) -> None:
        factory, mock_client = manage_client_factory
        mock_client.list_project_members.return_value = [
            _make_member(216, "max.ottomansky@keboola.com", "admin")
        ]
        mock_client.update_project_member_role.return_value = {
            "id": 216,
            "email": "max.ottomansky@keboola.com",
            "role": "guest",
        }
        svc = MemberService(store_with_master, manage_client_factory=factory)

        result = svc.set_member_role(
            manage_token=MANAGE_TOKEN,
            alias=ALIAS,
            email="max.ottomansky@keboola.com",
            role="guest",
        )

        assert result["role"] == "guest"
        mock_client.update_project_member_role.assert_called_once_with(PROJECT_ID, 216, "guest")

    def test_invalid_role_raises_value_error(
        self, store_with_master, manage_client_factory
    ) -> None:
        factory, _ = manage_client_factory
        svc = MemberService(store_with_master, manage_client_factory=factory)
        with pytest.raises(ValueError, match="Invalid role"):
            svc.set_member_role(
                manage_token=MANAGE_TOKEN,
                alias=ALIAS,
                email="x@y.com",
                role="developer",
            )
