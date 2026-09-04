"""Per-stack access-token cache and httpx auth hook for programmatic-auth sessions.

Two pieces live here:

- `SessionTokenProvider`: caches a stack's live access token in memory and
  refreshes it before the 1 hour access token expires, serialized across
  threads by a `threading.Lock` and across processes by a **refresh lease**
  recorded in `auth.json`. The cross-platform `filelock` behind
  `AuthStateStore.transaction()` protects each read and write of that file, but
  is never held across the network call -- the lease is what keeps a refresh
  token from being presented to the server by two requests at once, which is
  the replay that triggers server-side family revocation.
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

import os
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import httpx

from ..constants import (
    AUTH_REFRESH_ABANDON_GRACE,
    AUTH_REFRESH_LEASE_TTL,
    AUTH_REFRESH_MARGIN,
    AUTH_REFRESH_MAX_WALL_CLOCK,
    AUTH_REFRESH_POLL_INTERVAL,
    AUTH_REFRESH_WAIT_TIMEOUT,
)
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


class _AbandonedRefresh(KeboolaApiError):
    """The wall-clock ceiling fired; the request may still be in flight.

    A subclass rather than a flag so it stays a `KeboolaApiError` (callers and
    the CLI error mapping are unchanged) while the one caller that must tell this
    apart -- the lease holder, which may not release a claim protecting a token
    that is still on the wire -- can catch it precisely.
    """


@dataclass(frozen=True)
class _LeaseOutcome:
    """What one pass of the claim loop established.

    Exactly one of these three states: ``token`` set (another process already
    rotated; adopt it), ``session`` set (the lease is ours; refresh it), or both
    empty (somebody else holds a live lease; wait).
    """

    token: str = ""
    session: StackSession | None = None


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
    process, and a **refresh lease** in `auth.json` across processes. The
    cross-platform `filelock` on `auth.json.lock` protects each read/write of
    that file; it is never held across the network call (see `_refresh_locked`).
    """

    def __init__(
        self,
        stack_url: str,
        state_store: AuthStateStore,
        client_factory: Callable[[str], AuthClient] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stack_url = normalize_stack_url(stack_url)
        self._state_store = state_store
        self._client_factory = client_factory
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached_token: str = ""
        self._cached_expires_at: datetime | None = None
        # Identifies THIS provider's lease claims. Process id alone would be
        # ambiguous: a pid is reused after the process exits, so a recycled pid
        # could release or renew a lease it never took.
        self._holder_id = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"

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

    def peek_access_token(self) -> str | None:
        """Return a currently-fresh access token WITHOUT ever refreshing.

        Best-effort callers (usage telemetry) use this so they never trigger the
        network refresh or the cross-process lease wait that `get_access_token`
        can. It reads only the in-memory cache and, failing that, the persisted
        session under the brief local file lock -- never the network, never the
        refresh lease. Returns None when only a refresh would produce a usable
        token, so the caller skips its work rather than block.
        """
        if self._cache_is_fresh():
            return self._cached_token
        with self._state_store.transaction():
            session = self._state_store.get_session(self._stack_url)
        if session is not None and session.access_token_fresh():
            return session.access_token
        return None

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

        Cross-process serialization is a **refresh lease**, not the file lock:
        `auth.json.lock` is held only for the local read and the local write, and
        never across the network call. The invariant being protected is that a
        given refresh token is presented to the server by at most one request at
        a time -- a second, concurrent presentation is the replay that triggers
        server-side family revocation, i.e. a hard logout.

        Holding the file lock across the request would enforce that too, but at
        the cost of an unbounded hold: every other process, including a read-only
        `auth status`, would wait `AUTH_LOCK_TIMEOUT` and then report the lock as
        stuck (`ConfigError`, exit 5) whenever the auth service was merely slow.
        The lease keeps the invariant without that hold, and expires on its own so
        a crashed or abandoned holder self-heals.

        A caller that loses the lease polls until the holder persists a rotated
        pair -- which the step-5 check below then adopts -- or until
        `AUTH_REFRESH_WAIT_TIMEOUT` runs out.
        """
        deadline = self._monotonic() + AUTH_REFRESH_WAIT_TIMEOUT
        while True:
            outcome = self._claim_lease_or_adopt(rejected_token=rejected_token)
            if outcome.token:
                return outcome.token
            if outcome.session is not None:
                return self._refresh_as_lease_holder(outcome.session)
            if self._monotonic() >= deadline:
                raise KeboolaApiError(
                    message=(
                        f"Another kbagent process is still refreshing your Keboola login "
                        f"for {self._stack_url} (waited "
                        f"{AUTH_REFRESH_WAIT_TIMEOUT:.0f}s). Run the command again."
                    ),
                    status_code=0,
                    error_code=ErrorCode.TIMEOUT,
                    retryable=True,
                )
            self._sleep(AUTH_REFRESH_POLL_INTERVAL)

    def _claim_lease_or_adopt(self, *, rejected_token: str) -> _LeaseOutcome:
        """One pass of steps 5-6 plus the lease claim, under the file lock.

        Returns a token to adopt, or the session to refresh once the lease is
        ours, or neither when somebody else holds a live lease.
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
                return _LeaseOutcome(token=session.access_token)

            # Step 6: a refresh token past its known expiry never round-trips.
            if session.refresh_token_expired():
                self._state_store.delete_session(self._stack_url)
                raise KeboolaApiError(
                    message=(f"Your Keboola login for {self._stack_url} expired. {_LOGIN_REMEDY}"),
                    error_code=ErrorCode.SESSION_EXPIRED,
                    retryable=False,
                )

            if self._state_store.claim_refresh_lease(
                self._stack_url, holder=self._holder_id, ttl=AUTH_REFRESH_LEASE_TTL
            ):
                return _LeaseOutcome(session=session)
            return _LeaseOutcome()

    def _refresh_as_lease_holder(self, session: StackSession) -> str:
        """Run the network refresh while holding the lease, then release it.

        The lease is released on every outcome EXCEPT an abandoned request: there
        the token may still be travelling, so the claim is extended by the short
        `AUTH_REFRESH_ABANDON_GRACE` instead. That pause keeps the next command
        from stampeding straight back into a request we have only just walked
        away from; it deliberately does not try to outlast the server's grace
        window, because a presentation past that window is what turns a forgiven
        replay into family revocation.

        Every other outcome releases: a request that reached a verdict -- success,
        a server error, a body the decoder choked on, an interrupt -- is no longer
        in flight, so holding the claim would only stall the next command for the
        rest of the TTL. The release is therefore keyed on "not abandoned" rather
        than on a list of exception types, because the failure that matters here
        is the one nobody anticipated: an exception outside the expected set would
        otherwise leave a claim standing that no process can release.
        """
        try:
            token = self._perform_refresh(session)
        except _AbandonedRefresh:
            self._state_store.extend_refresh_lease(
                self._stack_url, holder=self._holder_id, ttl=AUTH_REFRESH_ABANDON_GRACE
            )
            raise
        except BaseException:
            self._state_store.release_refresh_lease(self._stack_url, holder=self._holder_id)
            raise
        self._state_store.release_refresh_lease(self._stack_url, holder=self._holder_id)
        return token

    def _refresh_within_budget(self, client: AuthClient, refresh_token: str) -> CliTokenResponse:
        """Issue the refresh request under a hard wall-clock ceiling.

        `AuthClient.refresh` already carries short per-phase httpx timeouts, but
        httpx applies `read` / `write` per I/O operation and has no
        total-duration option, so a server that trickles a response stays inside
        them for as long as it likes. Without a ceiling, one unresponsive auth
        service would keep a refresh lease claimed until it expired and stall
        every command against this stack for that long.

        A request still running at the deadline is abandoned rather than awaited,
        and reported as `_AbandonedRefresh` so the lease holder knows to keep the
        claim rather than release it -- the token may still be travelling, and a
        second presentation of it is what family revocation punishes.

        Abandoning is genuinely abandoning: `httpx.Client.close()` returns at once
        but does NOT abort a request already in flight (measured, httpx 0.28.1),
        so the worker outlives this call until its own per-phase timeout fires. It
        is a daemon thread, so it can never delay interpreter exit, and it touches
        no shared state -- persistence stays on the calling thread below. In a
        short CLI invocation that is the end of it; in a long-running `kbagent
        serve` process, repeated stalls against the same unresponsive auth service
        can leave several such workers parked until they time out.

        A worker paused between `refresh`'s contention attempts ends here too, and
        deliberately so: `_perform_refresh` closes the client as this call unwinds,
        so the pending replay raises instead of reaching the wire. That is the
        outcome we want -- this thread has already given up and extended the lease
        precisely so no second presentation happens -- and the worker's outcome,
        whatever it turns out to be, is discarded unread below.

        Second cost: the server may have rotated a pair that is then discarded,
        leaving the on-disk refresh token one generation stale. That is the same
        state any lost response leaves behind, and the server's idempotent grace
        window is what forgives it (see `AuthClient.refresh`) -- provided the
        retry lands inside a window that started when the server rotated, which
        is why `AUTH_REFRESH_ABANDON_GRACE` holds the lease only briefly.
        """
        rotated: list[CliTokenResponse] = []
        failure: list[BaseException] = []

        def _run() -> None:
            try:
                rotated.append(client.refresh(refresh_token))
            except BaseException as exc:  # re-raised on the caller's thread below
                failure.append(exc)

        worker = threading.Thread(target=_run, name="kbagent-auth-refresh", daemon=True)
        worker.start()
        worker.join(AUTH_REFRESH_MAX_WALL_CLOCK)
        if worker.is_alive():
            raise _AbandonedRefresh(
                message=(
                    f"Refreshing your Keboola login at {self._stack_url} exceeded its "
                    f"{AUTH_REFRESH_MAX_WALL_CLOCK:.0f}s budget. Run the command again."
                ),
                status_code=0,
                error_code=ErrorCode.TIMEOUT,
                retryable=True,
            )
        if failure:
            raise failure[0]
        return rotated[0]

    def _perform_refresh(self, session: StackSession) -> str:
        """Step 7: the network refresh call, plus persist-before-use (step 7a/b)."""
        with self._build_client() as client:
            try:
                tokens = self._refresh_within_budget(client, session.refresh_token)
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


class StaticBearerAuth(httpx.Auth):
    """Stamp a fixed bearer token and project id, and never refresh.

    Best-effort callers (usage telemetry) use this instead of `BearerAuth` so a
    401 can never trigger the network force-refresh `BearerAuth` runs on a
    rejected token. A fire-and-forget event that gets a 401 simply fails, which
    is the correct outcome -- it must not make the command wait on the network.
    """

    def __init__(self, token: str, project_id: int | None = None) -> None:
        self._token = token
        self._project_id = project_id

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        if self._project_id is not None:
            request.headers["X-KBC-ProjectId"] = str(self._project_id)
        yield request


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
