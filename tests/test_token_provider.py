"""Tests for `SessionTokenProvider` and `BearerAuth` (0.77.0).

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
  adopts whatever the winner persisted, instead of minting a second pair.

The cross-process test at the bottom uses real spawned processes. What it
proves and what it deliberately does not is documented on the test itself.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from keboola_agent_cli.auth.auth_client import AuthClient
from keboola_agent_cli.auth.models import CliTokenResponse, StackSession
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.auth.token_provider import (
    BearerAuth,
    SessionTokenProvider,
    get_session_token_provider,
    reset_provider_registry,
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
        """Without the per-provider lock this would mint ten refresh tokens.

        Nine of them would immediately become stale, and reusing any one after
        the server's 30s grace window triggers family revocation -- a hard
        logout. Exactly one refresh is the whole invariant.
        """
        store = AuthStateStore(tmp_path)
        _seed_session(store, access_expires_in=-10)
        fake = _FakeAuthClient()
        provider = SessionTokenProvider(STACK_URL, store, client_factory=lambda _url: fake)

        with ThreadPoolExecutor(max_workers=10) as executor:
            tokens = list(executor.map(lambda _i: provider.get_access_token(), range(10)))

        assert len(fake.calls) == 1
        assert set(tokens) == {"kbc_at_rotated_1"}

    def test_registry_returns_one_provider_per_stack_and_state_file(self, tmp_path: Path) -> None:
        """A fresh provider per call site would each hold its own lock and refresh
        independently -- the registry is what makes the in-process lock effective."""
        store = AuthStateStore(tmp_path)
        first = get_session_token_provider(STACK_URL, store)
        second = get_session_token_provider("connection.keboola.com", store)
        assert first is second


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


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "fork" and os.name == "nt",
    reason="spawn start method unavailable",
)
class TestCrossProcessLock:
    """Real separate processes contending on `auth.json` via `filelock`.

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


class _PidTaggedAuthClient:
    """Fake `AuthClient` for the cross-process race worker below.

    Returns a token pair derived from this process's own pid and reports
    every `refresh()` call on ``call_queue`` -- that is what lets the test
    tell, after the fact, which of the two racing processes actually
    performed the one real refresh. A dedicated class rather than
    `_FakeAuthClient` (used elsewhere in this file) because it must be
    picklable for `multiprocessing`'s ``spawn`` start method: it is
    constructed fresh inside each worker process and carries no
    `threading.Lock` or other unpicklable state across the process boundary.
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


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "fork" and os.name == "nt",
    reason="spawn start method unavailable",
)
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
