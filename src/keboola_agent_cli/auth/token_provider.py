"""Per-stack access-token cache and httpx auth hook for programmatic-auth sessions.

Two pieces live here:

- `SessionTokenProvider`: caches a stack's live access token in memory and
  refreshes it -- serialized both across threads (a `threading.Lock`) and
  across processes (the cross-platform `filelock` behind
  `AuthStateStore.transaction()`) -- before the 1 hour access token expires.
  A rotated token pair is always persisted to `auth.json` BEFORE it is handed
  to a caller, so a crash between refresh and use can never leave an
  in-flight token that was never written down.
- `BearerAuth`: an `httpx.Auth` hook that stamps `Authorization: Bearer` (and
  optionally `X-KBC-ProjectId`) on every request, and retries a 401 exactly
  once with a force-refreshed token. This is the zero-churn seam: any
  existing `httpx.Client` gains session auth by passing `auth=BearerAuth(...)`
  at construction, without any of the ~150 client-factory call sites changing
  shape (see `http_base.py`'s additive `http_auth` parameter).

See docs/programmatic-auth-login-plan.md section 4.4 for the refresh
algorithm this module implements literally.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import httpx

from ..constants import AUTH_REFRESH_MARGIN
from ..errors import ErrorCode, KeboolaApiError
from ..models import normalize_stack_url
from .models import CliTokenResponse, IntrospectResponse, StackSession
from .state_store import AuthStateStore

if TYPE_CHECKING:
    # Only needed for the type hint on the injectable factory; the real
    # import happens lazily inside `_build_client` so importing this module
    # never drags in `auth_client.py` (and its `httpx` client construction).
    from .auth_client import AuthClient

_LOGIN_REMEDY = "Run `kbagent auth login` to sign in again."

# Process-wide registry of providers, keyed by (auth.json path, normalized
# stack URL) so every caller sharing a config dir + stack shares one cache
# and one in-process lock, instead of each `KeboolaClient` instance
# refreshing independently. Guarded by its own lock (registry mutation is a
# different concern from a single provider's refresh serialization).
_REGISTRY_LOCK = threading.Lock()
_PROVIDER_REGISTRY: dict[tuple[str, str], SessionTokenProvider] = {}


class TokenProvider(Protocol):
    """Minimal interface `BearerAuth` needs from a token cache.

    A `Protocol` (not an ABC) so tests can inject a bare stand-in without
    subclassing `SessionTokenProvider`.
    """

    def get_access_token(self) -> str:
        """Return a live access token, refreshing first if it is stale."""
        ...

    def force_refresh(self, rejected_token: str) -> str:
        """Force a refresh after `rejected_token` was rejected (HTTP 401)."""
        ...


class SessionTokenProvider:
    """Per-stack access-token cache with thread- and process-safe rotation.

    One instance per (auth.json, stack) per process -- see
    `get_session_token_provider`. Parallel multi-project commands run through
    a `ThreadPoolExecutor`, and several kbagent processes can run at once, so
    refresh must be serialized on both axes: a `threading.Lock` inside the
    process and the cross-platform `filelock` on `auth.json.lock` (via
    `AuthStateStore.transaction()`) across processes.
    """

    def __init__(
        self,
        stack_url: str,
        state_store: AuthStateStore,
        client_factory: Callable[[str], AuthClient] | None = None,
    ) -> None:
        self._stack_url = normalize_stack_url(stack_url)
        self._state_store = state_store
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._cached_token: str = ""
        self._cached_expires_at: datetime | None = None

    @property
    def stack_url(self) -> str:
        """The normalized stack URL this provider caches a token for."""
        return self._stack_url

    def get_access_token(self) -> str:
        """Return a live access token, refreshing first if it is stale.

        Implements plan section 4.4 steps 1-8: an in-memory fast path (no
        lock), then a double-checked refresh under the thread lock and the
        cross-process file lock, re-reading `auth.json` before minting a new
        pair in case another process already rotated it.
        """
        if self._cache_is_fresh():  # step 1
            return self._cached_token
        with self._lock:  # step 2
            if self._cache_is_fresh():  # step 3: another thread may have refreshed
                return self._cached_token
            return self._refresh_locked()

    def force_refresh(self, rejected_token: str) -> str:
        """Force a refresh after `rejected_token` was rejected (HTTP 401).

        If the cache already holds a different token, another thread (or
        process, via the step-5 re-read) rotated it first -- return that one
        with no network call. Otherwise invalidate the cache and refresh.

        ``rejected_token`` is passed down so the step-5 "another process
        already rotated" shortcut cannot hand back the very token the server
        just rejected: a token can be revoked long BEFORE its nominal expiry
        (logout elsewhere, password/MFA change, admin cascade), so being
        time-fresh is not evidence of being valid. Without this, a 401 retry
        would replay the same dead credential and fail again.
        """
        with self._lock:
            if self._cached_token and self._cached_token != rejected_token:
                return self._cached_token
            self._cached_token = ""
            self._cached_expires_at = None
            return self._refresh_locked(rejected_token=rejected_token)

    def introspect(self) -> IntrospectResponse:
        """Introspect the session using a guaranteed-live access token.

        `auth status` MUST call this rather than introspecting the stored
        access token directly -- a healthy session routinely has an expired
        1 hour access token alongside a valid 30 day refresh token, so a
        naive introspect of the on-disk token would misreport a live session
        as dead.
        """
        token = self.get_access_token()
        with self._build_client() as client:
            return client.introspect(token)

    def _build_client(self) -> AuthClient:
        if self._client_factory is not None:
            return self._client_factory(self._stack_url)
        # Lazy import: importing token_provider must never drag in the full
        # AuthClient (and its httpx.Client construction) -- see module
        # docstring and the auth-contract import-cycle rules.
        from .auth_client import AuthClient

        return AuthClient(self._stack_url)

    def _cache_is_fresh(self, *, now: datetime | None = None) -> bool:
        if not self._cached_token or self._cached_expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return (self._cached_expires_at - moment).total_seconds() > AUTH_REFRESH_MARGIN

    def _refresh_locked(self, *, rejected_token: str = "") -> str:
        """Steps 4-8 of the refresh algorithm. Caller must hold `self._lock`.

        ``rejected_token`` (set only by `force_refresh`) names an access token
        the server has just rejected, so the step-5 adopt-what-another-process
        persisted shortcut must skip it even when it still looks time-fresh.

        Holds the `auth.json.lock` file lock across the re-read AND the
        network refresh call (deliberately -- this is what serializes
        refresh across processes: a second process blocks here, then adopts
        the pair the first process just persisted instead of minting its
        own). This is a different lock from `ConfigStore`'s `config.json`
        lock, which must never be held across network I/O; that invariant is
        untouched by this module.

        Holding a lock across network I/O is only safe while the hold stays
        shorter than the `AUTH_LOCK_TIMEOUT` every other holder waits, so
        `AuthClient.refresh` is a single attempt bounded by
        `AUTH_REFRESH_MAX_WALL_CLOCK` (< half of `AUTH_LOCK_TIMEOUT`). Without
        that bound a slow auth service would make concurrent processes report
        this lock as stuck -- with a `ConfigError` (exit 5) that names the
        wrong cause.
        """
        with self._state_store.transaction():
            session = self._state_store.get_session(self._stack_url)
            if session is None:
                raise KeboolaApiError(
                    message=(f"No active Keboola session for {self._stack_url}. {_LOGIN_REMEDY}"),
                    error_code=ErrorCode.SESSION_NOT_FOUND,
                    retryable=False,
                )

            # Step 5: another PROCESS may have already rotated this pair.
            # Never adopt a token the server just rejected, however fresh its
            # nominal expiry looks -- revocation is not expiry.
            if session.access_token_fresh() and session.access_token != rejected_token:
                self._cached_token = session.access_token
                self._cached_expires_at = session.access_expires_at
                return self._cached_token

            # Step 6: a refresh token past its known expiry never round-trips.
            if session.refresh_token_expired():
                self._state_store.delete_session(self._stack_url)
                raise KeboolaApiError(
                    message=(f"Your Keboola login for {self._stack_url} expired. {_LOGIN_REMEDY}"),
                    error_code=ErrorCode.SESSION_EXPIRED,
                    retryable=False,
                )

            return self._perform_refresh(session)

    def _perform_refresh(self, session: StackSession) -> str:
        """Step 7: the network refresh call, plus persist-before-use (step 7a/b)."""
        with self._build_client() as client:
            try:
                tokens = client.refresh(session.refresh_token)
            except KeboolaApiError as exc:
                if exc.error_code == ErrorCode.SESSION_EXPIRED:
                    # Family-revoked / invalid_grant: purge before re-raising
                    # so a stale, unusable session never lingers in auth.json.
                    self._state_store.delete_session(self._stack_url)
                raise

        now = datetime.now(UTC)
        updated = session.model_copy(
            update={
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "access_expires_at": now + timedelta(seconds=tokens.expires_in),
                "refresh_expires_at": self._resolve_refresh_expiry(tokens, session),
                "session_id": tokens.session_id or session.session_id,
            }
        )
        # Persist BEFORE updating the in-memory cache / returning to the
        # caller -- a crash right after this line still leaves a durable,
        # usable pair on disk.
        self._state_store.put_session(updated)
        self._cached_token = updated.access_token
        self._cached_expires_at = updated.access_expires_at
        return self._cached_token

    @staticmethod
    def _resolve_refresh_expiry(tokens: CliTokenResponse, session: StackSession) -> datetime | None:
        """Refresh-token expiry from the response when present, else the previous value.

        The wire-shape question (which key, what unit, is it even sent) belongs
        to `CliTokenResponse.refresh_expiry`, shared with `AuthService.login`.
        The only rule this method adds is rotation-specific: a response without
        the field keeps the expiry already on record instead of clearing it,
        since a rotation does not shorten a refresh-token family's lifetime.
        """
        expiry = tokens.refresh_expiry()
        return expiry if expiry is not None else session.refresh_expires_at


class BearerAuth(httpx.Auth):
    """httpx auth hook that stamps `Authorization: Bearer` + `X-KBC-ProjectId`.

    Generator-based (the `httpx.Auth` contract) so a 401 response can be
    retried exactly once with a force-refreshed token -- the seam that lets
    every existing client gain session auth without touching the ~150
    `(stack_url, token) -> KeboolaClient` factory call sites.
    """

    def __init__(self, provider: TokenProvider, project_id: int | None = None) -> None:
        self._provider = provider
        self._project_id = project_id

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._provider.get_access_token()
        self._stamp(request, token)
        response = yield request
        if response.status_code == 401:
            token = self._provider.force_refresh(token)
            self._stamp(request, token)
            yield request

    def _stamp(self, request: httpx.Request, token: str) -> None:
        request.headers["Authorization"] = f"Bearer {token}"
        if self._project_id is not None:
            request.headers["X-KBC-ProjectId"] = str(self._project_id)


def get_session_token_provider(stack_url: str, state_store: AuthStateStore) -> SessionTokenProvider:
    """Return the process-wide provider for (state_store.state_path, normalized stack_url).

    Sharing one provider per (auth.json, stack) across every caller in the
    process is what makes the in-process `threading.Lock` in
    `SessionTokenProvider` actually serialize concurrent refreshes -- a fresh
    provider per call site would each hold its own lock and refresh
    independently.
    """
    key = (str(state_store.state_path), normalize_stack_url(stack_url))
    with _REGISTRY_LOCK:
        provider = _PROVIDER_REGISTRY.get(key)
        if provider is None:
            provider = SessionTokenProvider(stack_url, state_store)
            _PROVIDER_REGISTRY[key] = provider
        return provider


def reset_provider_registry() -> None:
    """Drop all cached providers. Test-only seam; also used by `auth logout`."""
    with _REGISTRY_LOCK:
        _PROVIDER_REGISTRY.clear()
