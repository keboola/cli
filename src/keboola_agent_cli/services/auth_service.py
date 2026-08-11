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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import sleep as _time_sleep
from typing import Any

from ..auth.auth_client import AuthClient
from ..auth.device import run_device_flow
from ..auth.environment import BrowserEnvironment, detect_browser_environment, open_browser
from ..auth.models import CliTokenResponse, DeviceAuthorization, MfaChallengeResult, StackSession
from ..auth.pkce import (
    PkceAuthorizationError,
    PkceCallbackServer,
    PkceSetupError,
    PkceStateMismatch,
    generate_pkce_challenge,
)
from ..auth.sentinel import is_session_token
from ..auth.state_store import AuthStateStore
from ..auth.token_provider import SessionTokenProvider, reset_provider_registry
from ..auth.totp import compute_totp_code
from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import normalize_stack_url
from ._auth_registration import (
    SESSION_UNSUPPORTED_FEATURES,
    ProjectCandidate,
    ProjectCandidatesResult,
    ProjectSelection,
    RegisteredProject,
    RegisterProjectsResult,
    apply_selections,
    build_candidates,
    default_unsupported_features,
    require_single_selection_mode,
    resolve_selections,
)

# Error codes from `provider.introspect()` (via `AuthClient.refresh`) that mean
# "could not reach the auth service", as opposed to a definitive answer about
# the session itself -- `auth status` reports these as "degraded" (offline,
# falling back to on-disk expiry data) rather than misreporting a live
# session as expired just because the network happened to be down.
_NETWORK_ERROR_CODES = frozenset(
    {ErrorCode.TIMEOUT, ErrorCode.CONNECTION_ERROR, ErrorCode.RETRY_EXHAUSTED}
)


def _iso(value: datetime | None) -> str:
    """Format a datetime as ISO-8601, or "" when absent."""
    return value.isoformat() if value is not None else ""


def _noop_notice(_message: str) -> None:
    """Default `on_notice`: a caller that wants no progress narration."""


def _noop_device_prompt(_authorization: DeviceAuthorization) -> None:
    """Default `on_device_prompt`: a caller that renders the code itself, or not at all."""


def _is_browser_safe_url(url: str) -> bool:
    """True for a URL that may be handed to the platform's browser opener.

    `verificationUriComplete` arrives from the stack and goes to
    `webbrowser.open` (or a platform command), which honours whatever scheme it
    is given -- `file://`, a registered custom-scheme handler, or a leading `-`
    that the underlying command reads as a flag. The stack URL itself is held to
    `https://` by `normalize_stack_url`; a value the stack supplies gets no more
    trust than that.
    """
    return url.startswith("https://")


@dataclass(frozen=True)
class _LiveAccessToken:
    """Outcome of the best-effort live-token fetch that orphan cleanup needs.

    `token` is None exactly when `unavailable_reason` explains why the current
    session could not produce one.
    """

    token: str | None
    unavailable_reason: str


