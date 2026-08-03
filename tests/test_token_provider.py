"""Tests for `SessionTokenProvider` and `BearerAuth` (0.80.0).

A programmatic session's refresh token is **rotated on every refresh**, with a
30-second idempotent grace window and server-side *family revocation* on
replay. Reusing a stale refresh token after that window logs the user out hard.
Everything in this file exists to protect that invariant:

- refresh is serialized across threads (one `threading.Lock` per provider, and
  one provider per (auth.json, stack) via the process registry),
- refresh is serialized across processes (a real cross-platform `filelock` on
  `auth.json.lock`, because `ConfigStore`'s `fcntl` helper is a no-op on
  Windows and this project ships a Windows wheel),
- a rotated pair is persisted **before** it is handed to a caller,
- a process that blocks on the file lock **re-reads** `auth.json` afterwards and
  adopts whatever the winner persisted, instead of minting a second pair,
- a refresh that fails inside the file lock releases it, keeps the still-valid
  session on disk, and surfaces a network error rather than the misleading
  "another kbagent process may be stuck holding it".

The cross-process test at the bottom uses real spawned processes. What it
proves and what it deliberately does not is documented on the test itself.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import filelock
import httpx
import pytest

from keboola_agent_cli.auth import token_provider as tp_mod
from keboola_agent_cli.auth.auth_client import AuthClient
from keboola_agent_cli.auth.models import CliTokenResponse, RefreshLease, StackSession
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.auth.token_provider import (
    BearerAuth,
    SessionTokenProvider,
    get_session_token_provider,
    reset_provider_registry,
)
from keboola_agent_cli.constants import (
    AUTH_REFRESH_ABANDON_GRACE,
    AUTH_REFRESH_LEASE_MAX_HORIZON,
    AUTH_REFRESH_LEASE_TTL,
    AUTH_REFRESH_MAX_WALL_CLOCK,
    AUTH_REFRESH_WAIT_TIMEOUT,
)
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

STACK_URL = "https://connection.keboola.com"


class _FakeAuthClient(AuthClient):
    """Stand-in for `AuthClient` that counts refreshes instead of doing HTTP.

    Subclasses `AuthClient` (rather than duck-typing an unrelated class) so it
    type-checks against `SessionTokenProvider`'s `client_factory:
    Callable[[str], AuthClient]` signature. `__init__` deliberately skips
    `super().__init__()` -- no real `httpx.Client` / network stack is built.

    Used as a context manager because `SessionTokenProvider` builds its client
    with `with self._build_client() as client:`.
    """

    def __init__(
        self,
        *,
        counter: list[str] | None = None,
        raises: Exception | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self.calls: list[str] = counter if counter is not None else []
        self._raises = raises
        self._lock = lock or threading.Lock()
        self._serial = 0

    def __enter__(self) -> _FakeAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def close(self) -> None:
        return None

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        with self._lock:
            self.calls.append(refresh_token)
            self._serial += 1
            serial = self._serial
        if self._raises is not None:
            raise self._raises
        return CliTokenResponse(
            accessToken=f"kbc_at_rotated_{serial}",
            refreshToken=f"kbc_rt_rotated_{serial}",
            expiresIn=3600,
            sessionId="session-1",
        )


def _seed_session(
    store: AuthStateStore,
    *,
    access_expires_in: int,
    refresh_expires_in: int | None = 30 * 24 * 3600,
    access_token: str = "kbc_at_original",
) -> StackSession:
    """Persist a session whose access token expires in `access_expires_in` seconds."""
    now = datetime.now(UTC)
    session = StackSession(
        stack_url=STACK_URL,
        session_id="session-1",
        user_email="user@example.com",
        user_name="Test User",
        access_token=access_token,
        refresh_token="kbc_rt_original",
        access_expires_at=now + timedelta(seconds=access_expires_in),
        refresh_expires_at=(
            now + timedelta(seconds=refresh_expires_in) if refresh_expires_in is not None else None
        ),
        created_at=now,
    )
    store.put_session(session)
    return session


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """The provider registry is process-global; keep tests isolated."""
    reset_provider_registry()
    yield
    reset_provider_registry()


# ----------------------------------------------------------------------------
# Proactive refresh (AUTH_REFRESH_MARGIN boundary)
# ----------------------------------------------------------------------------


class TestProactiveRefresh:
    def test_fresh_access_token_is_reused_without_network(self, tmp_path: Path) -> None:
        """Comfortably inside the margin -> no refresh call at all."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        assert provider.get_access_token() == "kbc_at_original"
        assert fake.calls == []

    def test_token_inside_refresh_margin_is_refreshed(self, tmp_path: Path) -> None:
        """Within AUTH_REFRESH_MARGIN (120s) of expiry -> refresh before handing it out.

        A token that is technically still valid but about to expire would die
        mid-flight on a slow request; refreshing early is what avoids that.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=60)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        assert provider.get_access_token() == "kbc_at_rotated_1"
        assert fake.calls == ["kbc_rt_original"]

    def test_rotated_pair_is_persisted_before_being_returned(self, tmp_path: Path) -> None:
        """Persist-before-use: a crash after the return must not lose the pair."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        token = provider.get_access_token()

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.access_token == token == "kbc_at_rotated_1"
        assert persisted.refresh_token == "kbc_rt_rotated_1"


# ----------------------------------------------------------------------------
# Refresh-token expiry propagation
# ----------------------------------------------------------------------------


class _RefreshExpiryAuthClient(AuthClient):
    """Fake whose refresh response carries a server-sent `refreshExpiresIn`.

    Built via `model_validate` (not kwargs) so the test exercises the real wire
    shape an `extra="allow"` field arrives in.
    """

    def __init__(self, *, refresh_expires_in: int) -> None:
        self._refresh_expires_in = refresh_expires_in
        self.calls: list[str] = []

    def __enter__(self) -> _RefreshExpiryAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def close(self) -> None:
        return None

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(refresh_token)
        return CliTokenResponse.model_validate(
            {
                "accessToken": "kbc_at_rotated_1",
                "refreshToken": "kbc_rt_rotated_1",
                "expiresIn": 3600,
                "sessionId": "session-1",
                "refreshExpiresIn": self._refresh_expires_in,
            }
        )


class TestRefreshExpiryPropagation:
    """A rotation must neither invent a refresh expiry nor drop the one on record.

    No deployment sends `refreshExpiresIn` today, so the first test below covers
    every real rotation happening in production: the expiry already stored stays
    put. The second pins that the field IS honoured the moment a backend starts
    sending it -- the same helper `AuthService.login` now uses, so the two cannot
    disagree.
    """

    def test_absent_field_keeps_the_expiry_already_on_record(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        seeded = _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        provider.get_access_token()

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.refresh_token == "kbc_rt_rotated_1"  # rotation did happen
        assert persisted.refresh_expires_at == seeded.refresh_expires_at

    def test_server_sent_seconds_replace_the_stored_expiry(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10, refresh_expires_in=60)
        fake = _RefreshExpiryAuthClient(refresh_expires_in=7 * 24 * 3600)

        before = datetime.now(UTC)
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        provider.get_access_token()

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.refresh_expires_at is not None
        assert persisted.refresh_expires_at >= before + timedelta(days=7)
        assert persisted.refresh_expires_at <= datetime.now(UTC) + timedelta(days=7)


# ----------------------------------------------------------------------------
# Terminal states
# ----------------------------------------------------------------------------


class TestTerminalStates:
    def test_missing_session_raises_session_not_found(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()

        assert exc_info.value.error_code == ErrorCode.SESSION_NOT_FOUND
        assert "auth login" in exc_info.value.message
        assert fake.calls == []

    def test_expired_refresh_token_short_circuits_without_network(self, tmp_path: Path) -> None:
        """A refresh token past its known expiry never round-trips to the server."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10, refresh_expires_in=-5)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()

        assert exc_info.value.error_code == ErrorCode.SESSION_EXPIRED
        assert fake.calls == []
        assert store.get_session(STACK_URL) is None

    def test_family_revoked_purges_session_and_raises(self, tmp_path: Path) -> None:
        """invalid_grant / family revoke -> purge auth.json, do not leave a dead session."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient(
            raises=KeboolaApiError(
                "Your Keboola login expired or was revoked.",
                error_code=ErrorCode.SESSION_EXPIRED,
            )
        )

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()

        assert exc_info.value.error_code == ErrorCode.SESSION_EXPIRED
        assert store.get_session(STACK_URL) is None


# ----------------------------------------------------------------------------
# A refresh that times out
# ----------------------------------------------------------------------------


class _TimingOutThenOkAuthClient(AuthClient):
    """Fake whose first refresh times out and whose second one succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> _TimingOutThenOkAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def close(self) -> None:
        return None

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(refresh_token)
        if len(self.calls) == 1:
            raise KeboolaApiError(
                "Refreshing your Keboola login timed out.",
                error_code=ErrorCode.TIMEOUT,
                retryable=True,
            )
        return CliTokenResponse(
            accessToken="kbc_at_rotated_1",
            refreshToken="kbc_rt_rotated_1",
            expiresIn=3600,
            sessionId="session-1",
        )


class TestRefreshTimeout:
    """A refresh bounded by `AUTH_REFRESH_TIMEOUT` can time out; that must be clean.

    The failure happens while `auth.json.lock` is held, so three things have to
    hold at once: the error keeps its network classification (never the
    `ConfigError` "another process may be stuck holding it", which blames the
    wrong thing and exits 5), the still-valid session survives on disk, and
    both the file lock and the in-process lock are released so the next attempt
    is not wedged.
    """

    def test_timeout_keeps_its_network_classification(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: _TimingOutThenOkAuthClient()
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()

        assert exc_info.value.error_code == ErrorCode.TIMEOUT

    def test_session_is_not_purged(self, tmp_path: Path) -> None:
        """Only a rejected grant means "log in again"; a timeout says nothing about
        whether the refresh token is still good, so it must stay."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: _TimingOutThenOkAuthClient()
        )

        with pytest.raises(KeboolaApiError):
            provider.get_access_token()

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.refresh_token == "kbc_rt_original"

    def test_file_lock_is_released(self, tmp_path: Path) -> None:
        """Probed with an independent `FileLock` at timeout=0: a same-thread
        `store.transaction()` is reentrant and would pass even if the lock leaked."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: _TimingOutThenOkAuthClient()
        )

        with pytest.raises(KeboolaApiError):
            provider.get_access_token()

        probe = filelock.FileLock(str(store.lock_path), timeout=0)
        probe.acquire()  # raises filelock.Timeout if the failed refresh leaked it
        probe.release()

    def test_a_later_attempt_succeeds(self, tmp_path: Path) -> None:
        """Neither lock is wedged: the retry the user (or the next command) makes
        reaches the server and rotates the pair."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _TimingOutThenOkAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        with pytest.raises(KeboolaApiError):
            provider.get_access_token()

        assert provider.get_access_token() == "kbc_at_rotated_1"
        assert fake.calls == ["kbc_rt_original", "kbc_rt_original"]


# ----------------------------------------------------------------------------
# The wall-clock ceiling on the lock hold (review F2)
# ----------------------------------------------------------------------------


class _StalledAuthClient(AuthClient):
    """Fake that mimics a server trickling a response: `refresh` does not return.

    This is the shape httpx's per-phase `read` / `write` timeouts cannot catch --
    they are applied per I/O operation, so a response that keeps dribbling resets
    them forever.

    `close()` here releases the stalled call, which is deliberately MORE
    cooperative than the real thing: `httpx.Client.close()` returns immediately
    without aborting a request already in flight. The fake is generous only so an
    abandoned worker cannot sit parked for the rest of the suite; what these tests
    assert -- elapsed time and the file lock coming free -- does not depend on it.
    """

    def __init__(self, *, late_tokens: CliTokenResponse | None = None) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self._released = threading.Event()
        self._late_tokens = late_tokens

    def __enter__(self) -> _StalledAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._released.set()

    def release(self) -> None:
        """Let the stalled call finish, as a server finally answering would."""
        self._released.set()

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(refresh_token)
        self.entered.set()
        # Bounded so a broken ceiling cannot park this thread for the whole suite.
        self._released.wait(timeout=30)
        if self._late_tokens is not None:
            return self._late_tokens
        raise KeboolaApiError(
            "Connection closed while refreshing.",
            error_code=ErrorCode.CONNECTION_ERROR,
            retryable=True,
        )


class TestRefreshWallClockCeiling:
    """The lock hold is capped by `AUTH_REFRESH_MAX_WALL_CLOCK`, enforced here.

    `AUTH_REFRESH_TIMEOUT` cannot provide this: httpx applies `read` / `write`
    per I/O operation and has no total-duration option, so summing the phases
    describes a hope, not a bound. What every other process actually depends on
    is that `auth.json.lock` comes free within its `AUTH_LOCK_TIMEOUT` -- and
    that is what these tests assert.
    """

    @staticmethod
    def _provider(store: AuthStateStore, client: _StalledAuthClient) -> SessionTokenProvider:
        return SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: client)

    def test_a_stalled_refresh_gives_up_at_the_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tp_mod, "AUTH_REFRESH_MAX_WALL_CLOCK", 0.2)
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        client = _StalledAuthClient()
        provider = self._provider(store, client)

        started = time.monotonic()
        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()
        elapsed = time.monotonic() - started

        assert client.entered.is_set(), "the request must actually have been issued"
        assert exc_info.value.error_code == ErrorCode.TIMEOUT
        assert exc_info.value.retryable is True
        # Generous, but far below the 30 s wait every other lock holder would
        # otherwise sit through before blaming a "stuck" process.
        assert elapsed < 5.0

    def test_the_file_lock_comes_free_after_the_ceiling_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property the ceiling exists for, probed the way another process sees it."""
        monkeypatch.setattr(tp_mod, "AUTH_REFRESH_MAX_WALL_CLOCK", 0.2)
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        client = _StalledAuthClient()

        with pytest.raises(KeboolaApiError):
            self._provider(store, client).get_access_token()

        probe = filelock.FileLock(str(store.lock_path), timeout=0)
        probe.acquire()  # filelock.Timeout if the abandoned refresh still holds it
        probe.release()

    def test_the_still_valid_session_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ceiling breach says nothing about the refresh token, so it must stay."""
        monkeypatch.setattr(tp_mod, "AUTH_REFRESH_MAX_WALL_CLOCK", 0.2)
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)

        with pytest.raises(KeboolaApiError):
            self._provider(store, _StalledAuthClient()).get_access_token()

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.refresh_token == "kbc_rt_original"

    def test_a_rotation_that_lands_after_the_ceiling_is_never_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Persistence stays on the thread that holds the lock.

        The abandoned worker may still be handed a rotated pair. Writing it from
        there would touch `auth.json` with no lock held, so it is discarded --
        the stale-by-one-generation refresh token on disk is what the server's
        idempotent grace window exists to forgive.
        """
        monkeypatch.setattr(tp_mod, "AUTH_REFRESH_MAX_WALL_CLOCK", 0.2)
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        client = _StalledAuthClient(
            late_tokens=CliTokenResponse(
                accessToken="kbc_at_too_late",
                refreshToken="kbc_rt_too_late",
                expiresIn=3600,
                sessionId="session-1",
            )
        )

        with pytest.raises(KeboolaApiError):
            self._provider(store, client).get_access_token()
        client.release()
        time.sleep(0.2)  # give the abandoned worker every chance to write

        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.access_token == "kbc_at_original"
        assert persisted.refresh_token == "kbc_rt_original"


# ----------------------------------------------------------------------------
# The refresh lease (review B-1)
# ----------------------------------------------------------------------------


class _LockProbingAuthClient(AuthClient):
    """Fake that inspects the file lock and the lease from INSIDE the request.

    The point of the lease is that `auth.json.lock` is not held while the network
    call runs. That can only be observed from within the call, so this fake takes
    the measurement there rather than inferring it afterwards.
    """

    def __init__(self, store: AuthStateStore) -> None:
        self._store = store
        self.calls: list[str] = []
        self.lock_was_free: bool | None = None
        self.lease_holder_during_call: str | None = None

    def __enter__(self) -> _LockProbingAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        self.calls.append(refresh_token)
        probe = filelock.FileLock(str(self._store.lock_path), timeout=0)
        try:
            probe.acquire()
            probe.release()
            self.lock_was_free = True
        except filelock.Timeout:
            self.lock_was_free = False
        lease = self._store.get_refresh_lease(STACK_URL)
        self.lease_holder_during_call = lease.holder if lease else None
        return CliTokenResponse(
            accessToken="kbc_at_rotated_1",
            refreshToken="kbc_rt_rotated_1",
            expiresIn=3600,
            sessionId="session-1",
        )


class TestRefreshLease:
    """A refresh token may be presented to the server by one request at a time.

    Before the lease that was enforced by holding `auth.json.lock` across the
    network call, which made every other process (including a read-only `auth
    status`) wait `AUTH_LOCK_TIMEOUT` and then blame a "stuck" lock whenever the
    auth service was merely slow. These tests pin both halves of the trade: the
    lock comes free immediately, and the token is still never presented twice.
    """

    def test_the_file_lock_is_free_while_the_request_is_in_flight(self, tmp_path: Path) -> None:
        """The property the whole redesign exists for, measured from inside the call."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _LockProbingAuthClient(store)

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        assert provider.get_access_token() == "kbc_at_rotated_1"
        assert fake.lock_was_free is True

    def test_the_lease_is_held_during_the_request_and_released_after(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _LockProbingAuthClient(store)

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        provider.get_access_token()

        assert fake.lease_holder_during_call is not None
        assert store.get_refresh_lease(STACK_URL) is None

    def test_a_caller_that_loses_the_lease_adopts_the_winners_pair(self, tmp_path: Path) -> None:
        """No second request: the loser polls, then takes what the holder persisted."""
        store = AuthStateStore(tmp_path)
        session = _seed_session(store, access_expires_in=-10)
        with store.transaction():
            store.claim_refresh_lease(STACK_URL, holder="someone-else", ttl=AUTH_REFRESH_LEASE_TTL)
        fake = _FakeAuthClient()

        def _winner_finishes(_seconds: float) -> None:
            """Stand in for the holder completing while this caller waits."""
            store.put_session(
                session.model_copy(
                    update={
                        "access_token": "kbc_at_from_winner",
                        "refresh_token": "kbc_rt_from_winner",
                        "access_expires_at": datetime.now(UTC) + timedelta(hours=1),
                    }
                )
            )

        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: fake, sleep=_winner_finishes
        )

        assert provider.get_access_token() == "kbc_at_from_winner"
        assert fake.calls == [], "the loser must not issue its own refresh"

    def test_a_live_foreign_lease_times_out_with_a_truthful_message(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        with store.transaction():
            store.claim_refresh_lease(STACK_URL, holder="someone-else", ttl=AUTH_REFRESH_LEASE_TTL)
        fake = _FakeAuthClient()
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0])

        provider = SessionTokenProvider(
            STACK_URL,
            store,
            client_factory=lambda _url: fake,
            sleep=lambda _s: None,
            monotonic=lambda: next(clock),
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            provider.get_access_token()

        assert exc_info.value.error_code == ErrorCode.TIMEOUT
        assert "Another kbagent process" in exc_info.value.message
        assert fake.calls == []

    def test_an_expired_lease_is_taken_over(self, tmp_path: Path) -> None:
        """A holder that crashed mid-refresh must not wedge everyone for good."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        with store.transaction():
            store.claim_refresh_lease(STACK_URL, holder="crashed-holder", ttl=-1.0)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)
        assert provider.get_access_token() == "kbc_at_rotated_1"
        assert fake.calls == ["kbc_rt_original"]

    def test_an_abandoned_request_keeps_the_lease_so_nobody_replays_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The B-1 fix: an in-flight token must not be presented by a second caller.

        The ceiling firing does NOT mean the request is over -- it may still land
        server-side. Releasing the lease there would let the next process fire the
        same refresh token concurrently, which is the replay that triggers
        server-side family revocation.
        """
        monkeypatch.setattr(tp_mod, "AUTH_REFRESH_MAX_WALL_CLOCK", 0.2)
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        client = _StalledAuthClient()

        with pytest.raises(KeboolaApiError):
            SessionTokenProvider(
                STACK_URL, store, client_factory=lambda _url: client
            ).get_access_token()

        lease = store.get_refresh_lease(STACK_URL)
        assert lease is not None, "the lease was released while the token was still in flight"
        assert lease.is_live()

    def test_a_completed_failure_releases_the_lease(self, tmp_path: Path) -> None:
        """A request that finished carries no in-flight token, so the next attempt may run."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _TimingOutThenOkAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        with pytest.raises(KeboolaApiError):
            provider.get_access_token()
        assert store.get_refresh_lease(STACK_URL) is None

        assert provider.get_access_token() == "kbc_at_rotated_1"

    def test_an_unexpected_exception_still_releases_the_lease(self, tmp_path: Path) -> None:
        """A claim nobody can release is worse than the error that caused it.

        Only `_AbandonedRefresh` means the token may still be in flight. Anything
        else -- a decoder that choked on the body, an interrupt, a bug -- reached a
        verdict, so the claim must go. Keying the release on a list of expected
        exception types leaves the unanticipated one holding the lease for the
        whole TTL, stalling every command against the stack meanwhile.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)

        class _ExplodingAuthClient(_FakeAuthClient):
            def refresh(self, refresh_token: str) -> CliTokenResponse:
                raise RecursionError("maximum recursion depth exceeded while decoding")

        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: _ExplodingAuthClient()
        )

        with pytest.raises(RecursionError):
            provider.get_access_token()

        assert store.get_refresh_lease(STACK_URL) is None

    def test_a_lease_from_a_fast_clock_does_not_wedge_the_stack(self, tmp_path: Path) -> None:
        """The self-healing property must not depend on the writer's clock being right.

        A container that boots before NTP syncs, a VM resumed from a snapshot, or
        a wrong RTC all persist an expiry anchored to the wrong instant. Honouring
        it locks every later process out of the stack for the whole skew -- an hour
        here -- with no recovery short of `auth logout` and a full re-login.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        skewed_now = datetime.now(UTC) + timedelta(hours=1)
        with store.transaction():
            state = store.load()
            state.refresh_leases[STACK_URL] = RefreshLease(
                holder="crashed-holder-with-a-fast-clock",
                expires_at=skewed_now + timedelta(seconds=AUTH_REFRESH_LEASE_TTL),
            )
            store.save(state)
        fake = _FakeAuthClient()

        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        assert provider.get_access_token() == "kbc_at_rotated_1"
        assert fake.calls == ["kbc_rt_original"]

    def test_a_normal_lease_is_still_honoured(self, tmp_path: Path) -> None:
        """The horizon clamp must not turn every live claim into a takeable one."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        with store.transaction():
            store.claim_refresh_lease(STACK_URL, holder="holder-a", ttl=AUTH_REFRESH_LEASE_TTL)

        lease = store.get_refresh_lease(STACK_URL)
        assert lease is not None
        assert lease.is_live()
        with store.transaction():
            assert not store.claim_refresh_lease(STACK_URL, holder="holder-b", ttl=1.0)

    def test_a_deleted_session_takes_its_lease_with_it(self, tmp_path: Path) -> None:
        """Otherwise a fresh login would wait out a lease nobody can release."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        with store.transaction():
            store.claim_refresh_lease(STACK_URL, holder="gone", ttl=600.0)

        store.delete_session(STACK_URL)

        assert store.get_refresh_lease(STACK_URL) is None


class TestLeaseConstantArithmetic:
    """Relationships between the lease constants, so a later tune cannot break them.

    Each is a property the behavioural tests above rely on but cannot state:
    they exercise one set of values, while these hold for any set.
    """

    def test_the_horizon_admits_every_ttl_this_code_grants(self) -> None:
        """A horizon below a granted TTL rejects the claims we write ourselves.

        `RefreshLease.is_live` treats an expiry beyond the horizon as the work of
        a skewed clock. Set the horizon under either TTL and every ordinary claim
        reads as unusable -- the takeover path fires on live leases and two
        processes refresh the same token at once, which is the exact collision
        the lease exists to prevent.
        """
        assert AUTH_REFRESH_LEASE_MAX_HORIZON > AUTH_REFRESH_LEASE_TTL
        assert AUTH_REFRESH_LEASE_MAX_HORIZON > AUTH_REFRESH_ABANDON_GRACE

    def test_an_abandoned_lease_expires_before_a_waiter_gives_up(self) -> None:
        """Otherwise every command reports contention instead of just refreshing.

        A waiter polls for `AUTH_REFRESH_WAIT_TIMEOUT` and then raises "another
        kbagent process is still refreshing". While an abandon-extended claim
        outlives that budget, each command against the stack pays the full wait
        and then fails, though nothing is actually refreshing any more.
        """
        assert AUTH_REFRESH_MAX_WALL_CLOCK + AUTH_REFRESH_ABANDON_GRACE < AUTH_REFRESH_WAIT_TIMEOUT

    def test_the_abandon_pause_leaves_room_inside_the_server_grace_window(self) -> None:
        """The recovery model is a prompt retry, not a wait for the token to cool.

        The server measures its grace window from its own rotation, so the clock
        is already running when the ceiling fires. Abandon plus pause has to fit
        inside the window with room for the next command to start, or the retry
        lands past it and is punished as a proven replay -- family revocation,
        a forced re-login. `PROGRAMMATIC_AUTH_GRACE_PERIOD_SECONDS` is stack-side
        and never reported, so this pins the documented default and no more --
        recovery is NOT reliable as a stack approaches the documented 1 s floor,
        and no value here can make it so, because the ceiling alone already
        exceeds that floor. The pause is what we control; keeping it small is
        what keeps the reachable range as wide as it can be.
        """
        server_grace_default = 30.0
        # What the retry itself needs once the pause is over: a new process to
        # start, read auth.json and reach the refresh call.
        next_command_headroom = 10.0
        earliest_retry = AUTH_REFRESH_MAX_WALL_CLOCK + AUTH_REFRESH_ABANDON_GRACE

        assert earliest_retry + next_command_headroom <= server_grace_default

    def test_the_pause_is_a_minor_term_in_the_abandon_recovery_window(self) -> None:
        """The ceiling, not the pause, is what decides how short a window still recovers.

        Stated as a relationship so a later bump cannot quietly invert it: once the
        pause grows to rival the ceiling, shrinking it stops buying reach and the
        comment above -- which sends anyone wanting a wider range to the ceiling
        instead -- becomes wrong advice.
        """
        assert AUTH_REFRESH_ABANDON_GRACE < AUTH_REFRESH_MAX_WALL_CLOCK / 2


# ----------------------------------------------------------------------------
# force_refresh
# ----------------------------------------------------------------------------


class TestForceRefresh:
    def test_short_circuits_when_another_thread_already_rotated(self, tmp_path: Path) -> None:
        """The cached token already differs from the rejected one -> no network call."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        provider.get_access_token()  # warm the cache, no refresh
        assert fake.calls == []

        assert provider.force_refresh("kbc_at_someOtherStaleToken") == "kbc_at_original"
        assert fake.calls == []

    def test_refreshes_when_the_cached_token_is_the_rejected_one(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        current = provider.get_access_token()
        assert provider.force_refresh(current) == "kbc_at_rotated_1"
        assert fake.calls == ["kbc_rt_original"]


# ----------------------------------------------------------------------------
# BearerAuth through a real httpx.Client
# ----------------------------------------------------------------------------


class TestBearerAuthReactive401:
    def test_401_is_retried_exactly_once_with_a_refreshed_token(
        self, tmp_path: Path, httpx_mock
    ) -> None:
        """The whole point of the generator-based httpx.Auth hook.

        First attempt carries the stale token and gets a 401; the hook force-
        refreshes and replays the request once. A second 401 must NOT loop.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        httpx_mock.add_response(url=f"{STACK_URL}/probe", status_code=401, json={})
        httpx_mock.add_response(url=f"{STACK_URL}/probe", status_code=200, json={"ok": True})

        with httpx.Client(base_url=STACK_URL, auth=BearerAuth(provider, 10105)) as client:
            response = client.get("/probe")

        assert response.status_code == 200
        requests = httpx_mock.get_requests()
        assert len(requests) == 2
        # httpx replays the SAME Request instance on the auth retry, mutating
        # its headers in place, so both recorded entries show the final value --
        # the first attempt's original header cannot be read back here. What
        # matters is asserted instead: the replay carried the rotated token,
        # exactly one refresh was performed, and the project binding survived
        # the retry (a header set only on the first pass would be lost).
        assert requests[-1].headers["Authorization"] == "Bearer kbc_at_rotated_1"
        assert requests[-1].headers["X-KBC-ProjectId"] == "10105"
        assert fake.calls == ["kbc_rt_original"]

    def test_persistent_401_does_not_loop(self, tmp_path: Path, httpx_mock) -> None:
        """A second 401 is returned to the caller rather than retried forever."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        httpx_mock.add_response(url=f"{STACK_URL}/probe", status_code=401, json={})
        httpx_mock.add_response(url=f"{STACK_URL}/probe", status_code=401, json={})

        with httpx.Client(base_url=STACK_URL, auth=BearerAuth(provider)) as client:
            response = client.get("/probe")

        assert response.status_code == 401
        assert len(httpx_mock.get_requests()) == 2

    def test_no_project_header_when_project_id_is_none(self, tmp_path: Path, httpx_mock) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)
        provider = SessionTokenProvider(
            STACK_URL, store, client_factory=lambda _url: _FakeAuthClient()
        )
        httpx_mock.add_response(url=f"{STACK_URL}/probe", json={})

        with httpx.Client(base_url=STACK_URL, auth=BearerAuth(provider)) as client:
            client.get("/probe")

        assert "X-KBC-ProjectId" not in httpx_mock.get_requests()[0].headers


# ----------------------------------------------------------------------------
# In-process race
# ----------------------------------------------------------------------------


class TestInProcessRace:
    def test_ten_threads_on_an_expired_cache_trigger_exactly_one_refresh(
        self, tmp_path: Path
    ) -> None:
        """Ten concurrent callers must mint exactly one refresh token.

        Nine extra rotations would immediately go stale, and reusing any one
        after the server's 30s grace window triggers family revocation -- a hard
        logout. This asserts the end-to-end outcome without attributing it to
        either serialization layer: the per-provider `threading.Lock` and the
        flock-based re-read-and-adopt in `_refresh_locked` each suffice on their
        own, so this passes with either one removed. What the thread lock
        specifically contributes is pinned by the next test.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        with ThreadPoolExecutor(max_workers=10) as executor:
            tokens = list(executor.map(lambda _i: provider.get_access_token(), range(10)))

        assert len(fake.calls) == 1
        assert set(tokens) == {"kbc_at_rotated_1"}

    def test_the_thread_lock_keeps_nine_threads_out_of_the_file_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The thread lock's own contribution: nine threads never reach the flock.

        `_refresh_locked` reads the session exactly once per entry, so counting
        `get_session` counts how many threads got past the double-checked cache
        and into the cross-process transaction. With the `threading.Lock` the
        losers wake up, see a fresh cache, and return without touching
        `auth.json` at all -- one entry. Without it all ten queue on the OS lock
        and re-read in turn; the refresh count stays 1 either way, which is why
        the test above cannot tell the difference.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        reads: list[str] = []
        real_get_session = store.get_session

        def counting_get_session(stack_url: str) -> StackSession | None:
            reads.append(stack_url)
            return real_get_session(stack_url)

        monkeypatch.setattr(store, "get_session", counting_get_session)

        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(lambda _i: provider.get_access_token(), range(10)))

        assert len(fake.calls) == 1
        assert len(reads) == 1

    def test_registry_returns_one_provider_per_stack_and_state_file(self, tmp_path: Path) -> None:
        """A fresh provider per call site would each hold its own lock and refresh
        independently -- the registry is what makes the in-process lock effective."""
        store = AuthStateStore(tmp_path)
        first = get_session_token_provider(STACK_URL, store)
        second = get_session_token_provider("connection.keboola.com", store)
        assert first is second


# ----------------------------------------------------------------------------
# Stack URL as a dict key
# ----------------------------------------------------------------------------


class TestStackKeyIsCaseInsensitive:
    """Both places a stack URL is used as a key must fold host case.

    Hostnames are case-insensitive, so a session stored under
    `Connection.Keboola.Com` has to be the same session a later
    `connection.keboola.com` lookup finds -- otherwise `auth status` reports a
    live session as missing, and the next login mints a redundant second session
    for one physical stack. `models.normalize_stack_url` is the single place that
    canonicalizes; these pin that both key surfaces actually go through it.
    """

    def test_a_session_stored_mixed_case_is_found_lowercase(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        now = datetime.now(UTC)
        store.put_session(
            StackSession(
                stack_url="https://Connection.Keboola.Com",
                session_id="session-1",
                user_email="user@example.com",
                user_name="Test User",
                access_token="kbc_at_original",
                refresh_token="kbc_rt_original",
                access_expires_at=now + timedelta(seconds=3600),
                refresh_expires_at=now + timedelta(days=30),
                created_at=now,
            )
        )

        found = store.get_session("https://connection.keboola.com")

        assert found is not None
        assert found.access_token == "kbc_at_original"
        # Persisted canonically, so `auth.json` holds one key per physical stack.
        assert found.stack_url == "https://connection.keboola.com"
        assert json.loads(store.state_path.read_text(encoding="utf-8"))["sessions"] == {
            "https://connection.keboola.com": found.model_dump(mode="json")
        }

    def test_the_provider_registry_shares_one_provider_across_spellings(
        self, tmp_path: Path
    ) -> None:
        """Two providers for one stack would each hold their own thread lock and
        their own token cache, refreshing independently."""
        store = AuthStateStore(tmp_path)
        mixed = get_session_token_provider("https://Connection.Keboola.Com", store)
        lower = get_session_token_provider("connection.keboola.com", store)

        assert mixed is lower
        assert mixed.stack_url == "https://connection.keboola.com"


# ----------------------------------------------------------------------------
# Cross-process race (real spawned processes)
# ----------------------------------------------------------------------------


def _hold_lock_worker(config_dir: str, marker_path: str, hold_seconds: float) -> None:
    """Enter the auth.json transaction, record enter/exit, hold briefly.

    Module-level so the `spawn` start method can pickle it (required on macOS
    and Windows).
    """
    import time as _time

    store = AuthStateStore(Path(config_dir))
    with store.transaction():
        with open(marker_path, "a", encoding="utf-8") as handle:
            handle.write(f"enter {os.getpid()} {_time.monotonic()}\n")
            handle.flush()
        _time.sleep(hold_seconds)
        with open(marker_path, "a", encoding="utf-8") as handle:
            handle.write(f"exit {os.getpid()} {_time.monotonic()}\n")
            handle.flush()


def _append_session_worker(config_dir: str, stack_url: str) -> None:
    """Read-modify-write `auth.json` under the transaction lock."""
    store = AuthStateStore(Path(config_dir))
    with store.transaction():
        state = store.load()
        session = state.sessions[stack_url]
        session.orphaned_session_ids.append(f"orphan-{os.getpid()}")
        store.save(state)


class TestCrossProcessLock:
    """Real separate processes contending on `auth.json` via `filelock`.

    Requires the ``spawn`` start method, which CPython offers on every platform
    this project supports; these must fail loudly rather than skip if it ever
    goes missing, because a silent skip would leave the cross-process guarantee
    unverified.

    What this proves: the lock guarding rotation is a genuine OS-level advisory
    lock that serializes *across processes*, so two kbagent invocations cannot
    interleave a read-modify-write of `auth.json` and lose one another's pair.
    That is the property `SessionTokenProvider._refresh_locked` depends on, and
    the reason this store uses `filelock` rather than mirroring `ConfigStore`'s
    `fcntl` helper (a silent no-op on Windows).

    What this class covers is the lock itself; `TestCrossProcessRefreshRace`
    below covers the actual refresh round-trip -- a *fake* `AuthClient` is
    enough for that (no live auth backend needed), it just has to run inside
    two genuine OS processes for the assertion to mean anything.
    """

    def test_transactions_do_not_overlap(self, tmp_path: Path) -> None:
        marker = tmp_path / "markers.txt"
        marker.touch()
        ctx = multiprocessing.get_context("spawn")

        processes = [
            ctx.Process(target=_hold_lock_worker, args=(str(tmp_path), str(marker), 0.4))
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0, "worker process failed"

        events = [line.split()[0] for line in marker.read_text().splitlines() if line.strip()]
        assert len(events) == 4
        # Strict alternation proves mutual exclusion: an overlap would produce
        # "enter enter" before the first "exit".
        assert events == ["enter", "exit", "enter", "exit"]

    def test_concurrent_read_modify_write_loses_no_update(self, tmp_path: Path) -> None:
        """Four processes each append one orphan id; all four must survive.

        Without a real cross-process lock the last writer would clobber the
        others -- the same mechanism by which a delayed, stale writer could
        overwrite a newer token pair and trigger family revocation.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)

        ctx = multiprocessing.get_context("spawn")
        processes = [
            ctx.Process(target=_append_session_worker, args=(str(tmp_path), STACK_URL))
            for _ in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
            assert process.exitcode == 0, "worker process failed"

        session = store.get_session(STACK_URL)
        assert session is not None
        assert len(session.orphaned_session_ids) == 4
        assert len(set(session.orphaned_session_ids)) == 4

    def test_auth_json_stays_valid_json_under_contention(self, tmp_path: Path) -> None:
        """Atomic tmp+rename means a reader never observes a torn file."""
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=3600)

        ctx = multiprocessing.get_context("spawn")
        processes = [
            ctx.Process(target=_append_session_worker, args=(str(tmp_path), STACK_URL))
            for _ in range(3)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)

        payload: dict[str, Any] = json.loads(store.state_path.read_text(encoding="utf-8"))
        assert STACK_URL in payload["sessions"]


class _PidTaggedAuthClient(AuthClient):
    """Fake `AuthClient` for the cross-process race worker below.

    Returns a token pair derived from this process's own pid and reports
    every `refresh()` call on ``call_queue`` -- that is what lets the test
    tell, after the fact, which of the two racing processes actually
    performed the one real refresh. A dedicated class rather than
    `_FakeAuthClient` (used elsewhere in this file) because it must survive
    `multiprocessing`'s ``spawn`` start method: it is constructed fresh
    inside each worker process and carries no `threading.Lock` or other
    unpicklable state across the process boundary.

    Subclasses `AuthClient` (skipping `super().__init__()`, so no real
    `httpx.Client` is built) purely so it satisfies `SessionTokenProvider`'s
    `client_factory: Callable[[str], AuthClient]` signature under `ty`.
    """

    def __init__(self, call_queue: multiprocessing.Queue[int], delay: float) -> None:
        self._call_queue = call_queue
        self._delay = delay

    def __enter__(self) -> _PidTaggedAuthClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        import time as _time

        self._call_queue.put(os.getpid())
        _time.sleep(self._delay)
        return CliTokenResponse(
            accessToken=f"kbc_at_{os.getpid()}",
            refreshToken=f"kbc_rt_{os.getpid()}",
            expiresIn=3600,
            sessionId="sess-rotated",
        )


def _get_access_token_race_worker(
    config_dir: str,
    stack_url: str,
    refresh_delay: float,
    start_delay: float,
    result_queue: multiprocessing.Queue[dict[str, Any]],
    call_queue: multiprocessing.Queue[int],
) -> None:
    """Module-level (picklable for ``spawn``) worker driving a REAL
    ``SessionTokenProvider.get_access_token()`` call against the shared
    ``auth.json`` in ``config_dir``.

    Unlike `_hold_lock_worker` / `_append_session_worker` above, this drives
    the actual refresh algorithm (steps 4-8), with `_PidTaggedAuthClient`
    standing in for the network call.
    """
    import time as _time

    if start_delay:
        _time.sleep(start_delay)

    state_store = AuthStateStore(Path(config_dir))
    client = _PidTaggedAuthClient(call_queue, refresh_delay)

    provider = SessionTokenProvider(stack_url, state_store, client_factory=lambda _url: client)
    try:
        token = provider.get_access_token()
        result_queue.put({"pid": os.getpid(), "token": token, "error": None})
    except Exception as exc:
        result_queue.put({"pid": os.getpid(), "token": None, "error": repr(exc)})


class TestCrossProcessRefreshRace:
    """Two real OS processes both racing `SessionTokenProvider.get_access_token()`
    against the same stale session -- the scenario `TestCrossProcessLock`
    above deliberately stops short of (it proves the lock is mutually
    exclusive across processes; this proves the refresh algorithm's step-5
    re-read-and-adopt is what turns that mutual exclusion into "a delayed
    writer never clobbers a newer pair").

    Whichever process acquires ``auth.json.lock`` first performs the one real
    refresh; the OS-level `filelock` forces the other to block, then -- per
    plan step 5 -- it re-reads the now-fresh session under the lock and
    adopts it WITHOUT calling its own refresh. Worker B is given a small
    head-start delay to reliably play the role of the "delayed writer", but
    the assertions hold regardless of which process actually wins (spawn /
    scheduler jitter): exactly one of the two ever calls `refresh()`, both
    processes return the identical winning pair, and `auth.json` ends up
    holding exactly that pair -- never a second, stale-derived rotation from
    the loser. That is precisely the situation review B-4 warns about: an
    unserialized cross-process refresh would let a slow/delayed writer
    persist a stale pair after a faster one already rotated, and reusing that
    stale refresh token once its 30s grace window elapses triggers server-
    side refresh-token family revocation -- a hard logout.
    """

    def test_delayed_writer_never_clobbers_the_winners_pair(self, tmp_path: Path) -> None:
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)

        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()
        call_queue: multiprocessing.Queue = ctx.Queue()

        proc_a = ctx.Process(
            target=_get_access_token_race_worker,
            args=(str(tmp_path), STACK_URL, 0.3, 0.0, result_queue, call_queue),
        )
        # Worker B is the deliberately delayed ("late starter") writer.
        proc_b = ctx.Process(
            target=_get_access_token_race_worker,
            args=(str(tmp_path), STACK_URL, 0.3, 0.05, result_queue, call_queue),
        )

        proc_a.start()
        proc_b.start()
        proc_a.join(timeout=30)
        proc_b.join(timeout=30)

        assert proc_a.exitcode == 0
        assert proc_b.exitcode == 0

        results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
        refresh_pids: list[int] = []
        while not call_queue.empty():
            refresh_pids.append(call_queue.get(timeout=5))

        for result in results:
            assert result["error"] is None, f"worker {result['pid']} raised: {result['error']}"

        # Exactly one of the two processes ever called refresh() -- the other
        # discovered the already-fresh pair under the lock instead.
        assert len(refresh_pids) == 1
        winner_pid = refresh_pids[0]

        # Both processes agree on the same final token: the winner's.
        tokens = {result["token"] for result in results}
        assert tokens == {f"kbc_at_{winner_pid}"}

        # auth.json holds exactly the winner's pair -- the delayed process
        # never overwrote it with a stale-derived rotation of its own.
        persisted = store.get_session(STACK_URL)
        assert persisted is not None
        assert persisted.access_token == f"kbc_at_{winner_pid}"
        assert persisted.refresh_token == f"kbc_rt_{winner_pid}"
