"""Tests for the sync trust cluster (issues #466, #467, #472, #497).

Covers three reconcile behaviors added in 0.72:

* Pull re-materialization: a tracked config whose local dir/file is missing
  is re-written from remote instead of being registered with an empty
  ``pull_hash`` (the manifest<->disk invariant, issues #472/#466).
* ``pull --theirs``: remote wins everywhere -- locally-modified configs and
  rows are overwritten, true 3-way conflicts resolve by taking remote, and
  missing files are restored (issue #466).
* Never-fetched guard: a manifest entry with an empty ``pull_hash`` AND no
  file on disk (pre-0.72 phantom) is excluded from diff tracking so
  ``push --force`` never plans a remote DELETE for it (issue #472), and the
  adopted-by-id push path registers a manifest entry after update (#497).

Fixture style mirrors tests/test_sync_force_pull_baseline.py.
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

SAMPLE_VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)

SAMPLE_BRANCHES = [
    {"id": 12345, "name": "Main", "isDefault": True},
]


def _http_extractor(base_url: str, rows: list | None = None) -> list:
    """Single keboola.ex-http config carrying ``base_url`` (+ optional rows)."""
    return [
        {
            "id": "keboola.ex-http",
            "type": "extractor",
            "configurations": [
                {
                    "id": "cfg-001",
                    "name": "My HTTP Extractor",
                    "description": "Fetches data",
                    "configuration": {"parameters": {"baseUrl": base_url}},
                    "rows": rows if rows is not None else [],
                }
            ],
        },
    ]


def _row(path: str) -> dict:
    return {
        "id": "row-001",
        "name": "Users Endpoint",
        "description": "",
        "configuration": {"parameters": {"path": path}},
    }


REMOTE_V1 = _http_extractor("https://api.example.com")
REMOTE_V2 = _http_extractor("https://api-v2.example.com")  # remote moved on
REMOTE_V1_WITH_ROW = _http_extractor("https://api.example.com", rows=[_row("/users")])


def _make_mock_client(
    verify_token_response: TokenVerifyResponse | None = None,
    components_response: list | None = None,
    branches_response: list | None = None,
) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if verify_token_response:
        client.verify_token.return_value = verify_token_response
    if components_response is not None:
        client.list_components_with_configs.return_value = components_response
    if branches_response is not None:
        client.list_dev_branches.return_value = branches_response
    return client


def _svc(store: ConfigStore, components: list | None = None) -> SyncService:
    """SyncService whose factory mints a fresh mock client per call."""
    return SyncService(
        config_store=store,
        client_factory=lambda url, token: _make_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            components_response=components,
            branches_response=SAMPLE_BRANCHES,
        ),
    )


def _svc_with_client(store: ConfigStore, client: MagicMock) -> SyncService:
    """SyncService bound to one shared mock client (for call assertions)."""
    return SyncService(config_store=store, client_factory=lambda url, token: client)


def _init_and_pull(tmp_config_dir: Path, project_root: Path, remote: list) -> ConfigStore:
    """init + first pull, returning the ConfigStore."""
    store = setup_single_project(tmp_config_dir)
    _svc(store).init_sync(alias="prod", project_root=project_root)
    _svc(store, remote).pull(alias="prod", project_root=project_root)
    return store


def _config_yml(project_root: Path, under_rows: bool) -> Path:
    """Locate the config or row _config.yml under the pulled tree."""
    files = list(project_root.rglob(CONFIG_FILENAME))
    matches = [f for f in files if ("rows" in f.parts) == under_rows]
    assert len(matches) == 1, f"expected exactly one {'row' if under_rows else 'config'} file"
    return matches[0]


def _edit_param(config_file: Path, key: str, value: str) -> None:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["parameters"][key] = value
    config_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _entry_config_dir(project_root: Path) -> Path:
    """Absolute dir of the first (only) tracked config under ``main/``."""
    manifest = load_manifest(project_root)
    return project_root / "main" / manifest.configurations[0].path


def _phantomize_entry(project_root: Path) -> str:
    """Blank the first entry's pull hashes (the pre-0.72 phantom state).

    Returns the entry's relative path under the branch dir. The caller
    decides whether the on-disk dir is also removed.
    """
    manifest = load_manifest(project_root)
    entry = manifest.configurations[0]
    entry.metadata["pull_hash"] = ""
    entry.metadata["pull_config_hash"] = ""
    save_manifest(project_root, manifest)
    return entry.path


# ===================================================================
# Pull re-materialization (manifest<->disk invariant, issues #472/#466)
# ===================================================================


def test_pull_rematerializes_deleted_dir_when_remote_unchanged(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Deleted config dir + unchanged remote -> pull re-writes the files.

    Pre-fix, ``remote_unchanged`` short-circuited on matching hashes alone and
    the manifest was re-registered with an empty ``pull_hash`` (a phantom).
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_dir = _entry_config_dir(project_root)
    entry_path = load_manifest(project_root).configurations[0].path
    shutil.rmtree(config_dir)

    result = _svc(store, REMOTE_V1).pull("prod", project_root)

    config_file = config_dir / CONFIG_FILENAME
    assert config_file.exists(), "deleted config dir was not re-materialized"
    assert result["configs_pulled"] == 1
    actions = [(d["action"], d["path"]) for d in result["details"]]
    assert ("updated", entry_path) in actions

    entry_after = load_manifest(project_root).configurations[0]
    assert entry_after.metadata["pull_hash"], "pull_hash must be re-stamped non-empty"
    assert entry_after.metadata["pull_config_hash"]


def test_pull_rematerializes_phantom_entry_with_empty_hashes(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Pre-0.72 phantom (empty hashes + missing dir) heals on the next pull."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_dir = _entry_config_dir(project_root)
    entry_path = _phantomize_entry(project_root)
    shutil.rmtree(config_dir)

    result = _svc(store, REMOTE_V1).pull("prod", project_root)

    assert (config_dir / CONFIG_FILENAME).exists()
    actions = [(d["action"], d["path"]) for d in result["details"]]
    assert ("updated", entry_path) in actions

    entry_after = load_manifest(project_root).configurations[0]
    assert entry_after.metadata["pull_hash"]
    assert entry_after.metadata["pull_config_hash"]


# ===================================================================
# Never-fetched guard in diff / push (issue #472)
# ===================================================================


def test_diff_phantom_entry_is_never_fetched_not_deleted(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Phantom entry surfaces under ``never_fetched``, never as ``deleted``."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    entry_path = _phantomize_entry(project_root)
    shutil.rmtree(_entry_config_dir(project_root))

    diff = _svc(store, REMOTE_V1).diff("prod", project_root)

    assert diff["summary"]["deleted"] == 0
    assert diff["summary"]["never_fetched"] == 1
    assert diff["never_fetched"] == [
        {
            "component_id": "keboola.ex-http",
            "config_id": "cfg-001",
            "path": entry_path,
        }
    ]
    deleted = [c for c in diff["changes"] if c["change_type"] == "deleted"]
    assert deleted == []
    # The phantom's remote config is registered (just never fetched); it must
    # not double-report as a brand-new remote-only config.
    assert all(item["config_id"] != "cfg-001" for item in diff["remote_only"])


def test_push_force_with_phantom_entry_does_not_delete_remote(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The issue #472 disaster case: push --force must NOT plan a remote DELETE."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    _phantomize_entry(project_root)
    shutil.rmtree(_entry_config_dir(project_root))

    client = _make_mock_client(components_response=REMOTE_V1)
    result = _svc_with_client(store, client).push("prod", project_root, force=True)

    assert result["status"] == "no_changes"
    assert result["never_fetched"], "envelope must carry the never_fetched warning"
    client.delete_config.assert_not_called()


def test_push_force_still_deletes_when_pull_hash_recorded(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Contrast: a properly-pulled entry whose dir was removed IS a delete.

    The never-fetched guard must not swallow the legitimate GitOps flow
    "delete the local dir -> push removes the remote config".
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    shutil.rmtree(_entry_config_dir(project_root))

    client = _make_mock_client(components_response=REMOTE_V1)
    svc = _svc_with_client(store, client)

    diff = svc.diff("prod", project_root)
    assert diff["summary"]["deleted"] == 1
    assert diff["summary"]["never_fetched"] == 0

    result = svc.push("prod", project_root, force=True)

    assert result["status"] == "pushed"
    assert result["deleted"] == 1
    client.delete_config.assert_called_once()
    assert client.delete_config.call_args.kwargs["config_id"] == "cfg-001"
    assert load_manifest(project_root).configurations == []


def test_diff_phantom_parent_rows_produce_no_row_deletes(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Manifest rows under a never-fetched parent are excluded from tracking."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1_WITH_ROW)

    # Sanity: the pulled entry tracks the row before we phantomize.
    assert load_manifest(project_root).configurations[0].rows

    _phantomize_entry(project_root)
    shutil.rmtree(_entry_config_dir(project_root))

    diff = _svc(store, REMOTE_V1_WITH_ROW).diff("prod", project_root)

    row_deletes = [c for c in diff["changes"] if c["change_type"] == "deleted" and c.get("is_row")]
    assert row_deletes == []
    assert diff["changes"] == []
    assert diff["summary"]["never_fetched"] == 1


def test_diff_empty_pull_hash_with_file_present_stays_tracked(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Empty pull_hash but file ON disk is not a phantom -- it diffs normally."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    _phantomize_entry(project_root)  # blank hashes, file stays on disk

    diff = _svc(store, REMOTE_V1).diff("prod", project_root)
    assert diff["never_fetched"] == []
    assert diff["summary"]["never_fetched"] == 0
    assert diff["changes"] == []  # local file still equals remote

    # And a real local edit is detected as a pushable modification.
    _edit_param(_config_yml(project_root, under_rows=False), "baseUrl", "https://x.example.com")
    diff_after = _svc(store, REMOTE_V1).diff("prod", project_root)
    assert diff_after["summary"]["modified"] == 1
    assert diff_after["summary"]["never_fetched"] == 0


# ===================================================================
# pull --theirs (issue #466)
# ===================================================================


def test_theirs_overwrites_locally_modified_config(tmp_config_dir: Path, tmp_path: Path) -> None:
    """--theirs discards a local edit and re-writes the remote content."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_file = _config_yml(project_root, under_rows=False)
    _edit_param(config_file, "baseUrl", "https://changed.example.com")

    result = _svc(store, REMOTE_V1).pull("prod", project_root, theirs=True)

    after = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert after["parameters"]["baseUrl"] == "https://api.example.com"
    assert result["configs_pulled"] == 1
    skipped = [d for d in result["details"] if d["action"] == "skipped"]
    assert skipped == [], "--theirs must not preserve locally-modified files"
    assert any(d["action"] == "updated" for d in result["details"])

    # Tree is fully in sync afterwards.
    diff_after = _svc(store, REMOTE_V1).diff("prod", project_root)
    assert diff_after["summary"]["modified"] == 0
    assert diff_after["changes"] == []


