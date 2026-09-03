"""Tests for the `/auth/*` REST router (Task 2 of issue #537).

Covers three things:

1. Router -> service kwarg parity (the pattern from
   ``tests/test_server_router_calls.py``): each endpoint must call the
   corresponding ``AuthService`` method with the exact keyword arguments the
   brief specifies -- never a positional call, never a renamed kwarg, and
   never the CLI-only ``selections`` parameter.
2. Central error translation: a ``KeboolaApiError`` carrying
   ``SESSION_NOT_FOUND`` must answer HTTP 401 (via ``_SESSION_CREDENTIAL_CODES``
   in ``server/app.py``), and a ``ConfigError`` (the service's own guard
   against zero/two selectors) must answer a 4xx, never a 500.
3. The permission seam from Task 1: ``POST /auth/register-projects`` is a
   write operation and must be blocked by a deny-writes policy while the two
   GET endpoints stay open, and no route exists at all for
   ``/auth/login`` / ``/auth/login-password`` / ``/auth/logout``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.permissions import PermissionEngine
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry
from keboola_agent_cli.services._auth_registration import SESSION_UNSUPPORTED_FEATURES
from keboola_agent_cli.services.auth_service import (
    AuthStatusResult,
    ProjectCandidate,
    ProjectCandidatesResult,
    RegisteredProject,
    RegisterProjectsResult,
)

AUTH = {"Authorization": "Bearer test-token"}
STACK = "https://connection.keboola.com"


def _mock_registry(**services: Any) -> ServiceRegistry:
    """Bare ServiceRegistry with only the given service mocks attached.

    Matches the established pattern in test_server_router_calls.py. The
    permission engine is deliberately NOT here: it lives on ``app.state``, so
    overriding ``get_registry`` with this stub leaves enforcement intact.
    """
    registry = ServiceRegistry.__new__(ServiceRegistry)
    for name, mock in services.items():
        setattr(registry, name, mock)
    return registry


def _make_app_with_registry(tmp_path: Path, registry: ServiceRegistry) -> Any:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return app


def _status_result(**overrides: Any) -> AuthStatusResult:
    base: dict[str, Any] = {
        "status": "live",
        "stack_url": STACK,
        "session_id": "sess-1",
        "user_email": "user@example.com",
        "user_name": "Test User",
        "access_expires_at": "2026-08-24T00:00:00+00:00",
        "refresh_expires_at": "2026-09-23T00:00:00+00:00",
        "accessible_projects": [{"id": 123, "name": "Prod", "role": "admin"}],
        "orphaned_session_ids": [],
        "detail": "",
    }
    base.update(overrides)
    return AuthStatusResult(**base)


def _candidates_result(**overrides: Any) -> ProjectCandidatesResult:
    base: dict[str, Any] = {
        "stack_url": STACK,
        "candidates": [
            ProjectCandidate(
                project_id=123,
                project_name="Prod",
                role="admin",
                default_alias="prod-123",
                existing_alias="",
                registered=False,
            )
        ],
    }
    base.update(overrides)
    return ProjectCandidatesResult(**base)


def _register_result(**overrides: Any) -> RegisterProjectsResult:
    base: dict[str, Any] = {
        "status": "ok",
        "stack_url": STACK,
        "registered_projects": [
            RegisteredProject(
                alias="prod-123",
                project_id=123,
                project_name="Prod",
                status="registered",
            )
        ],
        "warnings": [],
    }
    base.update(overrides)
    return RegisterProjectsResult(**base)


# ---------------------------------------------------------------------------
# 1. kwarg parity
# ---------------------------------------------------------------------------


class TestKwargParity:
    def test_list_project_candidates_passes_stack_kwarg(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.list_project_candidates.return_value = _candidates_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.get("/auth/projects", headers=AUTH, params={"stack": STACK})

        assert resp.status_code == 200, resp.text
        auth_svc.list_project_candidates.assert_called_once_with(stack=STACK)

    def test_list_project_candidates_defaults_stack_to_none(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.list_project_candidates.return_value = _candidates_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.get("/auth/projects", headers=AUTH)

        assert resp.status_code == 200, resp.text
        auth_svc.list_project_candidates.assert_called_once_with(stack=None)

    def test_status_passes_stack_kwarg(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.status.return_value = _status_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.get("/auth/status", headers=AUTH, params={"stack": STACK})

        assert resp.status_code == 200, resp.text
        auth_svc.status.assert_called_once_with(stack=STACK)

    def test_register_projects_passes_exact_kwargs(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post(
                "/auth/register-projects",
                headers=AUTH,
                json={"stack": STACK, "project_ids": [123]},
            )

        assert resp.status_code == 200, resp.text
        kwargs = auth_svc.register_projects.call_args.kwargs
        assert kwargs == {
            "stack": STACK,
            "select_all": False,
            "project_ids": [123],
            "alias_overrides": None,
        }
        # The CLI picker's own parameter must never be reachable from REST.
        assert "selections" not in kwargs


# ---------------------------------------------------------------------------
# 2. request-body shape: `all` alias, project_ids + aliases int coercion
# ---------------------------------------------------------------------------


class TestRegisterProjectsBody:
    def test_all_alias_sets_select_all(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post("/auth/register-projects", headers=AUTH, json={"all": True})

        assert resp.status_code == 200, resp.text
        kwargs = auth_svc.register_projects.call_args.kwargs
        assert kwargs["select_all"] is True
        assert kwargs["project_ids"] is None

    def test_select_all_field_name_also_works(self, tmp_path: Path) -> None:
        # populate_by_name=True: the Python field name is accepted too, not
        # only the "all" alias.
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post("/auth/register-projects", headers=AUTH, json={"select_all": True})

        assert resp.status_code == 200, resp.text
        assert auth_svc.register_projects.call_args.kwargs["select_all"] is True

    def test_project_ids_and_aliases_pass_through_with_int_keys(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post(
                "/auth/register-projects",
                headers=AUTH,
                json={
                    "project_ids": [123, 456],
                    "aliases": {"123": "prod", "456": "stage"},
                },
            )

        assert resp.status_code == 200, resp.text
        kwargs = auth_svc.register_projects.call_args.kwargs
        assert kwargs["project_ids"] == [123, 456]
        assert kwargs["alias_overrides"] == {123: "prod", 456: "stage"}
        # JSON object keys are always strings on the wire; pydantic must have
        # coerced them to int per the `dict[int, str]` annotation.
        assert all(isinstance(k, int) for k in kwargs["alias_overrides"])


# ---------------------------------------------------------------------------
# 3. error translation
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    def test_session_not_found_answers_401_with_error_code(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.status.side_effect = KeboolaApiError(
            "no session", error_code=ErrorCode.SESSION_NOT_FOUND
        )
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.get("/auth/status", headers=AUTH, params={"stack": STACK})

        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["error"]["code"] == "SESSION_NOT_FOUND"

    def test_config_error_from_bad_selector_combo_is_4xx_not_500(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.register_projects.side_effect = ConfigError(
            "Selection modes are mutually exclusive; got select_all + project_ids."
        )
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post(
                "/auth/register-projects",
                headers=AUTH,
                json={"all": True, "project_ids": [1]},
            )

        assert 400 <= resp.status_code < 500, resp.text
        assert resp.json()["error"]["code"] == "CONFIG_ERROR"


# ---------------------------------------------------------------------------
# 4. permission enforcement
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    def _registry(self) -> ServiceRegistry:
        auth_svc = MagicMock()
        auth_svc.list_project_candidates.return_value = _candidates_result()
        auth_svc.status.return_value = _status_result()
        auth_svc.register_projects.return_value = _register_result()
        return _mock_registry(auth=auth_svc)

    def _deny_writes_app(self, tmp_path: Path) -> Any:
        """App whose session policy denies writes, with the mock registry bound.

        The engine is passed explicitly (the embedder/test escape hatch) and
        lives on ``app.state``, so the ``get_registry`` override below cannot
        take enforcement with it.
        """
        from keboola_agent_cli.permissions import apply_firewall_flags

        engine = PermissionEngine(
            apply_firewall_flags(None, deny_writes=True, deny_destructive=False)
        )
        app = create_app(
            config_dir=str(tmp_path), auth_token="test-token", permission_engine=engine
        )
        app.dependency_overrides[get_registry] = lambda: self._registry()
        return app

    def test_post_register_projects_is_denied(self, tmp_path: Path) -> None:
        with TestClient(self._deny_writes_app(tmp_path)) as client:
            resp = client.post("/auth/register-projects", headers=AUTH, json={"all": True})

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_get_projects_and_status_pass_through(self, tmp_path: Path) -> None:
        with TestClient(self._deny_writes_app(tmp_path)) as client:
            assert client.get("/auth/projects", headers=AUTH).status_code == 200
            assert client.get("/auth/status", headers=AUTH).status_code == 200

    def test_persisted_policy_in_the_served_dir_blocks_only_the_write(self, tmp_path: Path) -> None:
        """The reachable enforcement recipe, end to end.

        `kbagent --deny-writes serve` cannot start (`serve` is admin-class and
        `cli:write` spans admin), so a narrow persisted policy in the SERVED
        config dir is what an operator actually uses. No engine is passed here
        -- `create_app` must load this policy from disk itself.
        """
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import PermissionPolicy

        store = ConfigStore(config_dir=tmp_path)
        config = store.load()
        config.permissions = PermissionPolicy(mode="allow", deny=["auth.register-projects"])
        store.save(config)

        app = create_app(config_dir=str(tmp_path), auth_token="test-token")
        app.dependency_overrides[get_registry] = lambda: self._registry()

        with TestClient(app) as client:
            denied = client.post("/auth/register-projects", headers=AUTH, json={"all": True})
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
            assert client.get("/auth/projects", headers=AUTH).status_code == 200
            assert client.get("/auth/status", headers=AUTH).status_code == 200


# ---------------------------------------------------------------------------
# 5. token-free serialization
# ---------------------------------------------------------------------------


class TestTokenFreeSerialization:
    def test_register_projects_response_carries_no_token_material(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="prod-123",
                    project_id=123,
                    project_name="Prod",
                    status="registered",
                ),
                RegisteredProject(
                    alias="stage-456",
                    project_id=456,
                    project_name="Stage",
                    status="exists",
                    note="Already registered.",
                ),
            ],
            warnings=["Alias 'foo' already points at a different project; not overwritten."],
        )
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post("/auth/register-projects", headers=AUTH, json={"all": True})

        assert resp.status_code == 200, resp.text
        for needle in ("kbc-session://", "kbc_at_", "kbc_rt_"):
            assert needle not in resp.text

    def test_session_unsupported_features_survives_the_rest_boundary(self, tmp_path: Path) -> None:
        """`session_unsupported_features` is the caller's authoritative list.

        It rides `RegisterProjectsResult` via a default factory, so a REST
        caller that just registered session projects learns which surfaces will
        fail on them WITHOUT re-deriving the list by hand. `asdict` must carry
        it through unchanged -- dropping it would silently push every caller
        back to hard-coding a copy.
        """
        auth_svc = MagicMock()
        auth_svc.register_projects.return_value = _register_result()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post("/auth/register-projects", headers=AUTH, json={"all": True})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_unsupported_features"] == list(SESSION_UNSUPPORTED_FEATURES)
        assert body["session_unsupported_features"], "the list must not be empty"

    def test_projects_response_carries_no_token_material(self, tmp_path: Path) -> None:
        auth_svc = MagicMock()
        auth_svc.list_project_candidates.return_value = _candidates_result(
            candidates=[
                ProjectCandidate(
                    project_id=123,
                    project_name="Prod",
                    role="admin",
                    default_alias="prod-123",
                    existing_alias="prod-123",
                    registered=True,
                ),
                ProjectCandidate(
                    project_id=456,
                    project_name="Stage",
                    role="share",
                    default_alias="stage-456",
                    existing_alias="",
                    registered=False,
                ),
            ]
        )
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.get("/auth/projects", headers=AUTH)

        assert resp.status_code == 200, resp.text
        for needle in ("kbc-session://", "kbc_at_", "kbc_rt_"):
            assert needle not in resp.text


# ---------------------------------------------------------------------------
# 6. login / login-password / logout have no route
# ---------------------------------------------------------------------------


class TestNoCredentialMintingRoutes:
    @pytest.mark.parametrize(
        "path",
        ["/auth/login", "/auth/login-password", "/auth/logout"],
    )
    def test_route_does_not_exist(self, tmp_path: Path, path: str) -> None:
        auth_svc = MagicMock()
        app = _make_app_with_registry(tmp_path, _mock_registry(auth=auth_svc))

        with TestClient(app) as client:
            resp = client.post(path, headers=AUTH, json={})

        assert resp.status_code == 404, resp.text
