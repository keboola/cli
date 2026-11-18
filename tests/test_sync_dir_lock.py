"""One kbagent run at a time per sync directory (#827 follow-up, L1-L6).

The lock is an OS lock on ``.keboola/lock/sync.lock``. These tests hold it
from another thread or another process (a subprocess that prints
``acquired`` once it holds the lock -- no sleeps, no timing) and check that
every entry point fails fast with ``SYNC_DIRECTORY_BUSY`` and writes nothing,
that the error keeps its code, ``retryable`` and ``details`` through every
wrapper, and that a killed holder never blocks the next run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from formal_sync_fake import FakeStorage, Workspace
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sync_service import SyncService

TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
BUSY = "SYNC_DIRECTORY_BUSY"
runner = CliRunner()

_HOLDER = """
import sys
from keboola_agent_cli.sync.dir_lock import sync_dir_lock

with sync_dir_lock(sys.argv[1]):
    print("acquired", flush=True)
    sys.stdin.read()
"""


@contextmanager
def held_by_other_process(root: Path) -> Iterator[subprocess.Popen[str]]:
    """A second kbagent process holds *root*'s lock until the block ends (then it is killed)."""
    root.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if line.strip() != "acquired":
            proc.kill()
            _, err = proc.communicate()
            pytest.fail(f"the holder process did not take the lock: {line!r} {err}")
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


@contextmanager
def held_by_other_thread(root: Path) -> Iterator[None]:
    """Another thread of this process holds *root*'s lock until the block ends."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    root.mkdir(parents=True, exist_ok=True)
    acquired, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with sync_dir_lock(root):
                acquired.set()
                release.wait()
        except BaseException as exc:  # reported to the test below
            failure.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert acquired.wait(60), "the holder thread never took the lock"
        assert not failure, failure
        yield
    finally:
        release.set()
        thread.join(60)


def _assert_busy(exc: BaseException, root: Path) -> None:
    assert isinstance(exc, KeboolaApiError), exc
    assert exc.error_code == ErrorCode.SYNC_DIRECTORY_BUSY
    assert exc.retryable is True
    assert Path(exc.details["directory"]) == root.resolve()
    assert exc.details["lock_file"].endswith("sync.lock")


def _store(tmp_path: Path) -> ConfigStore:
    store = ConfigStore(config_dir=tmp_path / "config")
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TOKEN,
            project_name="Production",
            project_id=258,
        ),
    )
    return store


def _cli(store: ConfigStore, service: Any, args: list[str]) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.SyncService") as mock_sync,
    ):
        mock_store.return_value = store
        mock_projects.return_value = ProjectService(config_store=store)
        mock_sync.return_value = service
        return runner.invoke(app, args)


def _busy_envelope(result: Any, root: Path) -> dict[str, Any]:
    assert result.exit_code == 1, result.output
    error = json.loads(result.output)["error"]
    assert error["code"] == BUSY
    assert error["retryable"] is True
    assert Path(error["details"]["directory"]) == root.resolve()
    return error


# ---------------------------------------------------------------------------
# The lock itself (L1-L4)
# ---------------------------------------------------------------------------


def test_lock_file_stays_after_release_and_is_git_ignored(tmp_path: Path) -> None:
    """Risk 10: the native lock keeps its file; lock/ ignores itself."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    with sync_dir_lock(tmp_path):
        pass
    lock_dir = tmp_path / ".keboola" / "lock"
    assert (lock_dir / "sync.lock").is_file()
    assert (lock_dir / ".gitignore").read_bytes() == b"*\n"
    with sync_dir_lock(tmp_path):  # and it can be taken again
        pass


def test_same_thread_nests_another_thread_is_busy(tmp_path: Path) -> None:
    """Risk 1: ownership is per thread, never per process."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    with sync_dir_lock(tmp_path), sync_dir_lock(tmp_path):
        errors: list[BaseException] = []

        def enter() -> None:
            try:
                with sync_dir_lock(tmp_path):
                    pass
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=enter)
        thread.start()
        thread.join(60)
    assert len(errors) == 1
    _assert_busy(errors[0], tmp_path)


def test_other_process_is_busy_and_a_killed_holder_frees_the_lock(tmp_path: Path) -> None:
    """Risk 13: a second process fails fast; a crashed one never blocks the next run."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    with held_by_other_process(tmp_path) as holder:
        with pytest.raises(KeboolaApiError) as caught, sync_dir_lock(tmp_path):
            pass
        _assert_busy(caught.value, tmp_path)
        holder.kill()
        holder.wait()
        _acquire_after_holder_died(tmp_path)


