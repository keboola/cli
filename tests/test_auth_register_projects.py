"""Tests for the interactive-picker service surface: `AuthService`
`candidates_from_projects` / `list_project_candidates` / `register_projects`,
and the shared `_apply_selections` also exercised via `login(register_projects=True)`.

Split out of `test_auth_service.py` (which still owns login/status/logout)
because this file's fixtures center on introspection + config collisions
rather than the PKCE/device flow. Every network call is served by the same
kind of plain-stand-in `_FakeAuthClient` used there -- no mock library, so
call sequencing (e.g. "exactly one introspect") can be asserted precisely.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from keboola_agent_cli.auth.environment import BrowserEnvironment
from keboola_agent_cli.auth.models import (
    AuthProject,
    AuthUser,
    CliTokenResponse,
    IntrospectResponse,
    RevokeResult,
    StackSession,
)
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services import auth_service as svc_mod
from keboola_agent_cli.services.auth_service import (
    AuthService,
    ProjectCandidate,
    ProjectSelection,
)

STACK_URL = "https://connection.keboola.com"
STATIC_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


# ----------------------------------------------------------------------------
# PKCE loopback server -- never let the two `login()` tests below open a real
# listener and wait for a real browser callback that will never arrive.
# ----------------------------------------------------------------------------


class _FakeCallbackServer:
    """Stand-in for `PkceCallbackServer`: succeeds with a fixed callback."""

    def __init__(self, *, expected_state: str) -> None:
        self._expected_state = expected_state
        self.redirect_uri = "http://127.0.0.1:1/callback"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def wait(self, timeout: float | None = None):
        from keboola_agent_cli.auth.pkce import LoopbackCallback

        return LoopbackCallback(code="auth-code", state=self._expected_state)


@pytest.fixture(autouse=True)
def _patch_pkce_server(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Replace the real loopback listener with the in-memory fake for every test."""
    monkeypatch.setattr(svc_mod, "PkceCallbackServer", _FakeCallbackServer)
    yield


# ----------------------------------------------------------------------------
# Fakes (mirrors test_auth_service.py's _FakeAuthClient)
# ----------------------------------------------------------------------------


