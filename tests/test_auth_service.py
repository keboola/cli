"""Tests for `AuthService` -- login/status/logout business logic.

Every network call is served by `_FakeAuthClient` (a plain stand-in, not a
mock library, so PKCE/device call sequencing is asserted precisely) and every
environment/browser/sleep seam is injected, so these tests never open a
browser, sleep for real, or hit a real stack.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.auth.environment import BrowserEnvironment
from keboola_agent_cli.auth.models import (
    AuthProject,
    AuthUser,
    CliTokenResponse,
    DeviceAuthorization,
    IntrospectResponse,
    RevokeResult,
)
from keboola_agent_cli.auth.pkce import (
    LoopbackCallback,
    PkceAuthorizationError,
    PkceCallbackTimeout,
    PkceSetupError,
    PkceStateMismatch,
)
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services import auth_service as svc_mod
from keboola_agent_cli.services.auth_service import AuthService

STACK_URL = "https://connection.keboola.com"
ALIAS = "padak"
STATIC_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


# ----------------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------------


class _FakeAuthClient:
    """Stand-in for `AuthClient` recording every call it receives, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        self.authorize_url_value = "https://connection.keboola.com/admin/auth/pkce/authorize?..."
        self.exchange_response: CliTokenResponse | None = None
        self.exchange_side_effect: Exception | None = None
        self.introspect_response: IntrospectResponse | None = None
        self.introspect_side_effect: Exception | None = None
        self.refresh_response: CliTokenResponse | None = None
        self.refresh_side_effect: Exception | None = None
        self.revoke_result = RevokeResult(confirmed=True)
        self.delete_session_result = RevokeResult(confirmed=True)

    def __enter__(self) -> _FakeAuthClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def authorize_url(self, **kwargs: Any) -> str:
        self.calls.append(("authorize_url", kwargs))
        return self.authorize_url_value

    def exchange_pkce_code(self, **kwargs: Any) -> CliTokenResponse:
        self.calls.append(("exchange_pkce_code", kwargs))
        if self.exchange_side_effect is not None:
            raise self.exchange_side_effect
        assert self.exchange_response is not None
        return self.exchange_response

    def introspect(self, access_token: str) -> IntrospectResponse:
        self.calls.append(("introspect", access_token))
        if self.introspect_side_effect is not None:
            raise self.introspect_side_effect
        assert self.introspect_response is not None
        return self.introspect_response

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(("refresh", refresh_token))
        if self.refresh_side_effect is not None:
            raise self.refresh_side_effect
        assert self.refresh_response is not None
        return self.refresh_response

    def revoke(self, token: str, *, token_type_hint: str = "refreshToken") -> RevokeResult:
        self.calls.append(("revoke", (token, token_type_hint)))
        return self.revoke_result

    def delete_session(self, session_id: str, access_token: str) -> RevokeResult:
        self.calls.append(("delete_session", (session_id, access_token)))
        return self.delete_session_result


class _FakeCallbackServer:
    """Stand-in for `PkceCallbackServer`: succeeds with a fixed callback."""

    build_error: Exception | None = None
    wait_error: Exception | None = None
    wait_state_override: str | None = None

    def __init__(self, *, expected_state: str) -> None:
        build_error = type(self).build_error
        if build_error is not None:
            raise build_error
        self._expected_state = expected_state
        self.redirect_uri = "http://127.0.0.1:1/callback"

    def __enter__(self) -> _FakeCallbackServer:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> LoopbackCallback:
        wait_error = type(self).wait_error
        if wait_error is not None:
            raise wait_error
        state = type(self).wait_state_override or self._expected_state
        return LoopbackCallback(code="auth-code", state=state)


def _reset_fake_callback_server() -> None:
    _FakeCallbackServer.build_error = None
    _FakeCallbackServer.wait_error = None
    _FakeCallbackServer.wait_state_override = None


