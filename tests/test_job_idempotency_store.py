"""Tests for the client-side job idempotency store + dispatch (issue #427).

Covers the persistence (record/lookup/forget, atomic file, 0600, corrupt-file
tolerance) and the probe-before-create policy in ``run_idempotent_job``:
return-prior on non-failed, re-run on failed/404, force_rerun, and the
collision guard when a key is reused for a different component/config.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.job_idempotency_store import (
    JobIdempotencyEntry,
    JobIdempotencyStore,
    run_idempotent_job,
)


@pytest.fixture
def store(tmp_path: Path) -> JobIdempotencyStore:
    return JobIdempotencyStore(tmp_path / "job_idempotency.json")


def _entry(store: JobIdempotencyStore, key: str) -> JobIdempotencyEntry:
    """lookup() that asserts a hit -- keeps the assertions terse and type-clean."""
    got = store.lookup(key)
    assert got is not None, f"expected a recorded entry for {key!r}"
    return got


class _Create:
    """A create() thunk returning a fixed job dict, recording its call count."""

    def __init__(self, job: dict[str, Any]) -> None:
        self._job = job
        self.calls: list[int] = []

    def __call__(self) -> dict[str, Any]:
        self.calls.append(1)
        return self._job


class TestStorePersistence:
    def test_lookup_unseen_key_is_none(self, store: JobIdempotencyStore) -> None:
        assert store.lookup("nope") is None

    def test_record_then_lookup(self, store: JobIdempotencyStore) -> None:
        entry = store.record("K1", job_id="job-1", component_id="c", config_id="cfg", branch_id=7)
        assert isinstance(entry, JobIdempotencyEntry)
        got = _entry(store, "K1")
        assert got.job_id == "job-1"
        assert got.component_id == "c"
        assert got.config_id == "cfg"
        assert got.branch_id == 7
        assert got.created_at  # ISO timestamp set

    def test_record_overwrites(self, store: JobIdempotencyStore) -> None:
        store.record("K1", job_id="old", component_id="c", config_id="cfg")
        store.record("K1", job_id="new", component_id="c", config_id="cfg")
        assert _entry(store, "K1").job_id == "new"

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "idem.json"
        JobIdempotencyStore(path).record("K", job_id="j", component_id="c", config_id="cfg")
        # A fresh store object (simulating a process restart) sees the entry.
        assert _entry(JobIdempotencyStore(path), "K").job_id == "j"

    def test_forget(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="j", component_id="c", config_id="cfg")
        store.forget("K")
        assert store.lookup("K") is None
        # forgetting an unknown key (or with no file yet) is a no-op, not an error
        store.forget("missing")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode")
    def test_file_is_0600(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="j", component_id="c", config_id="cfg")
        assert (store.path.stat().st_mode & 0o777) == 0o600

    def test_valid_json_on_disk(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="j", component_id="c", config_id="cfg", branch_id=None)
        data = json.loads(store.path.read_text())
        assert data["version"] == 1
        assert data["entries"]["K"]["job_id"] == "j"
        assert data["entries"]["K"]["branch_id"] is None

    def test_corrupt_file_is_tolerated(self, store: JobIdempotencyStore) -> None:
        store.path.write_text("{ this is not valid json")
        # A corrupt dedup file must never wedge a job run.
        assert store.lookup("anything") is None
        # and a subsequent record recovers the file
        store.record("K", job_id="j", component_id="c", config_id="cfg")
        assert _entry(store, "K").job_id == "j"

    def test_concurrent_distinct_keys_dont_clobber(self, store: JobIdempotencyStore) -> None:
        # Read-modify-write under lock: recording many keys keeps them all.
        for i in range(20):
            store.record(f"K{i}", job_id=f"j{i}", component_id="c", config_id="cfg")
        for i in range(20):
            assert _entry(store, f"K{i}").job_id == f"j{i}"

    def test_parallel_processes_do_not_lose_entries(self, store: JobIdempotencyStore) -> None:
        """Real cross-process contention, which the loop above does not exercise.

        `record()` is a read-modify-write, so without a lock that actually holds
        across processes two writers interleave and one silently drops the
        other's entry. A dropped entry is not cosmetic: the next replay of that
        key finds nothing and creates a **duplicate job** -- the exact side
        effect this store exists to prevent.

        This is why the lock is `filelock` rather than ConfigStore's `fcntl`
        helper, which is a silent no-op on Windows.

        Confirmed to fail with the lock stubbed out -- as it happens by crashing
        rather than by losing an entry, because every writer builds the same
        `.tmp` path and one renames it out from under another
        (`FileNotFoundError`). Serialising the section fixes both failure modes;
        which one you get is a matter of timing.
        """
        workers, per_worker = 6, 8
        program = (
            "import sys;"
            "from keboola_agent_cli.services.job_idempotency_store import JobIdempotencyStore;"
            "s = JobIdempotencyStore(sys.argv[1]);"
            "w = sys.argv[2];"
            f"[s.record(f'w{{w}}-k{{i}}', job_id=f'j{{w}}{{i}}', component_id='c', config_id='cfg')"
            f" for i in range({per_worker})]"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", program, str(store.path), str(w)])
            for w in range(workers)
        ]
        for proc in procs:
            assert proc.wait(timeout=120) == 0, "a writer subprocess failed"

        for w in range(workers):
            for i in range(per_worker):
                entry = store.lookup(f"w{w}-k{i}")
                assert entry is not None, f"lost entry w{w}-k{i} -- writers were not serialised"
                assert entry.job_id == f"j{w}{i}"


class TestRunIdempotentJob:
    def test_no_key_always_creates(self, store: JobIdempotencyStore) -> None:
        create = _Create({"id": "j1", "status": "processing"})
        job, replayed = run_idempotent_job(
            store=store,
            key=None,
            component_id="c",
            config_id="cfg",
            branch_id=None,
            force_rerun=False,
            create=create,
            fetch=lambda jid: {},
        )
        assert job["id"] == "j1" and replayed is False

    def test_first_call_creates_and_records(self, store: JobIdempotencyStore) -> None:
        create = _Create({"id": "j1", "status": "processing"})
        job, replayed = run_idempotent_job(
            store=store,
            key="K",
            component_id="c",
            config_id="cfg",
            branch_id=5,
            force_rerun=False,
            create=create,
            fetch=lambda jid: {},
        )
        assert job["id"] == "j1" and replayed is False
        assert _entry(store, "K").job_id == "j1"
        assert _entry(store, "K").branch_id == 5

    @pytest.mark.parametrize("prior_status", ["processing", "waiting", "success", "warning"])
    def test_replay_returns_prior_when_not_failed(
        self, store: JobIdempotencyStore, prior_status: str
    ) -> None:
        store.record("K", job_id="prior", component_id="c", config_id="cfg")
        create = _Create({"id": "fresh", "status": "processing"})
        job, replayed = run_idempotent_job(
            store=store,
            key="K",
            component_id="c",
            config_id="cfg",
            branch_id=None,
            force_rerun=False,
            create=create,
            fetch=lambda jid: {"id": jid, "status": prior_status},
        )
        assert job["id"] == "prior" and replayed is True
        assert create.calls == []  # no new job created

    @pytest.mark.parametrize("prior_status", ["error", "terminated", "cancelled"])
    def test_replay_reruns_when_prior_failed(
        self, store: JobIdempotencyStore, prior_status: str
    ) -> None:
        store.record("K", job_id="prior", component_id="c", config_id="cfg")
        create = _Create({"id": "fresh", "status": "processing"})
        job, replayed = run_idempotent_job(
            store=store,
            key="K",
            component_id="c",
            config_id="cfg",
            branch_id=None,
            force_rerun=False,
            create=create,
            fetch=lambda jid: {"id": jid, "status": prior_status},
        )
        assert job["id"] == "fresh" and replayed is False
        assert _entry(store, "K").job_id == "fresh"  # entry overwritten

    def test_force_rerun_ignores_non_failed_prior(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="prior", component_id="c", config_id="cfg")
        create = _Create({"id": "fresh", "status": "processing"})
        fetched: list[str] = []

        def fetch(jid: str) -> dict[str, Any]:
            fetched.append(jid)
            return {"id": jid, "status": "success"}

        job, replayed = run_idempotent_job(
            store=store,
            key="K",
            component_id="c",
            config_id="cfg",
            branch_id=None,
            force_rerun=True,
            create=create,
            fetch=fetch,
        )
        assert job["id"] == "fresh" and replayed is False
        assert fetched == []  # force_rerun short-circuits before fetching prior

    def test_collision_different_job_raises(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="prior", component_id="c", config_id="cfg")
        with pytest.raises(KeboolaApiError) as exc:
            run_idempotent_job(
                store=store,
                key="K",
                component_id="OTHER",
                config_id="cfg",
                branch_id=None,
                force_rerun=False,
                create=_Create({"id": "x", "status": "processing"}),
                fetch=lambda jid: {"id": jid, "status": "processing"},
            )
        assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT

    def test_prior_404_reruns(self, store: JobIdempotencyStore) -> None:
        store.record("K", job_id="gone", component_id="c", config_id="cfg")

        def fetch_404(jid: str) -> dict[str, Any]:
            raise KeboolaApiError("nope", status_code=404, error_code=ErrorCode.NOT_FOUND)

        job, replayed = run_idempotent_job(
            store=store,
            key="K",
            component_id="c",
            config_id="cfg",
            branch_id=None,
            force_rerun=False,
            create=_Create({"id": "fresh", "status": "processing"}),
            fetch=fetch_404,
        )
        assert job["id"] == "fresh" and replayed is False

    def test_prior_fetch_non_404_propagates(self, store: JobIdempotencyStore) -> None:
        # A flaky fetch (e.g. 500) must NOT silently create a duplicate: propagate.
        store.record("K", job_id="prior", component_id="c", config_id="cfg")

        def fetch_500(jid: str) -> dict[str, Any]:
            raise KeboolaApiError("boom", status_code=500, error_code=ErrorCode.API_ERROR)

        with pytest.raises(KeboolaApiError) as exc:
            run_idempotent_job(
                store=store,
                key="K",
                component_id="c",
                config_id="cfg",
                branch_id=None,
                force_rerun=False,
                create=_Create({"id": "fresh", "status": "processing"}),
                fetch=fetch_500,
            )
        assert exc.value.status_code == 500