def test_theirs_resolves_true_conflict_by_taking_remote(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Local edited + remote changed: --theirs never raises, remote wins.

    Covered both with ``force=True`` (the guard is skipped) and with
    ``--theirs`` alone.
    """
    store = setup_single_project(tmp_config_dir)

    def bootstrap(root: Path) -> Path:
        root.mkdir()
        _svc(store).init_sync(alias="prod", project_root=root)
        _svc(store, REMOTE_V1).pull(alias="prod", project_root=root)
        config_file = _config_yml(root, under_rows=False)
        _edit_param(config_file, "baseUrl", "https://changed.example.com")
        return config_file

    # force=True + theirs=True: SyncConflictError guard must be skipped.
    file_a = bootstrap(tmp_path / "project-a")
    _svc(store, REMOTE_V2).pull("prod", tmp_path / "project-a", force=True, theirs=True)
    after_a = yaml.safe_load(file_a.read_text(encoding="utf-8"))
    assert after_a["parameters"]["baseUrl"] == "https://api-v2.example.com"

    # theirs=True alone works identically.
    file_b = bootstrap(tmp_path / "project-b")
    _svc(store, REMOTE_V2).pull("prod", tmp_path / "project-b", theirs=True)
    after_b = yaml.safe_load(file_b.read_text(encoding="utf-8"))
    assert after_b["parameters"]["baseUrl"] == "https://api-v2.example.com"


def test_theirs_overwrites_locally_modified_row(tmp_config_dir: Path, tmp_path: Path) -> None:
    """--theirs also discards row-level local edits (remote row wins)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1_WITH_ROW)

    row_file = _config_yml(project_root, under_rows=True)
    _edit_param(row_file, "path", "/changed")

    result = _svc(store, REMOTE_V1_WITH_ROW).pull("prod", project_root, theirs=True)

    after = yaml.safe_load(row_file.read_text(encoding="utf-8"))
    assert after["parameters"]["path"] == "/users"
    assert result["rows_pulled"] == 1
    assert not any(d.get("reason") == "row locally modified" for d in result["details"]), (
        "--theirs must not preserve locally-modified rows"
    )