@dataclass(frozen=True)
class _OrphanRetryOutcome:
    """Which recorded orphan sessions the server confirmed gone, and which it did not."""

    revoked: list[str]
    remaining: list[str]


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
    # What a session-backed project cannot do. A property of the session this
    # login just created, so it is always populated; the human renderer only
    # prints it once projects have actually been registered.
    session_unsupported_features: list[str] = field(default_factory=default_unsupported_features)


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
        notice = on_notice or _noop_notice
        prompt = on_device_prompt or _noop_device_prompt
        warnings: list[str] = []
        fallback_reason = ""
        method = "device" if device_code else "pkce"

        if method == "pkce":
            environment = self._browser_env_detector()
            if not environment.loopback_browser_usable:
                method = "device"
                fallback_reason = environment.reason
                notice(f"Using device login: {environment.reason}")

        with self._auth_client_factory(stack_url) as client:
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
                    client,
                    on_prompt=self._wrap_device_prompt(prompt, notice),
                    sleep=self._sleep,
                )
                tokens = outcome.tokens

            if tokens is None:  # pragma: no cover - defensive, unreachable by construction
                raise KeboolaApiError(
                    "Login flow completed without producing a token pair.",
                    error_code=ErrorCode.API_ERROR,
                )
            return self._finalize_login(
                client,
                stack_url,
                tokens,
                method=method,
                fallback_reason=fallback_reason,
                register_projects=register_projects,
                warnings=warnings,
            )

    def login_password(
        self,
        *,
        stack: str | None = None,
        email: str,
        password: str,
        totp_secret: str | None = None,
        register_projects: bool = False,
    ) -> LoginResult:
        """Password-grant login -- the unattended, CI-safe alternative to `login()`.

        Never opens a browser and completes entirely over HTTP, so it is
        safe to run from a secret-backed CI workflow (unlike `login()`,
        which requires a human at a browser or device). `totp_secret` (the
        account's base32 TOTP seed) resolves an MFA challenge for an account
        with TOTP-based MFA configured; WebAuthn-only accounts cannot use
        this method -- that ceremony needs a browser, which is exactly what
        this path exists to avoid.

        The code is computed here, immediately before the MFA request, not
        by the caller before `login_password` was even invoked: the login
        round trip through `_do_request`'s retry loop can itself take up to
        ~90s (3 attempts, 30s read timeout, backoff), and a code computed
        before it can drift out of the server's TOTP tolerance window by the
        time it would be submitted. See PR #565 review (C3).

        The rest of the algorithm (session persistence, best-effort revoke
        of the session it replaces, introspection, optional project
        registration) is identical to `login()` -- see `_finalize_login`.
        """
        stack_url = self._resolve_stack_url(stack)
        warnings: list[str] = []
        with self._auth_client_factory(stack_url) as client:
            result = client.login_password(email, password)
            if isinstance(result, MfaChallengeResult):
                if result.mfa_type != "totp":
                    raise KeboolaApiError(
                        f"This account requires MFA type {result.mfa_type!r}, which "
                        "password-grant login cannot resolve without a browser -- use "
                        "`kbagent auth login` instead.",
                        error_code=ErrorCode.AUTH_MFA_INVALID,
                        retryable=False,
                    )
                if not totp_secret:
                    raise ConfigError(
                        "This account requires a TOTP code to sign in -- pass "
                        "--totp-secret (kbagent computes the code from it)."
                    )
                try:
                    totp_code = compute_totp_code(totp_secret)
                except ValueError as exc:
                    raise ConfigError(f"--totp-secret: {exc}") from exc
                tokens = client.verify_mfa_totp(result.mfa_token, totp_code)
            else:
                tokens = result
            return self._finalize_login(
                client,
                stack_url,
                tokens,
                method="password",
                fallback_reason="",
                register_projects=register_projects,
                warnings=warnings,
            )

    def _finalize_login(
        self,
        client: AuthClient,
        stack_url: str,
        tokens: CliTokenResponse,
        *,
        method: str,
        fallback_reason: str,
        register_projects: bool,
        warnings: list[str],
    ) -> LoginResult:
        """Shared tail of every login method: persist, best-effort revoke the
        session this replaces, introspect, optionally register projects."""
        now = datetime.now(UTC)
        previous = self._state_store.get_session(stack_url)
        new_session = StackSession(
            stack_url=stack_url,
            session_id=tokens.session_id,
            user_email=tokens.user.email if tokens.user else "",
            user_name=tokens.user.name if tokens.user else "",
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_expires_at=now + timedelta(seconds=tokens.expires_in),
            # Read through the shared model helper, not hardcoded None: no
            # backend sends a refresh expiry today, but if one starts,
            # honouring it here (rather than only on the first refresh)
            # keeps login and rotation consistent. See
            # `CliTokenResponse.refresh_expiry` for why nothing is guessed
            # when the field is absent.
            refresh_expires_at=tokens.refresh_expiry(now=now),
            created_at=now,
            # An orphan is a session this CLI failed to revoke server-side,
            # and the only record that it exists. `put_session` replaces the
            # whole per-stack row, so a login that did not carry the list
            # forward would drop every orphan older than the session it is
            # replacing -- leaving a live session no `auth logout` can ever
            # reach, while telling the user logout would retry it.
            orphaned_session_ids=list(previous.orphaned_session_ids) if previous else [],
        )

        # Durable first, always -- never delete the old credentials before
        # the new ones are safely on disk (review B-1).
        self._state_store.put_session(new_session)

        replaced_session_id = ""
        orphaned_session_id = ""
        if previous is not None and previous.session_id != new_session.session_id:
            replaced_session_id = previous.session_id
            revoke_result = client.revoke(previous.refresh_token, token_type_hint="refreshToken")
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
            # Build candidates from the introspection this call already
            # holds -- introspecting a second time (e.g. via
            # `register_projects`) would be a redundant network round
            # trip against a session that was only just minted.
            candidates = self.candidates_from_projects(stack_url, accessible_projects)
            selections = [ProjectSelection(project_id=c.project_id) for c in candidates]
            registered_projects = apply_selections(
                self._config_store,
                stack_url,
                {c.project_id: c for c in candidates},
                selections,
                warnings,
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

    def _wrap_device_prompt(
        self,
        prompt: Callable[[DeviceAuthorization], None],
        notice: Callable[[str], None],
    ) -> Callable[[DeviceAuthorization], None]:
        """Wrap the caller's display callback with the best-effort browser open.

        The caller (``commands/auth.py``) owns *printing* the mandatory
        verificationUri + userCode; this service owns *opening*
        verificationUriComplete, since that is the injectable
        ``browser_opener`` seam the rest of this class already uses.

        A non-https URL is reported and not opened rather than skipped in
        silence: the login still completes from the printed URI + code, so the
        only thing lost is the convenience open, and a stack sending one is
        worth knowing about.
        """

        def _prompt(authorization: DeviceAuthorization) -> None:
            prompt(authorization)
            target = authorization.verification_uri_complete
            if not target:
                return
            if not _is_browser_safe_url(target):
                notice("Not opening the verification link: the stack sent a non-https URL.")
                return
            self._browser_opener(target)

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

    # ------------------------------------------------------------------
    # project candidates / registration
    # ------------------------------------------------------------------

    def candidates_from_projects(
        self, stack_url: str, projects: Sequence[Mapping[str, Any]]
    ) -> list[ProjectCandidate]:
        """Build collision-free registration candidates from accessible-project entries.

        Network-free, so the post-login picker can run against the data
        `login()` already fetched. See `_auth_registration.build_candidates`
        for the default-alias algorithm.
        """
        return build_candidates(self._config_store, stack_url, projects)

    def _introspect_accessible_projects(self, stack_url: str) -> list[dict[str, Any]]:
        """Require a stored session for `stack_url`, then introspect it live.

        Shared by `list_project_candidates` and `register_projects`. Goes
        through a `SessionTokenProvider` (built with this service's own
        injectable `auth_client_factory`, matching `status()`) rather than
        introspecting the stored access token directly -- a healthy session
        routinely has an expired 1 hour access token alongside a valid 30 day
        refresh token, and the provider refreshes first when needed.
        """
        session = self._state_store.get_session(stack_url)
        if session is None:
            raise KeboolaApiError(
                f"No active Keboola session for {stack_url}. Run `kbagent auth login`.",
                error_code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
            )
        provider = SessionTokenProvider(
            stack_url, self._state_store, client_factory=self._auth_client_factory
        )
        introspection = provider.introspect()
        return [
            {"id": project.id, "name": project.name, "role": project.role}
            for project in introspection.projects
        ]

    def list_project_candidates(self, *, stack: str | None = None) -> ProjectCandidatesResult:
        """Resolve the stack, require a stored session, introspect, and build candidates.

        The read path for the interactive picker and `auth register-projects`
        with no selection flags -- never mutates `config.json`.
        """
        stack_url = self._resolve_stack_url(stack)
        projects = self._introspect_accessible_projects(stack_url)
        candidates = self.candidates_from_projects(stack_url, projects)
        return ProjectCandidatesResult(stack_url=stack_url, candidates=candidates)

    def register_projects(
        self,
        *,
        stack: str | None = None,
        select_all: bool = False,
        project_ids: Sequence[int] | None = None,
        alias_overrides: Mapping[int, str] | None = None,
        selections: Sequence[ProjectSelection] | None = None,
    ) -> RegisterProjectsResult:
        """Register accessible projects as local aliases, under one of three selectors.

        Exactly one selector applies and they are alternatives, never layers:
        `select_all` takes every accessible project, `project_ids` takes those
        ids, `selections` takes an explicit list (what the interactive picker
        produces). `alias_overrides` (`--alias ID=ALIAS`) applies in every mode
        and fills in an alias only where the selection does not already carry
        one, so a picker-resolved alias is never overwritten.

        Introspection happens exactly once, here, regardless of the selector:
        the accessible set is needed both to validate the selection and to
        compute default aliases, so no caller should pre-fetch it.

        Raises:
            ConfigError: When zero or more than one selector is given (before
                any network call), or naming the offending id when a selection
                references a project this session cannot access -- validated
                up front, against the full candidate set, so a bad id in a
                batch of N never partially applies the other N-1.
            KeboolaApiError: `SESSION_NOT_FOUND` when no session exists for
                the stack (via `_introspect_accessible_projects`).
        """
        require_single_selection_mode(
            select_all=select_all, project_ids=project_ids, selections=selections
        )
        stack_url = self._resolve_stack_url(stack)
        projects = self._introspect_accessible_projects(stack_url)
        candidates = self.candidates_from_projects(stack_url, projects)
        candidates_by_id = {c.project_id: c for c in candidates}
        resolved = resolve_selections(
            candidates,
            select_all=select_all,
            project_ids=project_ids,
            selections=selections,
            alias_overrides=alias_overrides or {},
        )

        for selection in resolved:
            if selection.project_id not in candidates_by_id:
                raise ConfigError(
                    f"Project {selection.project_id} is not accessible to the current "
                    f"session on {stack_url}. Run 'kbagent auth register-projects' with "
                    "no selection flags to see the accessible project ids."
                )

        warnings: list[str] = []
        registered = apply_selections(
            self._config_store, stack_url, candidates_by_id, resolved, warnings
        )
        return RegisterProjectsResult(
            status="ok",
            stack_url=stack_url,
            registered_projects=registered,
            warnings=warnings,
        )

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

        try:
            with self._auth_client_factory(stack_url) as client:
                orphans_revoked: list[str] = []
                orphans_remaining: list[str] = list(session.orphaned_session_ids)
                orphan_skip_reason = ""
                if session.orphaned_session_ids:
                    live_token = self._try_get_live_access_token(stack_url)
                    orphan_skip_reason = live_token.unavailable_reason
                    if live_token.token is not None:
                        outcome = self._retry_orphans(
                            client, session.orphaned_session_ids, live_token.token
                        )
                        orphans_revoked = outcome.revoked
                        orphans_remaining = outcome.remaining

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
            # Any BearerAuth-backed client elsewhere in this process must stop
            # trusting its cached access token for this stack once the
            # session is gone -- drop the whole process-wide registry (test
            # seam doubles as the production logout hook). Outside the `with`
            # so it still runs when closing the client raises.
            reset_provider_registry()

    def _try_get_live_access_token(self, stack_url: str) -> _LiveAccessToken:
        """Best-effort live access token for orphan cleanup during `logout`.

        Never raises: `logout` must be able to skip orphan retries without
        blocking local cleanup, so a session whose own refresh token already
        expired (or an offline machine) comes back as `token=None` plus the
        reason.
        """
        provider = SessionTokenProvider(
            stack_url, self._state_store, client_factory=self._auth_client_factory
        )
        try:
            return _LiveAccessToken(token=provider.get_access_token(), unavailable_reason="")
        except KeboolaApiError as exc:
            return _LiveAccessToken(token=None, unavailable_reason=exc.message)

    @staticmethod
    def _retry_orphans(
        client: AuthClient, orphan_ids: list[str], access_token: str
    ) -> _OrphanRetryOutcome:
        """Attempt `AuthClient.delete_session` for each recorded orphan id.

        `delete_session` never raises, so this never needs to guard against an
        exception mid-loop.
        """
        revoked: list[str] = []
        remaining: list[str] = []
        for orphan_id in orphan_ids:
            result = client.delete_session(orphan_id, access_token)
            if result.confirmed:
                revoked.append(orphan_id)
            else:
                remaining.append(orphan_id)
        return _OrphanRetryOutcome(revoked=revoked, remaining=remaining)

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
    "SESSION_UNSUPPORTED_FEATURES",
    "AuthClientFactory",
    "AuthService",
    "AuthStatusResult",
    "LoginResult",
    "LogoutResult",
    "ProjectCandidate",
    "ProjectCandidatesResult",
    "ProjectSelection",
    "RegisterProjectsResult",
    "RegisteredProject",
    "default_auth_client_factory",
]