def _acquire_after_holder_died(root: Path) -> None:
    """Take the lock a killed process held.

    The kernel drops the lock with the dead process; Windows may do that a
    moment after the process handle signals, so a busy answer is retried
    within a generous deadline (no fixed sleep decides the outcome).
    """
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    deadline = time.monotonic() + 30
    while True:
        try:
            with sync_dir_lock(root):
                return
        except KeboolaApiError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def test_lock_that_cannot_be_created_is_config_error_not_busy(tmp_path: Path) -> None:
    """Risk 11: an OS error is reported as what it is, never as another run."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    (tmp_path / ".keboola").mkdir()
    (tmp_path / ".keboola" / "lock").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigError) as caught, sync_dir_lock(tmp_path):
        pass
    assert "sync.lock" in caught.value.message
    assert BUSY not in caught.value.message


def test_symlinked_root_is_the_same_lock(tmp_path: Path) -> None:
    """Risk 12: the key is the resolved root, so an alias nests in its owner thread."""
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with sync_dir_lock(real), sync_dir_lock(alias):
        pass
    with held_by_other_thread(real):
        with pytest.raises(KeboolaApiError) as caught, sync_dir_lock(alias):
            pass
        _assert_busy(caught.value, real)


def test_a_forked_child_owns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3: after a fork the child must not inherit the parent's ownership."""
    import threading as _threading

    from keboola_agent_cli.sync import dir_lock

    monkeypatch.setattr(dir_lock, "_owner", _threading.local())
    with dir_lock.sync_dir_lock(tmp_path):
        dir_lock._forget_after_fork()  # what os.register_at_fork runs in a child
        with pytest.raises(KeboolaApiError) as caught, dir_lock.sync_dir_lock(tmp_path):
            pass
        _assert_busy(caught.value, tmp_path)


def test_a_directory_without_keboola_gets_no_lock_folder(tmp_path: Path) -> None:
    """UNLOCKED: a command run in a plain directory leaves nothing behind."""
    from keboola_agent_cli.sync.dir_lock import UNLOCKED, sync_dir_lock

    with sync_dir_lock(tmp_path, outside=UNLOCKED):
        pass
    assert not (tmp_path / ".keboola").exists()


# ---------------------------------------------------------------------------
# Who takes it (L5) and the error on every path (L6)
# ---------------------------------------------------------------------------


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ws(tmp_config_dir: Path, tmp_path: Path, api: FakeStorage) -> Workspace:
    api.add("Orders", ["SELECT 1"])
    return Workspace(tmp_config_dir, tmp_path / "repo", api)


def test_first_pull_is_busy_while_another_run_holds_the_fresh_directory(
    ws: Workspace, api: FakeStorage
) -> None:
    """Risk 4: init and pull are one locked step; the second first-pull does nothing."""
    with held_by_other_thread(ws.root):
        with pytest.raises(KeboolaApiError) as caught:
            ws.service().pull(alias="prod", project_root=ws.root, init_if_missing=True)
        _assert_busy(caught.value, ws.root)
    assert not (ws.root / ".keboola" / "manifest.json").exists()
    assert api.calls == []

    result = ws.service().pull(
        alias="prod", project_root=ws.root, init_if_missing=True, no_storage=True, no_jobs=True
    )
    assert (ws.root / ".keboola" / "manifest.json").exists()
    assert result["status"] != "error"


@pytest.mark.parametrize("operation", ["diff", "push", "status"])
def test_tree_operations_are_busy_and_touch_nothing(
    ws: Workspace, api: FakeStorage, operation: str
) -> None:
    ws.init_and_pull()
    manifest_before = (ws.root / ".keboola" / "manifest.json").read_bytes()
    api.calls.clear()
    svc = ws.service()
    call = {
        "diff": lambda: svc.diff(alias="prod", project_root=ws.root),
        "push": lambda: svc.push(alias="prod", project_root=ws.root),
        "status": lambda: svc.status(project_root=ws.root),
    }[operation]
    with held_by_other_thread(ws.root):
        with pytest.raises(KeboolaApiError) as caught:
            call()
        _assert_busy(caught.value, ws.root)
    assert api.calls == []
    assert (ws.root / ".keboola" / "manifest.json").read_bytes() == manifest_before