def test_theirs_idempotent_when_tree_matches_pull_state(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Untouched tree + unchanged remote: --theirs re-writes nothing."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1_WITH_ROW)

    result = _svc(store, REMOTE_V1_WITH_ROW).pull("prod", project_root, theirs=True)

    assert result["configs_pulled"] == 0
    assert result["rows_pulled"] == 0


def test_theirs_restores_deleted_config_dir(tmp_config_dir: Path, tmp_path: Path) -> None:
    """--theirs re-materializes a deleted config dir from remote."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_dir = _entry_config_dir(project_root)
    shutil.rmtree(config_dir)

    _svc(store, REMOTE_V1).pull("prod", project_root, theirs=True)

    config_file = config_dir / CONFIG_FILENAME
    assert config_file.exists()
    after = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert after["parameters"]["baseUrl"] == "https://api.example.com"


# ===================================================================
# Adopted-by-id push writes the manifest entry (issue #497)
# ===================================================================


def test_adopted_by_id_push_registers_manifest_entry(tmp_config_dir: Path, tmp_path: Path) -> None:
    """Pushing an adopted (untracked, known-id) config records it in the manifest.

    Pre-#497 the update succeeded but no entry was written, so every
    subsequent diff re-adopted the file and a later local delete was
    invisible (never classified ``deleted``).
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    adopted_path = "extractor/keboola.ex-http/adopted-extractor"
    adopted_dir = project_root / "main" / adopted_path
    adopted_dir.mkdir(parents=True)
    (adopted_dir / CONFIG_FILENAME).write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": "Adopted Extractor",
                "description": "",
                "parameters": {"baseUrl": "https://local-edit.example.com"},
                "_keboola": {
                    "component_id": "keboola.ex-http",
                    "config_id": "cfg-777",
                },
            }
        ),
        encoding="utf-8",
    )

    remote = _http_extractor("https://api.example.com")
    remote[0]["configurations"].append(
        {
            "id": "cfg-777",
            "name": "Adopted Extractor",
            "description": "",
            "configuration": {"parameters": {"baseUrl": "https://original.example.com"}},
            "rows": [],
        }
    )
    client = _make_mock_client(components_response=remote)
    svc = _svc_with_client(store, client)

    diff = svc.diff("prod", project_root)
    assert diff["summary"]["added"] == 0
    assert diff["summary"]["modified"] == 1

    result = svc.push("prod", project_root)

    assert result["status"] == "pushed"
    assert result["created"] == 0
    assert result["updated"] == 1
    client.create_config.assert_not_called()
    client.update_config.assert_called_once()
    assert client.update_config.call_args.kwargs["config_id"] == "cfg-777"

    # The manifest ON DISK now tracks the adopted config with real hashes.
    saved = load_manifest(project_root)
    entries = [
        c for c in saved.configurations if c.component_id == "keboola.ex-http" and c.id == "cfg-777"
    ]
    assert len(entries) == 1
    assert entries[0].path == adopted_path
    assert entries[0].metadata["pull_hash"]
    assert entries[0].metadata["pull_config_hash"]