class _FakeAuthClient:
    """Stand-in for `AuthClient` recording every call it receives, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        self.introspect_response: IntrospectResponse | None = None
        self.introspect_calls = 0
        self.exchange_response: CliTokenResponse | None = None
        self.revoke_result = RevokeResult(confirmed=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def authorize_url(self, **kwargs: Any) -> str:
        self.calls.append(("authorize_url", kwargs))
        return "https://connection.keboola.com/admin/auth/pkce/authorize?..."

    def exchange_pkce_code(self, **kwargs: Any) -> CliTokenResponse:
        self.calls.append(("exchange_pkce_code", kwargs))
        assert self.exchange_response is not None
        return self.exchange_response

    def introspect(self, access_token: str) -> IntrospectResponse:
        self.calls.append(("introspect", access_token))
        self.introspect_calls += 1
        assert self.introspect_response is not None
        return self.introspect_response

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(("refresh", refresh_token))
        raise AssertionError("refresh should not be needed for a fresh session in these tests")

    def revoke(self, token: str, *, token_type_hint: str = "refreshToken") -> RevokeResult:
        self.calls.append(("revoke", (token, token_type_hint)))
        return self.revoke_result

    def delete_session(self, session_id: str, access_token: str) -> RevokeResult:
        self.calls.append(("delete_session", (session_id, access_token)))
        return RevokeResult(confirmed=True)


@pytest.fixture
def store(tmp_config_dir: Path) -> ConfigStore:
    return ConfigStore(config_dir=tmp_config_dir)


@pytest.fixture
def state_store(tmp_config_dir: Path) -> AuthStateStore:
    return AuthStateStore(tmp_config_dir)


def _make_service(
    store: ConfigStore, state_store: AuthStateStore, client: _FakeAuthClient
) -> AuthService:
    return AuthService(
        store,
        auth_client_factory=lambda _stack_url: client,  # ty: ignore[invalid-argument-type]
        state_store=state_store,
        browser_env_detector=lambda: BrowserEnvironment(
            loopback_browser_usable=True, reason="", opener="xdg-open"
        ),
        browser_opener=lambda _url: True,
        sleep=lambda _seconds: None,
    )


def _live_session(*, session_id: str = "sess-1", access_token: str = "at-1") -> StackSession:
    """A session whose access token is fresh enough that no refresh is triggered."""
    now = datetime.now(UTC)
    return StackSession(
        stack_url=STACK_URL,
        session_id=session_id,
        user_email="user@example.com",
        user_name="Test User",
        access_token=access_token,
        refresh_token="rt-1",
        access_expires_at=now + timedelta(hours=1),
        refresh_expires_at=now + timedelta(days=30),
        created_at=now,
    )


def _introspect(projects: list[AuthProject], session_id: str = "sess-1") -> IntrospectResponse:
    return IntrospectResponse(
        active=True,
        sessionId=session_id,
        user=AuthUser(email="user@example.com", name="Test User"),
        projects=projects,
    )


# ----------------------------------------------------------------------------
# candidates_from_projects -- pure, no network
# ----------------------------------------------------------------------------


class TestCandidatesFromProjects:
    def test_simple_project_gets_slugified_alias(self, store, state_store) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL, [{"id": 555, "name": "My Cool Project", "role": "admin"}]
        )
        assert candidates == [
            ProjectCandidate(
                project_id=555,
                project_name="My Cool Project",
                role="admin",
                default_alias="my-cool-project",
                existing_alias="",
                registered=False,
            )
        ]

    def test_two_projects_with_same_name_get_distinct_usable_aliases(
        self, store, state_store
    ) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL,
            [
                {"id": 1, "name": "Demo", "role": "admin"},
                {"id": 2, "name": "Demo", "role": "guest"},
            ],
        )
        aliases = [c.default_alias for c in candidates]
        assert aliases == ["demo", "demo-2"]
        assert len(set(aliases)) == 2
        # Both are new, unregistered candidates -- neither collided into a skip.
        assert all(not c.registered for c in candidates)

    def test_three_way_name_collision_uses_full_suffix_ladder(self, store, state_store) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL,
            [
                {"id": 1, "name": "Demo", "role": "admin"},
                {"id": 2, "name": "Demo", "role": "guest"},
                {"id": 3, "name": "Demo", "role": "guest"},
            ],
        )
        assert [c.default_alias for c in candidates] == ["demo", "demo-2", "demo-3"]

    def test_collision_with_existing_static_token_project_suffixes(
        self, store, state_store
    ) -> None:
        """A name collision with an existing *static-token* project must never
        collapse the two onto one alias -- the new candidate gets a `-{id}`
        suffix instead, and the static project stays exactly as it is."""
        store.add_project(
            "jirka-bq-sox",
            ProjectConfig(
                stack_url=STACK_URL, token=STATIC_TOKEN, project_name="Unrelated", project_id=1
            ),
        )
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL, [{"id": 9840, "name": "Jirka BQ SOX", "role": "admin"}]
        )
        assert candidates[0].default_alias == "jirka-bq-sox-9840"
        assert candidates[0].registered is False
        # The static project itself is untouched by candidate computation.
        assert store.get_project("jirka-bq-sox").token == STATIC_TOKEN

    def test_already_registered_project_matches_on_id_and_stack_not_alias_string(
        self, store, state_store
    ) -> None:
        """A project registered under a hand-picked alias must be reported
        under THAT alias -- matched on (project_id, stack_url), never on
        whatever alias string happens to be in config.json."""
        store.add_project(
            "my-custom-name",
            ProjectConfig(
                stack_url=STACK_URL,
                token="kbc-session://9840",
                project_name="Jirka BQ SOX",
                project_id=9840,
            ),
        )
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL, [{"id": 9840, "name": "Jirka BQ SOX", "role": "admin"}]
        )
        assert candidates[0].registered is True
        assert candidates[0].existing_alias == "my-custom-name"
        assert candidates[0].default_alias == "my-custom-name"

    def test_registration_on_a_different_stack_is_not_a_match(self, store, state_store) -> None:
        """The same project id registered under a DIFFERENT stack must not be
        treated as already-registered here -- stack_url is part of the match."""
        store.add_project(
            "other-stack-alias",
            ProjectConfig(
                stack_url="https://connection.eu-central-1.keboola.com",
                token="kbc-session://9840",
                project_name="Jirka BQ SOX",
                project_id=9840,
            ),
        )
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL, [{"id": 9840, "name": "Jirka BQ SOX", "role": "admin"}]
        )
        assert candidates[0].registered is False
        assert candidates[0].default_alias == "jirka-bq-sox"

    def test_blank_name_falls_back_to_project_id_alias(self, store, state_store) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        candidates = service.candidates_from_projects(
            STACK_URL, [{"id": 42, "name": "!!!", "role": "admin"}]
        )
        assert candidates[0].default_alias == "project-42"


# ----------------------------------------------------------------------------
# list_project_candidates -- requires a session, introspects
# ----------------------------------------------------------------------------


class TestListProjectCandidates:
    def test_no_session_raises_session_not_found(self, store, state_store) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        with pytest.raises(KeboolaApiError) as exc_info:
            service.list_project_candidates(stack=STACK_URL)
        assert exc_info.value.error_code == ErrorCode.SESSION_NOT_FOUND

    def test_delegates_to_candidates_from_projects(self, store, state_store) -> None:
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.list_project_candidates(stack=STACK_URL)

        assert result.stack_url == STACK_URL
        assert len(result.candidates) == 1
        assert result.candidates[0].default_alias == "my-cool-project"
        assert client.introspect_calls == 1


# ----------------------------------------------------------------------------
# register_projects
# ----------------------------------------------------------------------------


class TestRegisterProjectsService:
    def test_no_session_raises_session_not_found(self, store, state_store) -> None:
        service = _make_service(store, state_store, _FakeAuthClient())
        with pytest.raises(KeboolaApiError) as exc_info:
            service.register_projects(stack=STACK_URL, selections=[ProjectSelection(project_id=1)])
        assert exc_info.value.error_code == ErrorCode.SESSION_NOT_FOUND

    def test_registers_selected_project_under_default_alias(self, store, state_store) -> None:
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.register_projects(
            stack=STACK_URL, selections=[ProjectSelection(project_id=555)]
        )

        assert result.status == "ok"
        assert result.stack_url == STACK_URL
        assert result.registered_projects[0].status == "registered"
        assert result.registered_projects[0].alias == "my-cool-project"
        project = store.get_project("my-cool-project")
        assert project is not None
        assert project.token == "kbc-session://555"

    def test_explicit_alias_override_is_used_when_given(self, store, state_store) -> None:
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.register_projects(
            stack=STACK_URL,
            selections=[ProjectSelection(project_id=555, alias="padak")],
        )

        assert result.registered_projects[0].alias == "padak"
        assert store.get_project("padak") is not None
        assert store.get_project("my-cool-project") is None

    def test_selection_naming_inaccessible_project_id_raises_config_error(
        self, store, state_store
    ) -> None:
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        with pytest.raises(ConfigError, match="9999"):
            service.register_projects(
                stack=STACK_URL, selections=[ProjectSelection(project_id=9999)]
            )
        # Nothing should have been written -- validated up front, before apply.
        assert store.get_project("my-cool-project") is None

    def test_invalid_explicit_alias_raises_config_error(self, store, state_store) -> None:
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        with pytest.raises(ConfigError):
            service.register_projects(
                stack=STACK_URL,
                selections=[ProjectSelection(project_id=555, alias="bad alias with spaces")],
            )

    def test_already_registered_under_custom_alias_reports_exists_not_rewritten(
        self, store, state_store
    ) -> None:
        store.add_project(
            "my-custom-name",
            ProjectConfig(
                stack_url=STACK_URL,
                token="kbc-session://9840",
                project_name="Jirka BQ SOX",
                project_id=9840,
            ),
        )
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=9840, name="Jirka BQ SOX", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.register_projects(
            stack=STACK_URL, selections=[ProjectSelection(project_id=9840)]
        )

        assert result.registered_projects[0].status == "exists"
        assert result.registered_projects[0].alias == "my-custom-name"
        assert result.warnings == []
        # Still exactly one config entry for this project -- no duplicate write.
        assert store.get_project("my-custom-name").token == "kbc-session://9840"

    def test_requesting_a_different_alias_for_an_already_registered_project_is_skipped(
        self, store, state_store
    ) -> None:
        store.add_project(
            "my-custom-name",
            ProjectConfig(
                stack_url=STACK_URL,
                token="kbc-session://9840",
                project_name="Jirka BQ SOX",
                project_id=9840,
            ),
        )
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=9840, name="Jirka BQ SOX", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.register_projects(
            stack=STACK_URL,
            selections=[ProjectSelection(project_id=9840, alias="a-different-alias")],
        )

        assert result.registered_projects[0].status == "skipped"
        assert len(result.warnings) == 1
        assert "my-custom-name" in result.warnings[0]
        assert store.get_project("a-different-alias") is None

    def test_alias_taken_by_unrelated_project_is_skipped_and_never_overwritten(
        self, store, state_store
    ) -> None:
        store.add_project(
            "taken",
            ProjectConfig(
                stack_url=STACK_URL, token=STATIC_TOKEN, project_name="Other", project_id=1
            ),
        )
        state_store.put_session(_live_session())
        client = _FakeAuthClient()
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.register_projects(
            stack=STACK_URL,
            selections=[ProjectSelection(project_id=555, alias="taken")],
        )

        assert result.registered_projects[0].status == "skipped"
        assert len(result.warnings) == 1
        assert store.get_project("taken").token == STATIC_TOKEN


# ----------------------------------------------------------------------------
# login(register_projects=True) -- shared _apply_selections path
# ----------------------------------------------------------------------------


class TestLoginRegisterProjectsSharedPath:
    def test_login_register_projects_issues_exactly_one_introspect_call(
        self, store, state_store
    ) -> None:
        client = _FakeAuthClient()
        client.exchange_response = CliTokenResponse(
            accessToken="at-1",
            refreshToken="rt-1",
            expiresIn=3600,
            sessionId="sess-1",
            user=AuthUser(email="user@example.com", name="Test User"),
        )
        client.introspect_response = _introspect(
            [AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL, register_projects=True)

        assert client.introspect_calls == 1
        assert len(result.registered_projects) == 1
        reg = result.registered_projects[0]
        assert reg.status == "registered"
        assert reg.alias == "my-cool-project"
        assert reg.project_id == 555
        assert reg.project_name == "My Cool Project"
        project = store.get_project("my-cool-project")
        assert project is not None
        assert project.token == "kbc-session://555"

    def test_login_register_projects_suffixes_duplicate_names_instead_of_skipping(
        self, store, state_store
    ) -> None:
        client = _FakeAuthClient()
        client.exchange_response = CliTokenResponse(
            accessToken="at-1",
            refreshToken="rt-1",
            expiresIn=3600,
            sessionId="sess-1",
            user=AuthUser(email="user@example.com", name="Test User"),
        )
        client.introspect_response = _introspect(
            [
                AuthProject(id=1, name="Demo", role="admin"),
                AuthProject(id=2, name="Demo", role="guest"),
            ]
        )
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL, register_projects=True)

        assert client.introspect_calls == 1
        statuses = {r.alias: r.status for r in result.registered_projects}
        assert statuses == {"demo": "registered", "demo-2": "registered"}
        assert store.get_project("demo") is not None
        assert store.get_project("demo-2") is not None
