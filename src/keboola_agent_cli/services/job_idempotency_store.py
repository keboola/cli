"""Client-side idempotency store for Queue job runs (issue #427).

The Keboola Queue API ``POST /jobs`` accepts **no** client-supplied idempotency
key -- verified against the live spec (v1.3.8) and the server source: a
``deduplicationId`` exists internally but is daemon-only and never read from the
public create-job request. So an agentic orchestrator that replays a
side-effecting build step after a crash would create a *duplicate* job.

This module closes that gap on the client side: a small persistent map of
``idempotency_key -> {job_id, component_id, config_id, branch_id}`` plus a
probe-before-create helper (:func:`run_idempotent_job`). A replayed call with the
same key returns the prior job instead of firing the side effect again.

Persistence mirrors :class:`~keboola_agent_cli.auth.state_store.AuthStateStore`:
an atomic tmp+rename write with ``0600`` permissions, serialised by a REAL
cross-platform advisory lock (``filelock``) on a ``.lock`` sidecar -- so
concurrent ``run_job`` calls do not corrupt the file or lose each other's
entries.

Two deliberate choices, both learned the hard way on Windows:
- The lock lives on a **sidecar**, never on the state file itself. Windows
  refuses to ``os.replace()`` over a file that still has an open handle, so
  locking the target made every write fail with ``PermissionError``.
- The lock is ``filelock``, not ``ConfigStore``'s ``fcntl`` helper, which is a
  silent no-op on Windows. A lost entry here is not a cosmetic corruption: it
  makes the next replay create a **duplicate job**, which is the exact side
  effect this store exists to prevent.

Scope / limits:
- Dedup is **per store file** (per config-dir / per machine). A replay from a
  *different* machine that does not share the store file is not deduplicated.
  For DB-enforced, cross-machine dedup the Queue API would need to expose its
  internal ``deduplicationId`` -- a separate upstream request.
- Policy: a prior job that is still running or finished non-failed is returned
  (no new side effect); a prior job that *failed* is re-run (a replay after a
  fix should make progress), unless the caller pins the prior job explicitly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import filelock

from ..constants import JOB_IDEMPOTENCY_LOCK_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError

logger = logging.getLogger(__name__)

_STORE_VERSION = 1

# Terminal statuses that mean "this run did not succeed" -> a replay re-runs it.
# Mirrors ``JobResult.failed``. ``warning`` is a soft-success terminal and is
# deliberately NOT here (we don't re-run a job that warned).
_FAILED_STATUSES: frozenset[str] = frozenset({"error", "terminated", "cancelled"})


@dataclass(frozen=True)
class JobIdempotencyEntry:
    """One recorded job run, keyed by the caller's idempotency key."""

    job_id: str
    component_id: str
    config_id: str
    branch_id: int | None
    created_at: str

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> JobIdempotencyEntry:
        return cls(
            job_id=str(data.get("job_id", "")),
            component_id=str(data.get("component_id", "")),
            config_id=str(data.get("config_id", "")),
            branch_id=data.get("branch_id"),
            created_at=str(data.get("created_at", "")),
        )

    def _to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "component_id": self.component_id,
            "config_id": self.config_id,
            "branch_id": self.branch_id,
            "created_at": self.created_at,
        }