@pytest.fixture(autouse=True)
def _patch_pkce_server(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Replace the real loopback listener with the in-memory fake for every test."""
    _reset_fake_callback_server()
    monkeypatch.setattr(svc_mod, "PkceCallbackServer", _FakeCallbackServer)
    yield
    _reset_fake_callback_server()


@pytest.fixture
def store(tmp_config_dir: Path) -> ConfigStore:
    return ConfigStore(config_dir=tmp_config_dir)


@pytest.fixture
def state_store(tmp_config_dir: Path) -> AuthStateStore:
    return AuthStateStore(tmp_config_dir)


def _usable_env() -> BrowserEnvironment:
    return BrowserEnvironment(loopback_browser_usable=True, reason="", opener="xdg-open")


def _unusable_env(reason: str = "remote session") -> BrowserEnvironment:
    return BrowserEnvironment(loopback_browser_usable=False, reason=reason, opener="")


def _make_service(
    store: ConfigStore,
    state_store: AuthStateStore,
    client: _FakeAuthClient,
    *,
    browser_env: BrowserEnvironment | None = None,
) -> AuthService:
    env = browser_env or _usable_env()
    return AuthService(
        store,
        auth_client_factory=lambda _stack_url: client,  # ty: ignore[invalid-argument-type]
        state_store=state_store,
        browser_env_detector=lambda: env,
        browser_opener=lambda _url: True,
        sleep=lambda _seconds: None,
    )


def _tokens(
    *,
    session_id: str = "sess-1",
    access: str = "at-1",
    refresh: str = "rt-1",
    expires_in: int = 3600,
) -> CliTokenResponse:
    return CliTokenResponse(
        accessToken=access,
        refreshToken=refresh,
        expiresIn=expires_in,
        sessionId=session_id,
        user=AuthUser(email="user@example.com", name="Test User"),
    )


def _introspect(
    projects: list[AuthProject] | None = None, session_id: str = "sess-1"
) -> IntrospectResponse:
    return IntrospectResponse(
        active=True,
        sessionId=session_id,
        user=AuthUser(email="user@example.com", name="Test User"),
        projects=projects or [AuthProject(id=101, name="Prod Project", role="admin")],
    )


# ----------------------------------------------------------------------------
# Stack resolution
# ----------------------------------------------------------------------------


class TestResolveStackUrl:
    def test_explicit_url_is_normalized(self, store, state_store) -> None:
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        assert service._resolve_stack_url("connection.keboola.com") == STACK_URL

    def test_explicit_alias_wins_over_hostname_reading(self, store, state_store) -> None:
        store.add_project(
            ALIAS, ProjectConfig(stack_url=STACK_URL, token=STATIC_TOKEN, project_name="P")
        )
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        assert service._resolve_stack_url(ALIAS) == STACK_URL

    def test_default_project_used_when_no_stack_given(self, store, state_store) -> None:
        store.add_project(
            ALIAS, ProjectConfig(stack_url=STACK_URL, token=STATIC_TOKEN, project_name="P")
        )
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        assert service._resolve_stack_url(None) == STACK_URL

    def test_no_stack_no_default_raises_config_error(self, store, state_store) -> None:
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        with pytest.raises(ConfigError):
            service._resolve_stack_url(None)

    def test_garbage_stack_raises_config_error(self, store, state_store) -> None:
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        with pytest.raises(ConfigError):
            service._resolve_stack_url("http://insecure.example.com")


# ----------------------------------------------------------------------------
# login -- PKCE / device selection
# ----------------------------------------------------------------------------


class TestLoginMethodSelection:
    def test_pkce_happy_path(self, store, state_store) -> None:
        client = _FakeAuthClient()
        client.exchange_response = _tokens()
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL)

        assert result.method == "pkce"
        assert result.fallback_reason == ""
        assert result.session_id == "sess-1"
        assert result.user_email == "user@example.com"
        assert [name for name, _ in client.calls] == [
            "authorize_url",
            "exchange_pkce_code",
            "introspect",
        ]
        session = state_store.get_session(STACK_URL)
        assert session is not None
        assert session.access_token == "at-1"
        assert session.refresh_token == "rt-1"

    def test_no_token_field_anywhere_in_result(self, store, state_store) -> None:
        client = _FakeAuthClient()
        client.exchange_response = _tokens(access="super-secret-at", refresh="super-secret-rt")
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)
        result = service.login(stack=STACK_URL)
        import dataclasses

        rendered = repr(dataclasses.asdict(result))
        assert "super-secret-at" not in rendered
        assert "super-secret-rt" not in rendered

    def test_environment_forces_device_flow(self, store, state_store, monkeypatch) -> None:
        client = _FakeAuthClient()
        client.introspect_response = _introspect()
        service = _make_service(
            store, state_store, client, browser_env=_unusable_env("Detected SSH session.")
        )

        outcome_tokens = _tokens(session_id="sess-device")
        prompted: list[DeviceAuthorization] = []

        def _fake_run_device_flow(_client, *, on_prompt, sleep):
            authorization = DeviceAuthorization(
                deviceCode="dc",
                userCode="ABCD-EFGH",
                verificationUri="https://connection.keboola.com/device",
                expiresIn=600,
            )
            on_prompt(authorization)
            from keboola_agent_cli.auth.device import DeviceFlowOutcome

            return DeviceFlowOutcome(tokens=outcome_tokens, polls=1)

        monkeypatch.setattr(svc_mod, "run_device_flow", _fake_run_device_flow)

        result = service.login(stack=STACK_URL, on_device_prompt=prompted.append)

        assert result.method == "device"
        assert result.fallback_reason == "Detected SSH session."
        assert len(prompted) == 1
        # PKCE was never attempted: authorize_url/exchange were never called.
        assert [name for name, _ in client.calls] == ["introspect"]

    def test_device_code_flag_skips_environment_check(
        self, store, state_store, monkeypatch
    ) -> None:
        client = _FakeAuthClient()
        client.introspect_response = _introspect()
        env_calls: list[None] = []

        def _tracking_env() -> BrowserEnvironment:
            env_calls.append(None)
            return _usable_env()

        service = AuthService(
            store,
            auth_client_factory=lambda _s: client,  # ty: ignore[invalid-argument-type]
            state_store=state_store,
            browser_env_detector=_tracking_env,
            browser_opener=lambda _url: True,
            sleep=lambda _s: None,
        )

        def _fake_run_device_flow(_client, *, on_prompt, sleep):
            from keboola_agent_cli.auth.device import DeviceFlowOutcome

            return DeviceFlowOutcome(tokens=_tokens(), polls=1)

        monkeypatch.setattr(svc_mod, "run_device_flow", _fake_run_device_flow)

        result = service.login(stack=STACK_URL, device_code=True)

        assert result.method == "device"
        assert env_calls == []  # --device-code bypasses environment detection entirely


class TestLoginPkceFallback:
    def test_setup_error_falls_back_to_device(self, store, state_store, monkeypatch) -> None:
        _FakeCallbackServer.build_error = PkceSetupError("could not bind loopback listener")
        client = _FakeAuthClient()
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)

        def _fake_run_device_flow(_client, *, on_prompt, sleep):
            from keboola_agent_cli.auth.device import DeviceFlowOutcome

            return DeviceFlowOutcome(tokens=_tokens(session_id="sess-fallback"), polls=1)

        monkeypatch.setattr(svc_mod, "run_device_flow", _fake_run_device_flow)

        result = service.login(stack=STACK_URL)

        assert result.method == "device"
        assert "could not bind loopback listener" in result.fallback_reason
        assert client.calls[0][0] == "introspect"  # PKCE never reached authorize_url

    def test_callback_timeout_falls_back_to_device(self, store, state_store, monkeypatch) -> None:
        _FakeCallbackServer.wait_error = PkceCallbackTimeout("timed out")
        client = _FakeAuthClient()
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)

        def _fake_run_device_flow(_client, *, on_prompt, sleep):
            from keboola_agent_cli.auth.device import DeviceFlowOutcome

            return DeviceFlowOutcome(tokens=_tokens(), polls=1)

        monkeypatch.setattr(svc_mod, "run_device_flow", _fake_run_device_flow)

        result = service.login(stack=STACK_URL)

        assert result.method == "device"
        assert "timed out" in result.fallback_reason
        assert "exchange_pkce_code" not in [c[0] for c in client.calls]

    def test_state_mismatch_is_terminal_no_fallback(self, store, state_store, monkeypatch) -> None:
        _FakeCallbackServer.wait_error = PkceStateMismatch("state did not match")
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)

        def _unexpected_device_flow(*args, **kwargs):
            raise AssertionError("device flow must never run after a state mismatch")

        monkeypatch.setattr(svc_mod, "run_device_flow", _unexpected_device_flow)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.login(stack=STACK_URL)
        assert exc_info.value.error_code == ErrorCode.AUTH_STATE_MISMATCH
        assert "exchange_pkce_code" not in [c[0] for c in client.calls]

    def test_authorization_error_is_terminal_no_fallback(
        self, store, state_store, monkeypatch
    ) -> None:
        _FakeCallbackServer.wait_error = PkceAuthorizationError("access_denied", "user cancelled")
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)

        def _unexpected_device_flow(*args, **kwargs):
            raise AssertionError("device flow must never run after an authorization error")

        monkeypatch.setattr(svc_mod, "run_device_flow", _unexpected_device_flow)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.login(stack=STACK_URL)
        assert exc_info.value.error_code == ErrorCode.AUTH_FLOW_DENIED

    def test_exchange_failure_after_callback_is_terminal_no_fallback(
        self, store, state_store, monkeypatch
    ) -> None:
        client = _FakeAuthClient()
        client.exchange_side_effect = KeboolaApiError(
            "boom", error_code=ErrorCode.API_ERROR, status_code=500
        )
        service = _make_service(store, state_store, client)

        def _unexpected_device_flow(*args, **kwargs):
            raise AssertionError(
                "device flow must never run after a post-callback exchange failure"
            )

        monkeypatch.setattr(svc_mod, "run_device_flow", _unexpected_device_flow)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.login(stack=STACK_URL)
        assert exc_info.value.error_code == ErrorCode.API_ERROR


# ----------------------------------------------------------------------------
# login -- session replacement (review B-1)
# ----------------------------------------------------------------------------


class TestLoginSessionReplacement:
    def test_replaces_and_revokes_previous_session(self, store, state_store) -> None:
        state_store.put_session(_existing_session(session_id="old-sess", refresh_token="old-rt"))
        client = _FakeAuthClient()
        client.exchange_response = _tokens(session_id="new-sess")
        client.introspect_response = _introspect(session_id="new-sess")
        client.revoke_result = RevokeResult(confirmed=True)
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL)

        assert result.replaced_session_id == "old-sess"
        assert result.orphaned_session_id == ""
        assert result.warnings == []
        revoke_calls = [c for c in client.calls if c[0] == "revoke"]
        assert revoke_calls == [("revoke", ("old-rt", "refreshToken"))]
        session = state_store.get_session(STACK_URL)
        assert session.session_id == "new-sess"
        assert session.orphaned_session_ids == []

    def test_unconfirmed_revoke_is_recorded_as_orphan_and_warned(self, store, state_store) -> None:
        state_store.put_session(_existing_session(session_id="old-sess", refresh_token="old-rt"))
        client = _FakeAuthClient()
        client.exchange_response = _tokens(session_id="new-sess")
        client.introspect_response = _introspect(session_id="new-sess")
        client.revoke_result = RevokeResult(confirmed=False, message="network blip")
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL)

        assert result.orphaned_session_id == "old-sess"
        assert len(result.warnings) == 1
        session = state_store.get_session(STACK_URL)
        # The NEW session must survive even though the old one's revoke failed.
        assert session.session_id == "new-sess"
        assert session.orphaned_session_ids == ["old-sess"]

    def test_new_pair_persisted_before_revoke_is_attempted(self, store, state_store) -> None:
        """Durability ordering: put_session happens before the revoke call."""
        state_store.put_session(_existing_session(session_id="old-sess", refresh_token="old-rt"))
        client = _FakeAuthClient()
        client.exchange_response = _tokens(session_id="new-sess")
        client.introspect_response = _introspect(session_id="new-sess")

        seen_session_at_revoke_time: list[str] = []
        original_revoke = client.revoke

        def _tracking_revoke(token, *, token_type_hint="refreshToken"):
            stored = state_store.get_session(STACK_URL)
            seen_session_at_revoke_time.append(stored.session_id if stored else "")
            return original_revoke(token, token_type_hint=token_type_hint)

        client.revoke = _tracking_revoke  # ty: ignore[invalid-assignment]
        service = _make_service(store, state_store, client)

        service.login(stack=STACK_URL)

        assert seen_session_at_revoke_time == ["new-sess"]


class TestLoginRefreshExpiry:
    """Login must record a server-sent refresh expiry, not hardcode "unknown".

    Before `CliTokenResponse.refresh_expiry` existed, rotation honoured
    `refreshExpiresIn` while login wrote None, so a backend that started sending
    it would have been ignored until the first refresh -- and `auth status`
    would have reported no refresh expiry for a session that had one.
    """

    def test_absent_field_leaves_the_expiry_unknown(self, store, state_store) -> None:
        """Today's real wire shape: nothing is guessed, so the server stays the authority."""
        client = _FakeAuthClient()
        client.exchange_response = _tokens()
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL)

        assert result.refresh_expires_at == ""
        session = state_store.get_session(STACK_URL)
        assert session.refresh_expires_at is None

    def test_server_sent_seconds_are_persisted_and_reported(self, store, state_store) -> None:
        client = _FakeAuthClient()
        client.exchange_response = CliTokenResponse.model_validate(
            {
                "accessToken": "at-1",
                "refreshToken": "rt-1",
                "expiresIn": 3600,
                "sessionId": "sess-1",
                "refreshExpiresIn": 30 * 24 * 3600,
            }
        )
        client.introspect_response = _introspect()
        service = _make_service(store, state_store, client)

        before = datetime.now(UTC)
        result = service.login(stack=STACK_URL)

        session = state_store.get_session(STACK_URL)
        assert session.refresh_expires_at is not None
        assert session.refresh_expires_at >= before + timedelta(days=30)
        assert session.refresh_expires_at <= datetime.now(UTC) + timedelta(days=30)
        # Whatever was persisted is exactly what `auth status`/`--json` reports.
        assert result.refresh_expires_at == session.refresh_expires_at.isoformat()