def test_cli_pull_auto_init_reports_busy_not_init_error(tmp_path: Path) -> None:
    """Risk 8: the auto-init wrapper keeps the busy code, retryable and details."""
    store = _store(tmp_path)
    client = MagicMock()
    service = SyncService(config_store=store, client_factory=lambda u, t: client)
    root = tmp_path / "fresh"
    with held_by_other_process(root):
        result = _cli(
            store,
            service,
            ["--json", "sync", "pull", "--project", "prod", "--directory", str(root)],
        )
    _busy_envelope(result, root)
    client.verify_token.assert_not_called()
    assert not (root / ".keboola" / "manifest.json").exists()


@pytest.mark.parametrize("command", ["status", "branch-status", "branch-unlink"])
def test_cli_tree_readers_report_busy(ws: Workspace, tmp_path: Path, command: str) -> None:
    ws.init_and_pull()
    with held_by_other_process(ws.root):
        result = _cli(
            ws.store,
            ws.service(),
            ["--json", "sync", command, "--directory", str(ws.root)],
        )
    _busy_envelope(result, ws.root)


@pytest.mark.parametrize("bulk", ["diff_all", "push_all", "pull_all"])
def test_all_projects_keeps_the_busy_code_per_project(
    ws: Workspace, tmp_path: Path, bulk: str
) -> None:
    """Risk 8: --all-projects reports a busy project with its code, retryable and details."""
    base = tmp_path / "all"
    ws.root = base / "prod"
    ws.init_and_pull()
    with held_by_other_thread(ws.root):
        data = getattr(ws.service(), bulk)(base)
    entry = data["projects"]["prod"]
    assert entry["error_code"] == BUSY
    assert entry["retryable"] is True
    assert Path(entry["details"]["directory"]) == ws.root.resolve()
    assert data["summary"]["failed"] == 1


def test_scaffold_placement_never_hides_busy(tmp_path: Path) -> None:
    """Risk 8: the placement fallback (branch-<id>/) must not swallow a busy tree."""
    from keboola_agent_cli.sync import branch_registry
    from keboola_agent_cli.sync.dir_lock import busy_error

    root = tmp_path / "tree"
    (root / ".keboola").mkdir(parents=True)
    (root / ".keboola" / "manifest.json").write_text("{}", encoding="utf-8")
    project = MagicMock(project_id=258)
    with (
        patch.object(branch_registry, "register_branch_dir", side_effect=busy_error(root)),
        pytest.raises(KeboolaApiError) as caught,
    ):
        branch_registry.resolve_scaffold_placement(project, root, 20, MagicMock())
    _assert_busy(caught.value, root)


def test_config_new_push_on_a_busy_tree_creates_nothing(ws: Workspace, tmp_path: Path) -> None:
    """Risk 4: the lock is taken before the POST, so a busy tree creates no config."""
    ws.init_and_pull()
    config_service = MagicMock()
    component_service = MagicMock()
    component_service.generate_scaffold.return_value = {
        "component_id": "keboola.ex-http",
        "directory": "extractor/keboola.ex-http/new",
        "files": [{"path": "_config.yml", "content": "name: new\n"}],
    }
    args = ["--json", "config", "new", "--component-id", "keboola.ex-http", "--project", "prod"]
    args += ["--name", "new", "--push", "--output-dir", str(ws.root)]
    with (
        held_by_other_process(ws.root),
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.ComponentService") as mock_components,
        patch("keboola_agent_cli.cli.ConfigService") as mock_configs,
        patch("keboola_agent_cli.cli.SyncService") as mock_sync,
    ):
        mock_store.return_value = ws.store
        mock_projects.return_value = ProjectService(config_store=ws.store)
        mock_components.return_value = component_service
        mock_configs.return_value = config_service
        mock_sync.return_value = ws.service()
        result = runner.invoke(app, args)
    _busy_envelope(result, ws.root)
    config_service.create_config.assert_not_called()
    assert not list(ws.root.rglob("extractor/keboola.ex-http/new"))


def test_config_rename_into_a_busy_tree_renames_nothing(ws: Workspace, api: FakeStorage) -> None:
    from keboola_agent_cli.services.config_service import ConfigService

    ws.init_and_pull()
    client = MagicMock()
    service = ConfigService(config_store=ws.store, client_factory=lambda u, t: client)
    with held_by_other_thread(ws.root):
        with pytest.raises(KeboolaApiError) as caught:
            service.rename_config(
                alias="prod",
                component_id="keboola.snowflake-transformation",
                config_id="101",
                name="Renamed",
                directory=ws.root,
            )
        _assert_busy(caught.value, ws.root)
    client.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# Clone: source snapshot + fresh target (risks 2, 3)