class JobIdempotencyStore:
    """A persistent ``idempotency_key -> JobIdempotencyEntry`` map.

    Construct with the path to the JSON state file. For the CLI / service path
    this lives alongside ``config.json`` in the config-dir; an in-process SDK
    consumer supplies its own path (typically inside its resume-checkpoint dir).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        # A lock file SEPARATE from the state file. Taking the lock on the state
        # file itself works on POSIX but is fatal on Windows: `os.replace()`
        # cannot rename over a file that still has an open handle, so every write
        # died with `PermissionError [WinError 5]`.
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        # `filelock`, not ConfigStore's `fcntl` helper -- that helper is a silent
        # no-op on Windows, which would leave the read-modify-write below
        # unserialised there. Reentrant per thread, so a nested `_locked()` does
        # not deadlock. Same reasoning as `auth/state_store.py`.
        self._file_lock = filelock.FileLock(
            str(self._lock_path), timeout=JOB_IDEMPOTENCY_LOCK_TIMEOUT
        )

    @property
    def path(self) -> Path:
        return self._path

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise a read-modify-write against every other kbagent process."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._file_lock.acquire(timeout=JOB_IDEMPOTENCY_LOCK_TIMEOUT)
        except filelock.Timeout as exc:
            raise ConfigError(
                f"Could not acquire lock on {self._lock_path} within "
                f"{JOB_IDEMPOTENCY_LOCK_TIMEOUT}s. Another kbagent process may be "
                "stuck holding it."
            ) from exc
        try:
            yield
        finally:
            self._file_lock.release()

    def _write_atomically(self, data: dict[str, Any]) -> None:
        """Serialise ``data`` to a 0600 temp file and rename it into place."""
        tmp_path = self._path.with_suffix(".tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(str(tmp_path), str(self._path))

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": _STORE_VERSION, "entries": {}}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt dedup file must not wedge job runs: log and start fresh.
            # Worst case is one missed dedup, never a crash on a side-effecting path.
            logger.warning("Ignoring unreadable idempotency store %s: %s", self._path, exc)
            return {"version": _STORE_VERSION, "entries": {}}
        if not isinstance(data, dict):
            return {"version": _STORE_VERSION, "entries": {}}
        data.setdefault("entries", {})
        return data

    def lookup(self, key: str) -> JobIdempotencyEntry | None:
        """Return the entry recorded for ``key``, or ``None`` if unseen."""
        entry = self._read().get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        return JobIdempotencyEntry._from_dict(entry)

    def record(
        self,
        key: str,
        *,
        job_id: str,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> JobIdempotencyEntry:
        """Persist (or overwrite) the entry for ``key`` under an exclusive lock."""
        entry = JobIdempotencyEntry(
            job_id=job_id,
            component_id=component_id,
            config_id=config_id,
            branch_id=branch_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._locked():
            # Read-modify-write *inside* the lock so concurrent records (distinct
            # keys) don't clobber each other.
            data = self._read()
            data["version"] = _STORE_VERSION
            data.setdefault("entries", {})[key] = entry._to_dict()
            self._write_atomically(data)
        return entry

    def forget(self, key: str) -> None:
        """Drop the entry for ``key`` (e.g. to force a fresh run next time)."""
        if not self._path.exists():
            return
        with self._locked():
            data = self._read()
            if data.get("entries", {}).pop(key, None) is not None:
                self._write_atomically(data)


def _job_is_failed(job: dict[str, Any]) -> bool:
    return str(job.get("status") or "") in _FAILED_STATUSES


def run_idempotent_job(
    *,
    store: JobIdempotencyStore | None,
    key: str | None,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    force_rerun: bool,
    create: Callable[[], dict[str, Any]],
    fetch: Callable[[str], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Probe-before-create dispatch for an idempotent job run.

    Returns ``(job, replayed)``. When ``key`` (and a ``store``) are given and a
    prior non-failed run exists, that prior job is fetched and returned
    (``replayed=True``) instead of creating a new one. A prior *failed* run, a
    purged (404) prior job, or ``force_rerun=True`` falls through to a fresh
    ``create()`` (which is then recorded under ``key``).

    Args:
        store: The dedup store, or ``None`` to disable dedup entirely.
        key: The caller's idempotency key, or ``None`` to disable dedup.
        component_id / config_id: Identify the job; also used to detect a key
            reused for a *different* job (raises rather than return a wrong job).
        branch_id: Recorded with the entry (informational).
        force_rerun: If True, always create a new job (and overwrite the entry).
        create: Thunk that creates the job and returns its dict.
        fetch: ``job_id -> job dict`` (Queue ``get_job_detail``); used to
            resurrect a prior job's current state.

    Raises:
        KeboolaApiError: If ``key`` was already recorded for a different
            component/config (``ErrorCode.INVALID_ARGUMENT``).
    """
    if not key or store is None:
        return create(), False

    existing = store.lookup(key)
    # ``force_rerun`` is the documented escape hatch: it INTENTIONALLY bypasses
    # the collision guard below (and the prior-job probe), creating a fresh job
    # and overwriting the stored entry. Do not hoist the collision check out of
    # this ``not force_rerun`` branch -- the error message tells the caller to
    # pass --force-rerun precisely to get past it.
    if existing is not None and not force_rerun:
        if existing.component_id != component_id or existing.config_id != config_id:
            raise KeboolaApiError(
                message=(
                    f"Idempotency key {key!r} was already used for a different job "
                    f"({existing.component_id}/{existing.config_id}); refusing to reuse "
                    f"it for {component_id}/{config_id}. Use a distinct key, or "
                    f"--force-rerun to override."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        prior = _safe_fetch(fetch, existing.job_id)
        if prior is not None and not _job_is_failed(prior):
            logger.info(
                "idempotency: key %r -> returning prior job %s (status=%s)",
                key,
                existing.job_id,
                prior.get("status"),
            )
            return prior, True
        # Prior run failed / was purged -> fall through and re-run.

    job = create()
    store.record(
        key,
        job_id=str(job.get("id", "")),
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
    )
    return job, False


def _safe_fetch(fetch: Callable[[str], dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    """Fetch a prior job's state; treat a 404 (purged job) as "gone" -> re-run.

    Any *other* API error propagates: when we cannot determine the prior job's
    state we must not silently create a duplicate side effect.
    """
    if not job_id:
        return None
    try:
        return fetch(job_id)
    except KeboolaApiError as exc:
        if exc.status_code == 404:
            logger.info("idempotency: prior job %s not found (404); will re-run", job_id)
            return None
        raise