def _existing_session(*, session_id: str, refresh_token: str):
    from datetime import UTC, datetime, timedelta

    from keboola_agent_cli.auth.models import StackSession

    now = datetime.now(UTC)
    return StackSession(
        stack_url=STACK_URL,
        session_id=session_id,
        user_email="old@example.com",
        user_name="Old User",
        access_token="old-at",
        refresh_token=refresh_token,
        access_expires_at=now + timedelta(hours=1),
        refresh_expires_at=now + timedelta(days=30),
        created_at=now,
    )


# ----------------------------------------------------------------------------
# login -- --register-projects
# ----------------------------------------------------------------------------


class TestRegisterProjects:
    def test_registers_new_alias(self, store, state_store) -> None:
        client = _FakeAuthClient()
        client.exchange_response = _tokens()
        client.introspect_response = _introspect(
            projects=[AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL, register_projects=True)

        assert len(result.registered_projects) == 1
        reg = result.registered_projects[0]
        assert reg.status == "registered"
        assert reg.alias == "my-cool-project"
        project = store.get_project("my-cool-project")
        assert project is not None
        assert project.token == "kbc-session://555"
        assert project.project_id == 555

    def test_existing_alias_same_project_reports_exists_and_does_not_write(
        self, store, state_store
    ) -> None:
        store.add_project(
            "my-cool-project",
            ProjectConfig(
                stack_url=STACK_URL,
                token="kbc-session://555",
                project_name="My Cool Project",
                project_id=555,
            ),
        )
        client = _FakeAuthClient()
        client.exchange_response = _tokens()
        client.introspect_response = _introspect(
            projects=[AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL, register_projects=True)

        assert result.registered_projects[0].status == "exists"
        assert result.warnings == []

    def test_existing_alias_collision_suffixes_instead_of_skipping(
        self, store, state_store
    ) -> None:
        """Deliberate behaviour change: a name collision with an existing
        static-token project used to skip-with-warning; it now gets a
        `-{id}` suffixed alias instead, and the static project is left
        completely untouched (never overwritten)."""
        store.add_project(
            "my-cool-project",
            ProjectConfig(
                stack_url=STACK_URL, token=STATIC_TOKEN, project_name="Unrelated", project_id=1
            ),
        )
        client = _FakeAuthClient()
        client.exchange_response = _tokens()
        client.introspect_response = _introspect(
            projects=[AuthProject(id=555, name="My Cool Project", role="admin")]
        )
        service = _make_service(store, state_store, client)

        result = service.login(stack=STACK_URL, register_projects=True)

        assert result.registered_projects[0].status == "registered"
        assert result.registered_projects[0].alias == "my-cool-project-555"
        assert result.warnings == []
        # The static-token project must be left completely untouched.
        untouched = store.get_project("my-cool-project")
        assert untouched.token == STATIC_TOKEN
        new_project = store.get_project("my-cool-project-555")
        assert new_project is not None
        assert new_project.token == "kbc-session://555"


# ----------------------------------------------------------------------------
# status
# ----------------------------------------------------------------------------


class TestStatus:
    def test_missing_session(self, store, state_store) -> None:
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        result = service.status(stack=STACK_URL)
        assert result.status == "missing"

    def test_live_session_not_rotated(self, store, state_store) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        client = _FakeAuthClient()
        client.introspect_response = _introspect(session_id="sess-1")
        service = _make_service(store, state_store, client)

        result = service.status(stack=STACK_URL)

        assert result.status == "live"
        assert result.session_id == "sess-1"
        assert [c[0] for c in client.calls] == ["introspect"]  # no refresh call

    def test_stale_access_token_triggers_refresh_and_reports_refreshed(
        self, store, state_store
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from keboola_agent_cli.auth.models import StackSession

        now = datetime.now(UTC)
        stale = StackSession(
            stack_url=STACK_URL,
            session_id="sess-1",
            access_token="stale-at",
            refresh_token="rt-1",
            access_expires_at=now - timedelta(minutes=5),
            refresh_expires_at=now + timedelta(days=10),
            created_at=now - timedelta(hours=2),
        )
        state_store.put_session(stale)
        client = _FakeAuthClient()
        client.refresh_response = _tokens(access="fresh-at", refresh="fresh-rt")
        client.introspect_response = _introspect(session_id="sess-1")
        service = _make_service(store, state_store, client)

        result = service.status(stack=STACK_URL)

        assert result.status == "refreshed"
        session = state_store.get_session(STACK_URL)
        assert session.access_token == "fresh-at"

    def test_expired_refresh_token_reports_expired(self, store, state_store) -> None:
        from datetime import UTC, datetime, timedelta

        from keboola_agent_cli.auth.models import StackSession

        now = datetime.now(UTC)
        dead = StackSession(
            stack_url=STACK_URL,
            session_id="sess-1",
            access_token="stale-at",
            refresh_token="rt-1",
            access_expires_at=now - timedelta(days=1),
            refresh_expires_at=now - timedelta(minutes=1),
            created_at=now - timedelta(days=40),
        )
        state_store.put_session(dead)
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)

        result = service.status(stack=STACK_URL)

        assert result.status == "expired"
        assert state_store.get_session(STACK_URL) is None

    def test_server_rejected_refresh_reports_expired_and_purges(self, store, state_store) -> None:
        """A live-looking session whose refresh the SERVER rejects must self-heal.

        Regression test for the real production failure: the refresh token
        had not reached any locally known expiry (`refresh_expires_at` is
        None, as it is for every session today since the token response
        carries no refresh-expiry field), so the local pre-check in
        `_refresh_locked` step 6 could not catch it -- only the server's
        answer could. When that answer was misclassified as `INVALID_TOKEN`
        rather than `SESSION_EXPIRED`, `status()` re-raised instead of
        reporting, and the dead session was never purged, so every
        subsequent command failed the same opaque way indefinitely.
        """
        from datetime import UTC, datetime, timedelta

        from keboola_agent_cli.auth.models import StackSession

        now = datetime.now(UTC)
        state_store.put_session(
            StackSession(
                stack_url=STACK_URL,
                session_id="sess-1",
                access_token="stale-at",
                refresh_token="rt-revoked",
                access_expires_at=now - timedelta(minutes=30),
                refresh_expires_at=None,
                created_at=now - timedelta(hours=12),
            )
        )
        client = _FakeAuthClient()
        client.refresh_side_effect = KeboolaApiError(
            "Your Keboola login expired or was revoked. Run `kbagent auth login` to sign in again.",
            status_code=401,
            error_code=ErrorCode.SESSION_EXPIRED,
            retryable=False,
        )
        service = _make_service(store, state_store, client)

        result = service.status(stack=STACK_URL)

        assert result.status == "expired"
        assert "kbagent auth login" in result.detail
        assert state_store.get_session(STACK_URL) is None

    def test_network_failure_reports_degraded_not_expired(self, store, state_store) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        client = _FakeAuthClient()
        client.introspect_side_effect = KeboolaApiError(
            "cannot connect", error_code=ErrorCode.CONNECTION_ERROR, retryable=True
        )
        service = _make_service(store, state_store, client)

        result = service.status(stack=STACK_URL)

        assert result.status == "degraded"
        assert result.session_id == "sess-1"
        # On-disk data still gets surfaced even though the live check failed.
        assert result.access_expires_at != ""


# ----------------------------------------------------------------------------
# logout
# ----------------------------------------------------------------------------


class TestLogout:
    def test_no_session_raises_session_not_found(self, store, state_store) -> None:
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.logout(stack=STACK_URL)
        assert exc_info.value.error_code == ErrorCode.SESSION_NOT_FOUND

    def test_confirmed_revoke_clears_local_session(self, store, state_store) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        client = _FakeAuthClient()
        client.revoke_result = RevokeResult(confirmed=True)
        service = _make_service(store, state_store, client)

        result = service.logout(stack=STACK_URL)

        assert result.remote_revoked is True
        assert state_store.get_session(STACK_URL) is None
        assert ("revoke", ("rt-1", "refreshToken")) in client.calls

    def test_unconfirmed_revoke_reported_distinctly_but_still_clears_local(
        self, store, state_store
    ) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        client = _FakeAuthClient()
        client.revoke_result = RevokeResult(confirmed=False, message="timed out")
        service = _make_service(store, state_store, client)

        result = service.logout(stack=STACK_URL)

        assert result.remote_revoked is False
        assert "may still be active" in result.detail
        # Local cleanup still proceeds even though the remote outcome is uncertain.
        assert state_store.get_session(STACK_URL) is None

    def test_retries_recorded_orphans_via_delete_session(self, store, state_store) -> None:
        session = _existing_session(session_id="sess-1", refresh_token="rt-1")
        session = session.model_copy(update={"orphaned_session_ids": ["orphan-a", "orphan-b"]})
        state_store.put_session(session)
        client = _FakeAuthClient()

        def _delete_session(session_id, access_token):
            client.calls.append(("delete_session", (session_id, access_token)))
            if session_id == "orphan-a":
                return RevokeResult(confirmed=True)
            return RevokeResult(confirmed=False, message="unknown session")

        client.delete_session = _delete_session  # ty: ignore[invalid-assignment]
        service = _make_service(store, state_store, client)

        result = service.logout(stack=STACK_URL)

        assert result.orphans_revoked == ["orphan-a"]
        assert result.orphans_remaining == ["orphan-b"]
        # Orphan retry uses the session-id primitive with a bearer access
        # token -- never the public token-revoke body contract.
        delete_calls = [c for c in client.calls if c[0] == "delete_session"]
        assert {c[1][0] for c in delete_calls} == {"orphan-a", "orphan-b"}
        assert all(c[1][1] == "old-at" for c in delete_calls)
        # Orphan cleanup must run BEFORE the current session's own refresh
        # token is revoked (delete_session needs the still-live access token).
        revoke_index = [c[0] for c in client.calls].index("revoke")
        delete_indices = [i for i, c in enumerate(client.calls) if c[0] == "delete_session"]
        assert all(i < revoke_index for i in delete_indices)

    def test_orphan_retry_skipped_when_current_session_cannot_produce_a_live_token(
        self, store, state_store
    ) -> None:
        from datetime import UTC, datetime, timedelta

        from keboola_agent_cli.auth.models import StackSession

        now = datetime.now(UTC)
        dead = StackSession(
            stack_url=STACK_URL,
            session_id="sess-1",
            access_token="stale-at",
            refresh_token="rt-1",
            access_expires_at=now - timedelta(days=1),
            refresh_expires_at=now - timedelta(minutes=1),  # refresh token itself expired
            created_at=now - timedelta(days=40),
            orphaned_session_ids=["orphan-a"],
        )
        state_store.put_session(dead)
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)

        result = service.logout(stack=STACK_URL)

        # No live access token could be obtained -> orphan retry skipped, but
        # local cleanup (and the current session's own revoke attempt) still
        # completes.
        assert result.orphans_revoked == []
        assert result.orphans_remaining == ["orphan-a"]
        assert "orphaned session" in result.detail
        assert not any(c[0] == "delete_session" for c in client.calls)
        assert any(c[0] == "revoke" for c in client.calls)
        assert state_store.get_session(STACK_URL) is None

    def test_remove_projects_deletes_only_matching_session_aliases(
        self, store, state_store
    ) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        store.add_project(
            "session-alias",
            ProjectConfig(
                stack_url=STACK_URL, token="kbc-session://555", project_name="P", project_id=555
            ),
        )
        store.add_project(
            "static-alias", ProjectConfig(stack_url=STACK_URL, token=STATIC_TOKEN, project_name="S")
        )
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)

        result = service.logout(stack=STACK_URL, remove_projects=True)

        assert result.removed_projects == ["session-alias"]
        assert store.get_project("session-alias") is None
        assert store.get_project("static-alias") is not None

    def test_resets_provider_registry(self, store, state_store, monkeypatch) -> None:
        state_store.put_session(_existing_session(session_id="sess-1", refresh_token="rt-1"))
        client = _FakeAuthClient()
        service = _make_service(store, state_store, client)
        calls: list[None] = []
        monkeypatch.setattr(svc_mod, "reset_provider_registry", lambda: calls.append(None))

        service.logout(stack=STACK_URL)

        assert calls == [None]
