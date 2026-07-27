"""Business logic for `kbagent auth login` / `auth status` / `auth logout`.

Drives the browser-login flows (PKCE with an automatic device-code fallback,
or a forced device flow) to a durable, revocable `StackSession` in
`auth.json`, and answers the two follow-up questions every login needs:
"is my session still good?" (`status`) and "sign me out" (`logout`).

None of the result dataclasses below carry a token field -- `--json` output
is safe by construction, not by post-hoc filtering (see
docs/programmatic-auth-login-plan.md section 4.5 step 7).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import sleep as _time_sleep
from typing import Any

from ..auth.auth_client import AuthClient
from ..auth.device import run_device_flow
from ..auth.environment import BrowserEnvironment, detect_browser_environment, open_browser
from ..auth.models import AuthProject, CliTokenResponse, DeviceAuthorization, StackSession
from ..auth.pkce import (
    PkceAuthorizationError,
    PkceCallbackServer,
    PkceSetupError,
    PkceStateMismatch,
    generate_pkce_challenge,
)
from ..auth.sentinel import is_session_token, make_session_token
from ..auth.state_store import AuthStateStore
from ..auth.token_provider import SessionTokenProvider, reset_provider_registry
from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig, normalize_stack_url

# Error codes from `provider.introspect()` (via `AuthClient.refresh`) that mean
# "could not reach the auth service", as opposed to a definitive answer about
# the session itself -- `auth status` reports these as "degraded" (offline,
# falling back to on-disk expiry data) rather than misreporting a live
# session as expired just because the network happened to be down.
_NETWORK_ERROR_CODES = frozenset(
    {ErrorCode.TIMEOUT, ErrorCode.CONNECTION_ERROR, ErrorCode.RETRY_EXHAUSTED}
)

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify_project_name(name: str) -> str:
    """Turn a project name into a lowercase, hyphenated alias candidate."""
    return _SLUG_INVALID_CHARS.sub("-", name.strip().lower()).strip("-")


def _iso(value: datetime | None) -> str:
    """Format a datetime as ISO-8601, or "" when absent."""
    return value.isoformat() if value is not None else ""


@dataclass(frozen=True)
class RegisteredProject:
    """Outcome of registering one accessible project as a `kbagent` alias."""

    alias: str
    project_id: int
    project_name: str
    status: str  # "registered" | "exists" | "skipped"
    note: str = ""


@dataclass(frozen=True)
class LoginResult:
    """Result of a completed `kbagent auth login`. Carries no token value."""

    status: str  # "ok"
    method: str  # "pkce" | "device"
    stack_url: str
    session_id: str
    user_email: str
    user_name: str
    access_expires_at: str
    refresh_expires_at: str
    fallback_reason: str
    replaced_session_id: str
    orphaned_session_id: str
    accessible_projects: list[dict[str, Any]]
    registered_projects: list[RegisteredProject]
    warnings: list[str]


@dataclass(frozen=True)
class AuthStatusResult:
    """Result of `kbagent auth status`. Carries no token value."""

    status: str  # "live" | "refreshed" | "degraded" | "expired" | "missing"
    stack_url: str
    session_id: str
    user_email: str
    user_name: str
    access_expires_at: str
    refresh_expires_at: str
    accessible_projects: list[dict[str, Any]]
    orphaned_session_ids: list[str]
    detail: str


@dataclass(frozen=True)
class LogoutResult:
    """Result of `kbagent auth logout`. Carries no token value."""

    status: str  # "ok"
    stack_url: str
    session_id: str
    remote_revoked: bool
    detail: str
    removed_projects: list[str]
    orphans_revoked: list[str]
    orphans_remaining: list[str]


AuthClientFactory = Callable[[str], AuthClient]


def default_auth_client_factory(stack_url: str) -> AuthClient:
    """Construct a real `AuthClient` bound to `stack_url`."""
    return AuthClient(stack_url)


class AuthService:
    """Business logic for the `kbagent auth` command group.

    Every network-facing or environment-sensing dependency is an injectable
    seam so tests never open a real browser, sleep for real, or hit a real
    stack -- matching the DI pattern the rest of the service layer uses
    (``config_store`` + a client factory).
    """

    def __init__(
        self,
        config_store: ConfigStore,
        *,
        auth_client_factory: AuthClientFactory | None = None,
        state_store: AuthStateStore | None = None,
        browser_env_detector: Callable[[], BrowserEnvironment] = detect_browser_environment,
        browser_opener: Callable[[str], bool] = open_browser,
        sleep: Callable[[float], None] = _time_sleep,
    ) -> None:
        self._config_store = config_store
        self._auth_client_factory = auth_client_factory or default_auth_client_factory
        self._state_store = state_store or AuthStateStore.from_config_store(config_store)
        self._browser_env_detector = browser_env_detector
        self._browser_opener = browser_opener
        self._sleep = sleep

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------

    def login(
        self,
        *,
        stack: str | None = None,
        device_code: bool = False,
        register_projects: bool = False,
        on_device_prompt: Callable[[DeviceAuthorization], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> LoginResult:
        """Run the browser-login flow to a durable session, then introspect it.

        Algorithm (docs/programmatic-auth-login-plan.md section 4.5 / auth
        contract section 13), followed literally:

        1. Resolve the target stack (never discover one).
        2. Pick PKCE or device, falling back device-ward on a pre-exchange
           PKCE failure or a headless/remote environment.
        3. Persist the new session BEFORE anything else, THEN best-effort
           revoke the session it replaces (never the other way around).
        4. Introspect the just-minted (guaranteed-live) access token directly
           to list accessible projects -- no refresh is needed for a token
           that was issued moments ago, so this bypasses the process-wide
           `SessionTokenProvider` registry (that registry exists for the
           ~150 Storage/Manage client call sites, not for this one-shot read).
        5. Optionally register each accessible project as a local alias.
        """
        stack_url = self._resolve_stack_url(stack)
        notice = on_notice or (lambda _message: None)
        prompt = on_device_prompt or (lambda _authorization: None)
        warnings: list[str] = []
        fallback_reason = ""
        method = "device" if device_code else "pkce"

        if method == "pkce":
            environment = self._browser_env_detector()
            if not environment.loopback_browser_usable:
                method = "device"
                fallback_reason = environment.reason
                notice(f"Using device login: {environment.reason}")

        client = self._auth_client_factory(stack_url)
        try:
            tokens = None
            if method == "pkce":
                try:
                    tokens = self._perform_pkce(client)
                except PkceStateMismatch as exc:
                    raise KeboolaApiError(
                        str(exc), error_code=ErrorCode.AUTH_STATE_MISMATCH, retryable=False
                    ) from exc
                except PkceAuthorizationError as exc:
                    raise KeboolaApiError(
                        str(exc), error_code=ErrorCode.AUTH_FLOW_DENIED, retryable=False
                    ) from exc
                except PkceSetupError as exc:
                    # Pre-exchange failure only (bind/browser/timeout) -- safe
                    # to restart the whole login through the device flow.
                    fallback_reason = str(exc)
                    notice(f"Falling back to device login: {fallback_reason}")
                    method = "device"

            if method == "device":
                outcome = run_device_flow(
                    client, on_prompt=self._wrap_device_prompt(prompt), sleep=self._sleep
                )
                tokens = outcome.tokens

            if tokens is None:  # pragma: no cover - defensive, unreachable by construction
                raise KeboolaApiError(
                    "Login flow completed without producing a token pair.",
                    error_code=ErrorCode.API_ERROR,
                )
            now = datetime.now(UTC)
            new_session = StackSession(
                stack_url=stack_url,
                session_id=tokens.session_id,
                user_email=tokens.user.email if tokens.user else "",
                user_name=tokens.user.name if tokens.user else "",
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                access_expires_at=now + timedelta(seconds=tokens.expires_in),
                refresh_expires_at=None,
                created_at=now,
            )

            previous = self._state_store.get_session(stack_url)
            # Durable first, always -- never delete the old credentials before
            # the new ones are safely on disk (review B-1).
            self._state_store.put_session(new_session)

            replaced_session_id = ""
            orphaned_session_id = ""
            if previous is not None and previous.session_id != new_session.session_id:
                replaced_session_id = previous.session_id
                revoke_result = client.revoke(
                    previous.refresh_token, token_type_hint="refreshToken"
                )
                if not revoke_result.confirmed:
                    orphaned_session_id = previous.session_id
                    self._state_store.record_orphan(stack_url, previous.session_id)
                    warnings.append(
                        f"Could not confirm revocation of the previous session "
                        f"({previous.session_id}); it may still be active on the "
                        "server. `kbagent auth logout` will retry it."
                    )

            introspection = client.introspect(new_session.access_token)
            accessible_projects = [
                {"id": project.id, "name": project.name, "role": project.role}
                for project in introspection.projects
            ]

            registered_projects: list[RegisteredProject] = []
            if register_projects:
                registered_projects = self._register_projects(
                    stack_url, introspection.projects, warnings
                )

            return LoginResult(
                status="ok",
                method=method,
                stack_url=stack_url,
                session_id=new_session.session_id,
                user_email=new_session.user_email,
                user_name=new_session.user_name,
                access_expires_at=_iso(new_session.access_expires_at),
                refresh_expires_at=_iso(new_session.refresh_expires_at),
                fallback_reason=fallback_reason,
                replaced_session_id=replaced_session_id,
                orphaned_session_id=orphaned_session_id,
                accessible_projects=accessible_projects,
                registered_projects=registered_projects,
                warnings=warnings,
            )
        finally:
            client.close()

    def _wrap_device_prompt(
        self, prompt: Callable[[DeviceAuthorization], None]
    ) -> Callable[[DeviceAuthorization], None]:
        """Wrap the caller's display callback with the best-effort browser open.

        The caller (``commands/auth.py``) owns *printing* the mandatory
        verificationUri + userCode; this service owns *opening*
        verificationUriComplete, since that is the injectable
        ``browser_opener`` seam the rest of this class already uses.
        """

        def _prompt(authorization: DeviceAuthorization) -> None:
            prompt(authorization)
            if authorization.verification_uri_complete:
                self._browser_opener(authorization.verification_uri_complete)

        return _prompt

    def _perform_pkce(self, client: AuthClient) -> CliTokenResponse:
        """Run one PKCE attempt: authorize URL -> browser -> loopback -> exchange.

        Raises `PkceSetupError` / `PkceCallbackTimeout` (fallback-eligible,
        nothing exchanged yet), `PkceStateMismatch` / `PkceAuthorizationError`
        (terminal), or returns the exchanged `CliTokenResponse`. An exchange
        failure after a successful callback propagates as-is (terminal, no
        fallback -- the caller does not catch it as a `PkceSetupError`).
        """
        challenge = generate_pkce_challenge()
        with PkceCallbackServer(expected_state=challenge.state) as server:
            redirect_uri = server.redirect_uri
            authorize_url = client.authorize_url(
                redirect_uri=redirect_uri,
                code_challenge=challenge.code_challenge,
                state=challenge.state,
            )
            self._browser_opener(authorize_url)
            callback = server.wait()
        return client.exchange_pkce_code(
            code=callback.code,
            state=callback.state,
            redirect_uri=redirect_uri,
            code_verifier=challenge.code_verifier,
        )

    def _register_projects(
        self,
        stack_url: str,
        projects: list[AuthProject],
        warnings: list[str],
    ) -> list[RegisteredProject]:
        """Register each accessible project under a slugified alias.

        An existing alias pointing at the SAME project id and stack is left
        untouched (`status="exists"`, no write, no error). An existing alias
        pointing elsewhere is never overwritten (`status="skipped"` + a
        warning) -- this also protects a static-token project sharing the
        same alias name, which must never be silently replaced.
        """
        registered: list[RegisteredProject] = []
        for project in projects:
            alias = _slugify_project_name(project.name) or f"project-{project.id}"
            existing = self._config_store.get_project(alias)
            if existing is not None:
                if existing.project_id == project.id and (
                    normalize_stack_url(existing.stack_url) == stack_url
                ):
                    registered.append(
                        RegisteredProject(
                            alias=alias,
                            project_id=project.id,
                            project_name=project.name,
                            status="exists",
                        )
                    )
                else:
                    note = (
                        f"Alias '{alias}' already points at a different project; not overwritten."
                    )
                    warnings.append(note)
                    registered.append(
                        RegisteredProject(
                            alias=alias,
                            project_id=project.id,
                            project_name=project.name,
                            status="skipped",
                            note=note,
                        )
                    )
                continue

            self._config_store.add_project(
                alias,
                ProjectConfig(
                    stack_url=stack_url,
                    token=make_session_token(project.id),
                    project_name=project.name,
                    project_id=project.id,
                ),
            )
            registered.append(
                RegisteredProject(
                    alias=alias,
                    project_id=project.id,
                    project_name=project.name,
                    status="registered",
                )
            )
        return registered

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, *, stack: str | None = None) -> AuthStatusResult:
        """Report the session health for a stack without lying about it.

        Never introspects the stored access token directly -- a healthy
        session routinely has an expired 1 hour access token alongside a
        valid 30 day refresh token, so a naive introspect would misreport a
        live session as dead (review NB-2). Goes through a
        `SessionTokenProvider` (built with this service's own injectable
        `auth_client_factory`, not the process-wide registry, so this method
        stays fully unit-testable) which refreshes first when needed.
        """
        stack_url = self._resolve_stack_url(stack)
        session = self._state_store.get_session(stack_url)
        if session is None:
            return AuthStatusResult(
                status="missing",
                stack_url=stack_url,
                session_id="",
                user_email="",
                user_name="",
                access_expires_at="",
                refresh_expires_at="",
                accessible_projects=[],
                orphaned_session_ids=[],
                detail="No stored session for this stack. Run `kbagent auth login`.",
            )

        provider = SessionTokenProvider(
            stack_url, self._state_store, client_factory=self._auth_client_factory
        )
        access_token_before = session.access_token
        try:
            introspection = provider.introspect()
        except KeboolaApiError as exc:
            if exc.error_code == ErrorCode.SESSION_EXPIRED:
                return AuthStatusResult(
                    status="expired",
                    stack_url=stack_url,
                    session_id=session.session_id,
                    user_email=session.user_email,
                    user_name=session.user_name,
                    access_expires_at=_iso(session.access_expires_at),
                    refresh_expires_at=_iso(session.refresh_expires_at),
                    accessible_projects=[],
                    orphaned_session_ids=session.orphaned_session_ids,
                    detail=exc.message,
                )
            if exc.error_code in _NETWORK_ERROR_CODES:
                return AuthStatusResult(
                    status="degraded",
                    stack_url=stack_url,
                    session_id=session.session_id,
                    user_email=session.user_email,
                    user_name=session.user_name,
                    access_expires_at=_iso(session.access_expires_at),
                    refresh_expires_at=_iso(session.refresh_expires_at),
                    accessible_projects=[],
                    orphaned_session_ids=session.orphaned_session_ids,
                    detail=(
                        "Could not reach the Keboola auth service to verify the "
                        f"session live; showing locally stored data. {exc.message}"
                    ),
                )
            raise

        current = self._state_store.get_session(stack_url) or session
        rotated = current.access_token != access_token_before
        accessible_projects = [
            {"id": project.id, "name": project.name, "role": project.role}
            for project in introspection.projects
        ]
        return AuthStatusResult(
            status="refreshed" if rotated else "live",
            stack_url=stack_url,
            session_id=introspection.session_id or current.session_id,
            user_email=(introspection.user.email if introspection.user else current.user_email),
            user_name=(introspection.user.name if introspection.user else current.user_name),
            access_expires_at=_iso(current.access_expires_at),
            refresh_expires_at=_iso(current.refresh_expires_at),
            accessible_projects=accessible_projects,
            orphaned_session_ids=current.orphaned_session_ids,
            detail="",
        )

    # ------------------------------------------------------------------
    # logout
    # ------------------------------------------------------------------

    def logout(self, *, stack: str | None = None, remove_projects: bool = False) -> LogoutResult:
        """Revoke and clear the local session, reporting revoke uncertainty honestly.

        Local cleanup always proceeds even when a remote call fails or is
        uncertain -- `remote_revoked=False` must be surfaced distinctly
        rather than silently treated as a full success.

        Ordering matters: retrying a recorded orphan (review B-1) needs a
        LIVE access token for the current session, so that must happen
        BEFORE the current session's own refresh token is revoked --
        revoking it first would make obtaining that access token impossible
        for the rest of this call. Order:

        1. Obtain a live access token for the current session (best-effort;
           a failure here -- session already dead, offline -- means orphan
           retries are skipped, not that logout aborts).
        2. Retry each recorded orphan via `AuthClient.delete_session` (kills
           a session BY ID; the only primitive available, since an orphan's
           refresh token was never persisted -- it rotated away with the
           session it belonged to).
        3. Revoke the current session's refresh token (public endpoint,
           body-based contract -- never raises).
        4. Delete the local session from `auth.json` (removes the whole
           `StackSession` row, orphan bookkeeping included).
        5. `reset_provider_registry()`.
        """
        stack_url = self._resolve_stack_url(stack)
        session = self._state_store.get_session(stack_url)
        if session is None:
            raise KeboolaApiError(
                f"No active Keboola session for {stack_url}. Nothing to log out of.",
                error_code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
            )

        client = self._auth_client_factory(stack_url)
        try:
            orphans_revoked: list[str] = []
            orphans_remaining: list[str] = list(session.orphaned_session_ids)
            orphan_skip_reason = ""
            if session.orphaned_session_ids:
                access_token, orphan_skip_reason = self._try_get_live_access_token(stack_url)
                if access_token is not None:
                    orphans_revoked, orphans_remaining = self._retry_orphans(
                        client, session.orphaned_session_ids, access_token
                    )

            revoke_result = client.revoke(session.refresh_token, token_type_hint="refreshToken")
            detail_parts: list[str] = []
            if orphans_remaining and orphan_skip_reason:
                detail_parts.append(
                    f"Could not retry {len(orphans_remaining)} orphaned session(s): "
                    f"{orphan_skip_reason}"
                )
            if not revoke_result.confirmed:
                detail_parts.append(
                    f"Local credentials cleared, but the server session "
                    f"{session.session_id} may still be active"
                    + (f": {revoke_result.message}" if revoke_result.message else ".")
                )
            detail = " ".join(detail_parts)

            self._state_store.delete_session(stack_url)

            removed_projects: list[str] = []
            if remove_projects:
                config = self._config_store.load()
                for alias, project in list(config.projects.items()):
                    if is_session_token(project.token) and (
                        normalize_stack_url(project.stack_url) == stack_url
                    ):
                        self._config_store.remove_project(alias)
                        removed_projects.append(alias)

            return LogoutResult(
                status="ok",
                stack_url=stack_url,
                session_id=session.session_id,
                remote_revoked=revoke_result.confirmed,
                detail=detail,
                removed_projects=removed_projects,
                orphans_revoked=orphans_revoked,
                orphans_remaining=orphans_remaining,
            )
        finally:
            client.close()
            # Any BearerAuth-backed client elsewhere in this process must stop
            # trusting its cached access token for this stack once the
            # session is gone -- drop the whole process-wide registry (test
            # seam doubles as the production logout hook).
            reset_provider_registry()

    def _try_get_live_access_token(self, stack_url: str) -> tuple[str | None, str]:
        """Best-effort live access token for orphan cleanup during `logout`.

        Returns ``(token, "")`` on success or ``(None, reason)`` when the
        current session cannot produce one -- e.g. its own refresh token
        already expired, or the network is down. Never raises: `logout`
        must be able to skip orphan retries without blocking local cleanup.
        """
        provider = SessionTokenProvider(
            stack_url, self._state_store, client_factory=self._auth_client_factory
        )
        try:
            return provider.get_access_token(), ""
        except KeboolaApiError as exc:
            return None, exc.message

    @staticmethod
    def _retry_orphans(
        client: AuthClient, orphan_ids: list[str], access_token: str
    ) -> tuple[list[str], list[str]]:
        """Attempt `AuthClient.delete_session` for each recorded orphan id.

        Returns ``(revoked, remaining)``. `delete_session` never raises, so
        this never needs to guard against an exception mid-loop.
        """
        revoked: list[str] = []
        remaining: list[str] = []
        for orphan_id in orphan_ids:
            result = client.delete_session(orphan_id, access_token)
            if result.confirmed:
                revoked.append(orphan_id)
            else:
                remaining.append(orphan_id)
        return revoked, remaining

    # ------------------------------------------------------------------
    # shared
    # ------------------------------------------------------------------

    def _resolve_stack_url(self, stack: str | None) -> str:
        """Resolve the stack to log into/out of/inspect. Never discovers one.

        Precedence: an explicit ``--stack`` matching a registered project
        alias wins (an alias is a closed, exact-match set, so it is checked
        before treating the same string as a literal hostname); otherwise an
        explicit ``--stack`` is normalized as a URL; otherwise the default
        project's stack; otherwise a `ConfigError` naming the fix.
        """
        if stack:
            project = self._config_store.get_project(stack)
            if project is not None:
                return normalize_stack_url(project.stack_url)
            try:
                return normalize_stack_url(stack)
            except ValueError as exc:
                raise ConfigError(
                    f"'{stack}' is not a valid stack URL or a registered project alias: {exc}"
                ) from exc

        config = self._config_store.load()
        if config.default_project:
            project = config.projects.get(config.default_project)
            if project is not None:
                return normalize_stack_url(project.stack_url)

        raise ConfigError(
            "No stack to log into -- login is not stack discovery. Pass "
            "--stack <url-or-alias>, or register/select a default project "
            "first (`kbagent project add` / `kbagent project use`)."
        )


__all__ = [
    "AuthClientFactory",
    "AuthService",
    "AuthStatusResult",
    "LoginResult",
    "LogoutResult",
    "RegisteredProject",
    "default_auth_client_factory",
]