# ---------------------------------------------------------------------------


def test_copy_into_a_lock_only_target_keeps_its_lock_and_skips_the_sources(
    tmp_path: Path,
) -> None:
    from keboola_agent_cli.sync.clone import copy_reference_tree, is_fresh_target
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock

    source, target = tmp_path / "source", tmp_path / "target"
    (source / ".keboola" / "lock").mkdir(parents=True)
    (source / ".keboola" / "lock" / "source-marker").write_text("x", encoding="utf-8")
    (source / ".keboola" / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "main").mkdir()
    (source / "main" / "f.txt").write_text("data", encoding="utf-8")
    with sync_dir_lock(target):
        assert is_fresh_target(target)
        copy_reference_tree(source, target)
    assert (target / "main" / "f.txt").read_text(encoding="utf-8") == "data"
    assert (target / ".keboola" / "manifest.json").exists()
    assert (target / ".keboola" / "lock" / "sync.lock").exists()
    assert not (target / ".keboola" / "lock" / "source-marker").exists()
    assert not is_fresh_target(target)


def test_existing_unrelated_target_is_still_refused(tmp_path: Path) -> None:
    from keboola_agent_cli.sync.clone import copy_reference_tree, is_fresh_target

    target = tmp_path / "target"
    target.mkdir()
    (target / "notes.txt").write_text("mine", encoding="utf-8")
    assert not is_fresh_target(target)
    with pytest.raises(FileExistsError):
        copy_reference_tree(tmp_path / "source", target)
    assert (target / "notes.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("where", ["same", "inside", "outside"])
def test_clone_refuses_a_target_that_overlaps_the_source(ws: Workspace, where: str) -> None:
    ws.init_and_pull()
    target = {"same": ws.root, "inside": ws.root / "clone", "outside": ws.root.parent}[where]
    with pytest.raises(ConfigError, match="must not be the source"):
        ws.service().clone_project(source=ws.root, target_alias="prod", target_dir=target)
    assert not (ws.root / "clone").exists()


def test_clone_waits_for_no_one_a_busy_source_is_refused(ws: Workspace, tmp_path: Path) -> None:
    """Risk 3: a source another run is pulling is never copied half-way."""
    ws.init_and_pull()
    target = tmp_path / "clone"
    with held_by_other_thread(ws.root):
        with pytest.raises(KeboolaApiError) as caught:
            ws.service().clone_project(source=ws.root, target_alias="prod", target_dir=target)
        _assert_busy(caught.value, ws.root)
    assert not target.exists()


def test_fresh_clone_still_clones(tmp_path: Path, tmp_config_dir: Path) -> None:
    """Risk 2: the target's own lock folder does not turn a fresh target into an old one."""
    from test_sync_clone_links import World

    world = World(tmp_path, tmp_config_dir)
    result = world.clone()
    assert result["status"] == "cloned", result
    assert result["errors"] == []
    assert (world.clone_dir / ".keboola" / "lock" / "sync.lock").exists()
    assert world.clone()["status"] == "no_changes"


def test_clone_into_a_lock_only_target_clones(tmp_path: Path, tmp_config_dir: Path) -> None:
    from keboola_agent_cli.sync.dir_lock import sync_dir_lock
    from test_sync_clone_links import World

    world = World(tmp_path, tmp_config_dir)
    with sync_dir_lock(world.clone_dir):
        pass
    assert world.clone()["status"] == "cloned"


# ---------------------------------------------------------------------------
# Review round 1: a directory that is not a sync tree yet (items 1, 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "body_start"),
    [
        ("diff", "load_project_manifest"),
        ("push", "_reconcile_saved_promote_links"),
        ("status", "load_manifest"),
        ("branch_status", "_branch_status"),
        ("branch_unlink", "_branch_unlink"),
        ("branch_link", "_branch_link"),
    ],
)
def test_a_tree_operation_outside_a_sync_tree_stops_before_its_body(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, operation: str, body_start: str
) -> None:
    """Item 1: no `.keboola/` -> the "no manifest" error first; the body never runs unlocked.

    Before, the body ran unlocked, so a first `sync pull` starting in the same
    directory meanwhile was not serialized with it.
    """
    from keboola_agent_cli.services import sync_service as module

    entered: list[str] = []

    def body_ran(*_: Any, **__: Any) -> Any:
        entered.append(body_start)
        raise FileNotFoundError("the body ran")

    monkeypatch.setattr(module, body_start, body_ran)
    svc = ws.service()
    calls = {
        "diff": lambda: svc.diff(alias="prod", project_root=ws.root),
        "push": lambda: svc.push(alias="prod", project_root=ws.root),
        "status": lambda: svc.status(project_root=ws.root),
        "branch_status": lambda: svc.branch_status(project_root=ws.root),
        "branch_unlink": lambda: svc.branch_unlink(project_root=ws.root),
        "branch_link": lambda: svc.branch_link(alias="prod", project_root=ws.root),
    }
    with pytest.raises(FileNotFoundError) as caught:
        calls[operation]()
    assert entered == []
    assert "manifest" in str(caught.value).lower()
    assert not (ws.root / ".keboola").exists()


