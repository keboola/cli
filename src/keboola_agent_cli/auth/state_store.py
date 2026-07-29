"""Persistence for programmatic-auth sessions (auth.json)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import filelock

from ..constants import (
    AUTH_LOCK_TIMEOUT,
    AUTH_STATE_FILENAME,
    AUTH_STATE_LOCK_FILENAME,
    AUTH_STATE_VERSION,
)
from ..errors import ConfigError
from ..models import normalize_stack_url
from .models import AuthState, RefreshLease, StackSession

if TYPE_CHECKING:
    # Only needed for the type hint below; importing ConfigStore at runtime
    # would drag config_store.py into every consumer of this module (and this
    # package is meant to stay import-light -- see auth/__init__.py).
    from ..config_store import ConfigStore

logger = logging.getLogger(__name__)


class AuthStateStore:
    """Persistence for programmatic-auth sessions in ``auth.json`` (0600).

    Mirrors ConfigStore's atomic tmp+rename write and sidecar-lock pattern, with
    one deliberate difference: the lock is a REAL cross-platform advisory lock
    (``filelock``), not ConfigStore's ``fcntl`` helper -- that helper is a silent
    no-op on Windows, and unserialized refresh rotation there can persist a stale
    token pair, which after the server's 30 s grace window triggers refresh-token
    family revocation (a hard logout). See plan review B-4.
    """

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir
        self._state_path = config_dir / AUTH_STATE_FILENAME
        self._lock_path = config_dir / AUTH_STATE_LOCK_FILENAME
        # `filelock.FileLock` is thread-local and reentrant by default: nested
        # `transaction()` calls in the same thread just bump an internal
        # counter (no re-locking, no deadlock), while a different thread -- or
        # a different process -- contends on the real OS-level lock. That is
        # exactly the "reentrant per thread" semantics ConfigStore.transaction()
        # implements by hand with its own threading.local depth counter.
        self._file_lock = filelock.FileLock(str(self._lock_path), timeout=AUTH_LOCK_TIMEOUT)

    @classmethod
    def from_config_store(cls, config_store: ConfigStore) -> AuthStateStore:
        """Bind to ``config_store.config_dir`` so --config-dir / local .kbagent carry over."""
        return cls(config_store.config_dir)

    @property
    def state_path(self) -> Path:
        """Return the path to ``auth.json``."""
        return self._state_path

    @property
    def lock_path(self) -> Path:
        """Return the path to the sidecar lock file (``auth.json.lock``)."""
        return self._lock_path

    @property
    def config_dir(self) -> Path:
        """Return the directory holding ``auth.json`` (shared with ``config.json``)."""
        return self._config_dir

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the exclusive cross-process lock across a load -> mutate -> save cycle.

        Reentrant per thread (threading.local depth counter, provided by
        ``filelock.FileLock`` itself), like ``ConfigStore.transaction()``: a
        mutator that calls ``self.load()`` then ``self.save()`` inside its own
        ``with self.transaction():`` block does not re-acquire the OS lock, but
        a concurrent thread or process still contends on it correctly.
        """
        try:
            self._file_lock.acquire(timeout=AUTH_LOCK_TIMEOUT)
        except filelock.Timeout as exc:
            raise ConfigError(
                f"Could not acquire lock on {self._lock_path} within "
                f"{AUTH_LOCK_TIMEOUT}s. Another kbagent process may be stuck "
                "holding it."
            ) from exc
        try:
            yield
        finally:
            self._file_lock.release()

    def _fix_permissions_if_needed(self) -> None:
        """Reset ``auth.json`` back to 0600 if some other process widened it.

        Skipped on Windows (``os.name == "nt"``), where POSIX permission bits
        are not meaningful the same way and ``os.chmod`` cannot narrow ACLs.
        """
        if os.name == "nt":
            return
        try:
            mode = stat.S_IMODE(self._state_path.stat().st_mode)
        except OSError:
            return
        if mode & ~0o600:
            try:
                os.chmod(self._state_path, 0o600)
                logger.warning(
                    "Auth state file %s had overly broad permissions (%o); reset to 0600.",
                    self._state_path,
                    mode,
                )
            except OSError:
                logger.debug("Could not reset permissions on %s", self._state_path)

    def load(self) -> AuthState:
        """Load ``auth.json``. A missing file returns an empty ``AuthState``.

        Raises:
            ConfigError: If the file is corrupt, has the wrong shape, or its
                ``version`` is newer than this build's ``AUTH_STATE_VERSION``.
        """
        with self.transaction():
            if not self._state_path.exists():
                return AuthState()

            self._fix_permissions_if_needed()

            try:
                raw = self._state_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"Cannot read auth state file {self._state_path}: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise ConfigError(f"Auth state file is not valid UTF-8 text: {exc}") from exc

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"Auth state file {self._state_path} is not valid JSON: {exc}"
                ) from exc

            if not isinstance(data, dict):
                raise ConfigError(
                    f"Auth state file {self._state_path} has invalid structure: "
                    f"expected JSON object, got {type(data).__name__}"
                )

            version = data.get("version", AUTH_STATE_VERSION)
            if version > AUTH_STATE_VERSION:
                raise ConfigError(
                    f"Auth state file version {version} is newer than supported "
                    f"version {AUTH_STATE_VERSION}. Please upgrade keboola-cli."
                )

            try:
                return AuthState.model_validate(data)
            except Exception as exc:
                raise ConfigError(
                    f"Auth state file {self._state_path} has invalid structure: {exc}"
                ) from exc

    def save(self, state: AuthState) -> None:
        """Write ``auth.json`` atomically at 0600. Never leaves a partial file.

        Raises:
            ConfigError: If the file cannot be written.
        """
        with self.transaction():
            tmp_path = self._state_path.with_suffix(".tmp")
            try:
                self._config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                payload = state.model_dump(mode="json")
                json_str = json.dumps(payload, indent=2, ensure_ascii=False)
                data = (json_str + "\n").encode("utf-8")

                fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                os.replace(str(tmp_path), str(self._state_path))
            except OSError as exc:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                raise ConfigError(
                    f"Cannot write auth state file {self._state_path}: {exc}"
                ) from exc

    def get_session(self, stack_url: str) -> StackSession | None:
        """Return the persisted session for ``stack_url``, or None if absent."""
        state = self.load()
        return state.sessions.get(normalize_stack_url(stack_url))

    def put_session(self, session: StackSession) -> None:
        """Insert or replace the session for ``session.stack_url``.

        ``session.stack_url`` is normalized before use as the dict key, so a
        session built from a deep-link URL still lands under the same key as
        one built from the bare host.
        """
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(session.stack_url)
            if session.stack_url != normalized:
                session = session.model_copy(update={"stack_url": normalized})
            state.sessions[normalized] = session
            self.save(state)

    def delete_session(self, stack_url: str) -> bool:
        """Remove the session for ``stack_url``. Returns True if one was removed.

        Drops the stack's refresh lease too: with no session there is no token
        left to protect, and leaving the claim behind would make the next login
        wait out a lease that can never be released.
        """
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(stack_url)
            state.refresh_leases.pop(normalized, None)
            if normalized not in state.sessions:
                self.save(state)
                return False
            del state.sessions[normalized]
            self.save(state)
            return True

    def get_refresh_lease(self, stack_url: str) -> RefreshLease | None:
        """Return the stack's refresh lease, whether or not it is still live."""
        state = self.load()
        return state.refresh_leases.get(normalize_stack_url(stack_url))

    def claim_refresh_lease(self, stack_url: str, *, holder: str, ttl: float) -> bool:
        """Claim the right to refresh ``stack_url``; True when the claim succeeded.

        Refuses only while somebody else's claim is still live. An expired lease
        is taken over -- a holder that crashed mid-refresh must not block every
        later process for good. Re-claiming one's own lease renews it.

        Callers must run this inside a `transaction()` so the read-then-write is
        atomic against another process doing the same.
        """
        state = self.load()
        normalized = normalize_stack_url(stack_url)
        current = state.refresh_leases.get(normalized)
        if current is not None and current.holder != holder and current.is_live():
            return False
        state.refresh_leases[normalized] = RefreshLease(
            holder=holder, expires_at=datetime.now(UTC) + timedelta(seconds=ttl)
        )
        self.save(state)
        return True

    def extend_refresh_lease(self, stack_url: str, *, holder: str, ttl: float) -> None:
        """Push the holder's own lease expiry out by ``ttl`` from now. No-op otherwise."""
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(stack_url)
            current = state.refresh_leases.get(normalized)
            if current is None or current.holder != holder:
                return
            state.refresh_leases[normalized] = RefreshLease(
                holder=holder, expires_at=datetime.now(UTC) + timedelta(seconds=ttl)
            )
            self.save(state)

    def release_refresh_lease(self, stack_url: str, *, holder: str) -> None:
        """Drop ``holder``'s claim. Never touches a lease somebody else now holds."""
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(stack_url)
            current = state.refresh_leases.get(normalized)
            if current is None or current.holder != holder:
                return
            del state.refresh_leases[normalized]
            self.save(state)

    def record_orphan(self, stack_url: str, session_id: str) -> None:
        """Append ``session_id`` to the orphan list of the session at ``stack_url``.

        A no-op when no session is currently persisted for that stack (nothing
        to attach the orphan record to). Idempotent: recording the same
        ``session_id`` twice does not duplicate it.
        """
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(stack_url)
            session = state.sessions.get(normalized)
            if session is None:
                return
            if session_id not in session.orphaned_session_ids:
                session.orphaned_session_ids.append(session_id)
            self.save(state)

    def clear_orphans(self, stack_url: str) -> None:
        """Clear the orphan list of the session at ``stack_url``. No-op if absent."""
        with self.transaction():
            state = self.load()
            normalized = normalize_stack_url(stack_url)
            session = state.sessions.get(normalized)
            if session is None:
                return
            session.orphaned_session_ids = []
            self.save(state)
