"""Service-layer tests for ``PolicyBindingService`` (``rls bind|unbind|bindings|bindings-sweep``).

The metastore and Storage clients are ``MagicMock``s: the tests check the KID, the validation
order, the upsert (POST vs PUT), and every sweep condition, without HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.policy_binding_service import (
    BINDING_TYPE,
    KidKind,
    PolicyBindingService,
    parse_kid,
    workspace_kid,
)

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK = "dev-keboola-aws-eu-west-1"
PROJECT_ID = 5725
KID = f"KID:{STACK}:project-{PROJECT_ID}:workspace-42"
RLS_ID = "11111111-1111-4111-8111-111111111111"
RLS_ID_2 = "22222222-2222-4222-8222-222222222222"
CLS_ID = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 10, 21, tzinfo=UTC)
OLD = "2026-08-01T00:00:00Z"
RECENT = "2026-10-20T00:00:00Z"


def _api_error(status: int, code: ErrorCode = ErrorCode.API_ERROR) -> KeboolaApiError:
    return KeboolaApiError(message=f"HTTP {status}", status_code=status, error_code=code)


def _ctx(mock: MagicMock) -> MagicMock:
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _binding(
    kid: str = KID,
    item_id: str = "b-1",
    rls: list[str] | None = None,
    cls: list[str] | None = None,
    updated: str | None = OLD,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {"kid": kid}
    if rls:
        attrs["rlsPolicyUUIDs"] = rls
    if cls:
        attrs["clsPolicyUUIDs"] = cls
    return {
        "type": BINDING_TYPE,
        "id": item_id,
        "attributes": attrs,
        "meta": {"name": kid, "lastUpdated": updated},
    }


def _make(
    tmp_path: Path,
    *,
    bindings: list[dict[str, Any]] | None = None,
    workspace: dict[str, Any] | Exception | None = None,
    project_id: int | None = PROJECT_ID,
) -> tuple[PolicyBindingService, MagicMock, MagicMock]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=project_id,
        ),
    )
    metastore = _ctx(MagicMock())
    metastore.list_items.return_value = bindings or []
    metastore.post_item.return_value = {"id": "b-new"}
    metastore.put_item.side_effect = lambda _t, item_id, **_kw: {"id": item_id}
    storage = _ctx(MagicMock())
    if isinstance(workspace, Exception):
        storage.get_workspace.side_effect = workspace
    else:
        storage.get_workspace.return_value = workspace or {
            "id": 42,
            "connection": {"loginType": "governed"},
        }
    storage.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
    storage.verify_token.return_value.project_id = PROJECT_ID
    service = PolicyBindingService(
        config_store=store,
        client_factory=lambda url, token: storage,
        metastore_client_factory=lambda url, token: metastore,
        now=lambda: NOW,
    )
    return service, metastore, storage


# ---------------------------------------------------------------------------
# KID
# ---------------------------------------------------------------------------


class TestKid:
    def test_workspace_kid_is_canonical(self) -> None:
        assert workspace_kid(STACK, PROJECT_ID, 42) == KID

    @pytest.mark.parametrize("stack", [None, "", "Dev_Stack", "-dev", "dev--stack", "dev stack"])
    def test_missing_or_malformed_stack_id_is_a_usage_error(self, stack: str | None) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            workspace_kid(stack, PROJECT_ID, 42)
        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "dev-keboola-aws-eu-west-1" in exc_info.value.message

    @pytest.mark.parametrize(
        ("kid", "kind", "project", "workspace"),
        [
            (KID, KidKind.WORKSPACE, PROJECT_ID, 42),
            (f"KID:{STACK}:project-1:app-abc", KidKind.APP, None, None),
            (f"KID:{STACK}:project-1:workspace-01", KidKind.OTHER, None, None),
            (f"KID:{STACK}:project-1:branch-2:workspace-3", KidKind.OTHER, None, None),
            ("not-a-kid", KidKind.OTHER, None, None),
        ],
    )
    def test_parse_kid(
        self, kid: str, kind: KidKind, project: int | None, workspace: int | None
    ) -> None:
        parsed = parse_kid(kid)
        assert (parsed.kind, parsed.project_id, parsed.workspace_id) == (kind, project, workspace)


# ---------------------------------------------------------------------------
# bind
# ---------------------------------------------------------------------------


class TestBind:
    def test_creates_when_unbound(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path)

        result = service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID])

        metastore.post_item.assert_called_once_with(
            BINDING_TYPE, name=KID, data={"kid": KID, "rlsPolicyUUIDs": [RLS_ID]}
        )
        metastore.put_item.assert_not_called()
        assert result["action"] == "created"
        assert result["kid"] == KID
        assert result["binding_id"] == "b-new"
        assert "warnings" not in result

    def test_replaces_both_lists_when_bound(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding(rls=[RLS_ID], cls=[CLS_ID])])

        result = service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID_2])

        # PUT, not PATCH: the old clsPolicyUUIDs must not survive a rebind without --cls-policy.
        metastore.put_item.assert_called_once_with(
            BINDING_TYPE, "b-1", name=KID, data={"kid": KID, "rlsPolicyUUIDs": [RLS_ID_2]}
        )
        metastore.post_item.assert_not_called()
        assert result["action"] == "replaced"
        assert result["previous"] == {"rls_policy_ids": [RLS_ID], "cls_policy_ids": [CLS_ID]}

    def test_post_that_lost_a_race_is_put_once(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path)
        metastore.post_item.side_effect = _api_error(409, ErrorCode.ALREADY_EXISTS)
        metastore.list_items.side_effect = [[], [_binding()]]

        result = service.bind("prod", 42, stack_id=STACK, cls_policies=[CLS_ID])

        assert result["action"] == "replaced"
        metastore.put_item.assert_called_once()

    def test_dry_run_validates_and_writes_nothing(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding(rls=[RLS_ID])])

        result = service.bind("prod", 42, stack_id=STACK, cls_policies=[CLS_ID], dry_run=True)

        assert result["dry_run"] is True
        assert result["action"] == "replace"
        assert result["binding_id"] == "b-1"
        metastore.get_item.assert_called_once_with("cls-policy", CLS_ID)
        metastore.post_item.assert_not_called()
        metastore.put_item.assert_not_called()

    def test_ids_are_deduplicated_and_normalized(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path)

        service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID, RLS_ID.upper(), RLS_ID])

        assert metastore.post_item.call_args.kwargs["data"]["rlsPolicyUUIDs"] == [RLS_ID]

    @pytest.mark.parametrize(
        ("rls", "cls", "needle"),
        [
            ([], [], "at least one"),
            (["not-a-uuid"], [], "not a UUID"),
            ([f"{i:08d}-0000-4000-8000-000000000000" for i in range(101)], [], "at most 100"),
        ],
    )
    def test_usage_errors_before_any_call(
        self, tmp_path: Path, rls: list[str], cls: list[str], needle: str
    ) -> None:
        service, metastore, storage = _make(tmp_path)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.bind("prod", 42, stack_id=STACK, rls_policies=rls, cls_policies=cls)

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert needle in exc_info.value.message
        storage.get_workspace.assert_not_called()
        metastore.list_items.assert_not_called()

    def test_missing_stack_id_is_a_usage_error(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.bind("prod", 42, stack_id=None, rls_policies=[RLS_ID])

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        metastore.post_item.assert_not_called()

    def test_unknown_policy_fails_without_writing(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path)
        metastore.get_item.side_effect = [{"id": RLS_ID}, _api_error(404, ErrorCode.NOT_FOUND)]

        with pytest.raises(KeboolaApiError) as exc_info:
            service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID], cls_policies=[CLS_ID])

        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        assert f"cls-policy {CLS_ID}" in exc_info.value.message
        metastore.post_item.assert_not_called()

    def test_missing_workspace_fails(self, tmp_path: Path) -> None:
        service, metastore, storage = _make(
            tmp_path, workspace=_api_error(404, ErrorCode.NOT_FOUND)
        )
        storage.list_dev_branches.return_value = [{"id": 1, "isDefault": True}, {"id": 7}]

        with pytest.raises(KeboolaApiError) as exc_info:
            service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID])

        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        storage.get_workspace.assert_any_call(42, branch_id=7)
        metastore.post_item.assert_not_called()

    def test_workspace_found_in_a_dev_branch(self, tmp_path: Path) -> None:
        service, _, storage = _make(tmp_path)
        storage.get_workspace.side_effect = [
            _api_error(404, ErrorCode.NOT_FOUND),
            {"id": 42, "connection": {"loginType": "governed"}},
        ]
        storage.list_dev_branches.return_value = [{"id": 7}]

        result = service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID])

        assert result["action"] == "created"

    def test_non_governed_workspace_warns_but_binds(self, tmp_path: Path) -> None:
        service, _, _ = _make(
            tmp_path, workspace={"id": 42, "connection": {"loginType": "snowflake-person-keypair"}}
        )

        result = service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID])

        assert result["action"] == "created"
        assert "not 'governed'" in result["warnings"][0]

    def test_project_id_comes_from_the_token_when_not_stored(self, tmp_path: Path) -> None:
        service, _, storage = _make(tmp_path, project_id=None)

        result = service.bind("prod", 42, stack_id=STACK, rls_policies=[RLS_ID])

        storage.verify_token.assert_called_once()
        assert result["kid"] == KID


# ---------------------------------------------------------------------------
# unbind
# ---------------------------------------------------------------------------


class TestUnbind:
    def test_deletes_the_binding(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding(rls=[RLS_ID])])

        result = service.unbind("prod", 42, stack_id=STACK)

        metastore.delete_item.assert_called_once_with(BINDING_TYPE, "b-1")
        assert result["deleted"] is True
        assert result["kid"] == KID

    def test_dry_run_only_looks_it_up(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding(rls=[RLS_ID])])

        result = service.unbind("prod", 42, stack_id=STACK, dry_run=True)

        assert result["bound"] is True
        assert result["dry_run"] is True
        metastore.delete_item.assert_not_called()

    def test_no_binding_is_not_an_error(self, tmp_path: Path) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding(kid=KID + "1")])

        result = service.unbind("prod", 42, stack_id=STACK)

        assert result == {
            "project": "prod",
            "workspace_id": 42,
            "kid": KID,
            "bound": False,
            "deleted": False,
        }
        metastore.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------


class TestListBindings:
    def test_rows_with_workspace_existence(self, tmp_path: Path) -> None:
        gone = _binding(kid=f"KID:{STACK}:project-{PROJECT_ID}:workspace-9", item_id="b-2")
        app = _binding(kid=f"KID:{STACK}:project-{PROJECT_ID}:app-x", item_id="b-3")
        service, _, storage = _make(tmp_path, bindings=[_binding(rls=[RLS_ID]), gone, app])
        storage.get_workspace.side_effect = lambda ws, branch_id=None: (
            {"id": ws} if ws == 42 else (_ for _ in ()).throw(_api_error(404))
        )

        rows = service.list_bindings("prod")["bindings"]

        assert [(r["id"], r["workspace_exists"]) for r in rows] == [
            ("b-1", True),
            ("b-2", False),
            ("b-3", None),
        ]
        assert rows[0]["rls_policy_ids"] == [RLS_ID]
        assert rows[0]["updated"] == OLD

    def test_kid_of_another_project_is_not_checked(self, tmp_path: Path) -> None:
        other = _binding(kid=f"KID:{STACK}:project-999:workspace-42")
        service, _, storage = _make(tmp_path, bindings=[other])

        row = service.list_bindings("prod")["bindings"][0]

        assert row["workspace_exists"] is None
        assert "999" in row["workspace_check_error"]
        storage.get_workspace.assert_not_called()

    def test_filter_and_failed_check(self, tmp_path: Path) -> None:
        other = _binding(kid=f"KID:{STACK}:project-{PROJECT_ID}:workspace-9", item_id="b-2")
        service, _, _ = _make(tmp_path, bindings=[_binding(), other], workspace=_api_error(500))

        rows = service.list_bindings("prod", workspace_id=42)["bindings"]

        assert [r["id"] for r in rows] == ["b-1"]
        assert rows[0]["workspace_exists"] is None
        assert rows[0]["workspace_check_error"] == "HTTP 500"


# ---------------------------------------------------------------------------
# bindings-sweep
# ---------------------------------------------------------------------------


def _ws_kid(workspace_id: int, project_id: int = PROJECT_ID) -> str:
    return f"KID:{STACK}:project-{project_id}:workspace-{workspace_id}"


class TestSweep:
    @pytest.mark.parametrize(
        ("binding", "workspace", "status", "reason"),
        [
            (_binding(updated=OLD), _api_error(404), "orphaned", "no longer exists"),
            (_binding(updated=RECENT), _api_error(404), "kept", "within --older-than"),
            (_binding(updated=OLD), {"id": 42}, "kept", "workspace exists"),
            (_binding(updated=OLD), _api_error(500), "failed", "workspace check failed"),
            (_binding(updated=OLD), _api_error(403), "failed", "workspace check failed"),
            (_binding(updated=None), _api_error(404), "kept", "no last-update"),
            (_binding(updated="yesterday"), _api_error(404), "kept", "no last-update"),
            (
                _binding(kid=f"KID:{STACK}:project-{PROJECT_ID}:app-7", updated=OLD),
                _api_error(404),
                "kept",
                "app KID",
            ),
            (_binding(kid=_ws_kid(42, 999), updated=OLD), _api_error(404), "kept", "999"),
            (_binding(kid="KID:x:workspace-01", updated=OLD), _api_error(404), "kept", "canonical"),
        ],
    )
    def test_conditions(
        self,
        tmp_path: Path,
        binding: dict[str, Any],
        workspace: Any,
        status: str,
        reason: str,
    ) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[binding], workspace=workspace)

        result = service.sweep("prod", older_than_days=30, delete=False)

        row = result["bindings"][0]
        assert row["status"] == status
        assert reason in row["reason"]
        assert result["dry_run"] is True
        assert result["references_checked"] is False
        metastore.delete_item.assert_not_called()

    def test_gone_only_when_every_dev_branch_also_404s(self, tmp_path: Path) -> None:
        service, _, storage = _make(tmp_path, bindings=[_binding()])
        storage.get_workspace.side_effect = [_api_error(404), {"id": 42}]
        storage.list_dev_branches.return_value = [{"id": 1, "isDefault": True}, {"id": 7}]

        row = service.sweep("prod", older_than_days=30, delete=False)["bindings"][0]

        assert row["status"] == "kept"

    def test_failed_branch_listing_is_never_a_delete(self, tmp_path: Path) -> None:
        service, metastore, storage = _make(
            tmp_path, bindings=[_binding()], workspace=_api_error(404)
        )
        storage.list_dev_branches.side_effect = _api_error(500)

        result = service.sweep("prod", older_than_days=30, delete=True)

        assert result["bindings"][0]["status"] == "failed"
        metastore.delete_item.assert_not_called()

    def test_delete_removes_only_orphans(self, tmp_path: Path) -> None:
        bindings = [
            _binding(kid=_ws_kid(1), item_id="gone"),
            _binding(kid=_ws_kid(2), item_id="young", updated=RECENT),
            _binding(kid=f"KID:{STACK}:project-{PROJECT_ID}:app-3", item_id="app"),
        ]
        service, metastore, _ = _make(tmp_path, bindings=bindings, workspace=_api_error(404))

        result = service.sweep("prod", older_than_days=30, delete=True)

        metastore.delete_item.assert_called_once_with(BINDING_TYPE, "gone")
        assert result["summary"] == {"orphaned": 0, "deleted": 1, "kept": 2, "failed": 0}

    @pytest.mark.parametrize(("status", "outcome"), [(409, "kept"), (500, "failed")])
    def test_refused_delete(self, tmp_path: Path, status: int, outcome: str) -> None:
        service, metastore, _ = _make(tmp_path, bindings=[_binding()], workspace=_api_error(404))
        metastore.delete_item.side_effect = _api_error(status)

        row = service.sweep("prod", older_than_days=30, delete=True)["bindings"][0]

        assert row["status"] == outcome

    def test_older_than_zero_takes_every_age(self, tmp_path: Path) -> None:
        service, _, _ = _make(
            tmp_path, bindings=[_binding(updated=RECENT)], workspace=_api_error(404)
        )

        row = service.sweep("prod", older_than_days=0, delete=False)["bindings"][0]

        assert row["status"] == "orphaned"

    def test_negative_age_is_a_usage_error(self, tmp_path: Path) -> None:
        service, _, _ = _make(tmp_path)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.sweep("prod", older_than_days=-1, delete=False)

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