def test_config_new_into_a_plain_directory_keeps_its_scaffold_when_a_pull_starts(
    tmp_path: Path,
) -> None:
    """Item 1: a directory that was no sync tree when config new started stays unlocked.

    A first `sync pull` that starts there after the POST takes the lock; the
    scaffold placement must not then fail busy, or the created config would
    have no local files.
    """
    store = _store(tmp_path)
    out = tmp_path / "plain"
    out.mkdir()
    holders: list[Any] = []
    config_service = MagicMock()

    def create_then_a_pull_starts(**_: Any) -> dict[str, Any]:
        holder = held_by_other_process(out)
        holder.__enter__()
        holders.append(holder)
        return {"id": "12345", "name": "new", "branch_id": None, "configuration": None}

    config_service.create_config.side_effect = create_then_a_pull_starts
    component_service = MagicMock()
    component_service.generate_scaffold.return_value = {
        "component_id": "keboola.ex-http",
        "directory": "extractor/keboola.ex-http/new",
        "files": [{"path": "_config.yml", "content": "name: new\n"}],
    }
    args = ["--json", "config", "new", "--component-id", "keboola.ex-http", "--project", "prod"]
    args += ["--name", "new", "--push", "--output-dir", str(out)]
    try:
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
            patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
            patch("keboola_agent_cli.cli.ComponentService") as mock_components,
            patch("keboola_agent_cli.cli.ConfigService") as mock_configs,
            patch("keboola_agent_cli.cli.SyncService") as mock_sync,
        ):
            mock_store.return_value = store
            mock_projects.return_value = ProjectService(config_store=store)
            mock_components.return_value = component_service
            mock_configs.return_value = config_service
            mock_sync.return_value = SyncService(config_store=store)
            result = runner.invoke(app, args)
    finally:
        for holder in holders:
            holder.__exit__(None, None, None)
    assert result.exit_code == 0, result.output
    assert (out / "extractor/keboola.ex-http/new/_config.yml").exists()


def test_clone_into_an_unrelated_directory_leaves_no_lock_there(
    ws: Workspace, tmp_path: Path
) -> None:
    """Item 6: the target is checked before its lock folder is created."""
    ws.init_and_pull()
    target = tmp_path / "unrelated"
    target.mkdir()
    (target / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        ws.service().clone_project(source=ws.root, target_alias="prod", target_dir=target)
    assert not (target / ".keboola").exists()
    assert [p.name for p in target.iterdir()] == ["notes.txt"]


# ---------------------------------------------------------------------------
# Review round 2: decided once per command (A), an empty clone target (B)
# ---------------------------------------------------------------------------


def _pulled_manifest(root: Path) -> bytes:
    """What a first `sync pull` into *root* leaves: a manifest with a tracked config."""
    import json as _json

    (root / ".keboola").mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 2,
        "project": {"id": 258, "apiHost": "connection.keboola.com"},
        "naming": {},
        "branches": [{"id": 10, "path": "main"}],
        "configurations": [
            {"branchId": 10, "componentId": "keboola.ex-http", "id": "777", "path": "ex/old"}
        ],
    }
    path = root / ".keboola" / "manifest.json"
    path.write_text(_json.dumps(manifest), encoding="utf-8")
    (root / "main" / "ex" / "old").mkdir(parents=True)
    return path.read_bytes()


def test_config_new_into_a_plain_directory_never_touches_a_manifest_a_pull_writes(
    tmp_path: Path,
) -> None:
    """Round 2, A: a folder that was no sync tree when the command started stays plain.

    A first `sync pull` initialises the folder after the POST; config new must
    not load or save that manifest (registering its branch over the pull's
    entries), and writes its scaffold flat, as into any plain folder.
    """
    store = _store(tmp_path)
    out = tmp_path / "plain"
    out.mkdir()
    pulled: list[bytes] = []
    config_service = MagicMock()

    def create_then_a_pull_writes(**_: Any) -> dict[str, Any]:
        pulled.append(_pulled_manifest(out))
        return {"id": "12345", "name": "new", "branch_id": 20, "configuration": None}

    config_service.create_config.side_effect = create_then_a_pull_writes
    component_service = MagicMock()
    component_service.generate_scaffold.return_value = {
        "component_id": "keboola.ex-http",
        "directory": "extractor/keboola.ex-http/new",
        "files": [{"path": "_config.yml", "content": "name: new\n"}],
    }
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.list_dev_branches.return_value = [{"id": 20, "name": "dev-x"}]
    args = ["--json", "config", "new", "--component-id", "keboola.ex-http", "--project", "prod"]
    args += ["--name", "new", "--push", "--branch", "20", "--output-dir", str(out)]
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.ComponentService") as mock_components,
        patch("keboola_agent_cli.cli.ConfigService") as mock_configs,
        patch("keboola_agent_cli.cli.SyncService") as mock_sync,
    ):
        mock_store.return_value = store
        mock_projects.return_value = ProjectService(config_store=store)
        mock_components.return_value = component_service
        mock_configs.return_value = config_service
        mock_sync.return_value = SyncService(config_store=store, client_factory=lambda u, t: client)
        result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert (out / ".keboola" / "manifest.json").read_bytes() == pulled[0]
    assert (out / "extractor/keboola.ex-http/new/_config.yml").exists()
    client.list_dev_branches.assert_not_called()


def test_config_rename_into_a_plain_directory_never_touches_a_manifest_a_pull_writes(
    tmp_path: Path,
) -> None:
    """Round 2, A: rename decides at its start; a manifest a pull writes meanwhile is left alone."""
    from keboola_agent_cli.services.config_service import ConfigService

    store = _store(tmp_path)
    out = tmp_path / "plain"
    out.mkdir()
    pulled: list[bytes] = []
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "777", "name": "Old"}

    def renamed_then_a_pull_writes(**_: Any) -> dict[str, Any]:
        pulled.append(_pulled_manifest(out))
        return {"id": "777", "name": "New"}

    client.update_config.side_effect = renamed_then_a_pull_writes
    service = ConfigService(config_store=store, client_factory=lambda u, t: client)

    result = service.rename_config(
        alias="prod",
        component_id="keboola.ex-http",
        config_id="777",
        name="New",
        directory=out,
    )

    assert "sync" not in result
    assert "sync_skipped" not in result
    assert (out / ".keboola" / "manifest.json").read_bytes() == pulled[0]
    assert (out / "main" / "ex" / "old").is_dir()


@pytest.mark.parametrize("dry_run", [False, True])
def test_clone_into_an_existing_empty_directory_clones(
    tmp_path: Path, tmp_config_dir: Path, dry_run: bool
) -> None:
    """Round 2, B: an empty target folder is a fresh target, as before the lock."""
    from test_sync_clone_links import World

    world = World(tmp_path, tmp_config_dir)
    world.clone_dir.mkdir()
    result = world.clone(dry_run=dry_run)
    assert result["status"] == ("dry_run" if dry_run else "cloned"), result


# ---------------------------------------------------------------------------
# PR #1016 review NB-2 (revised): a read-only lock folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["status", "diff"])
def test_readers_need_a_writable_lock_folder(ws: Workspace, operation: str) -> None:
    """Fail closed: a reader that cannot take the lock does not run unlocked.

    Not being able to write the lock file does not mean nobody else writes the
    tree (another user's `sync pull` may hold it), so status and diff stop
    with CONFIG_ERROR naming the lock file instead of reading a tree that may
    be changing.
    """
    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("chmod does not make a file read-only here")
    ws.init_and_pull()
    lock_dir = ws.root / ".keboola" / "lock"
    targets = [lock_dir / "sync.lock", lock_dir]
    for path in targets:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        try:
            with open(lock_dir / "sync.lock", "r+b"):
                pytest.skip("the file system does not enforce the read-only mode")
        except PermissionError:
            pass
        svc = ws.service()
        with pytest.raises(ConfigError) as caught:
            if operation == "status":
                svc.status(project_root=ws.root)
            else:
                svc.diff(alias="prod", project_root=ws.root)
        assert "sync.lock" in caught.value.message
    finally:
        for path in reversed(targets):
            path.chmod(0o755 if path.is_dir() else 0o644)
