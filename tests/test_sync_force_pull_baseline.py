"""Regression tests for the `sync pull --force` baseline-corruption bug.

Field report (kbagent v0.51.1, project 5785): a user has un-pushed local edits
to config A, then runs ``sync pull --force`` (typically to resolve an
*unrelated* config B's conflict). ``--force`` bypassed the
``locally_modified`` guard in ``SyncService.pull()``, so when config A's remote
was unchanged the ``remote_unchanged`` short-circuit re-stamped A's manifest
``pull_hash`` from the *edited on-disk file*. Afterwards ``sync diff`` /
``sync push`` believed A was in sync and silently shipped nothing -- the local
edits were stranded while the remote still held the old config.

The fix splits ``--force`` behaviour by 3-way diff state:

* (b) local edited, remote UNCHANGED  -> preserve the file AND the 3-way base,
      so the pending delta stays visible to ``sync push`` (no data loss).
* (a) local edited, remote ALSO changed (true merge conflict) -> abort the pull
      with ``SyncConflictError`` before writing anything; the user resolves.

These tests pin both halves, at config and row granularity.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import SyncConflictError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService

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


# Remote states used across the tests.
REMOTE_V1 = _http_extractor("https://api.example.com")
REMOTE_V2 = _http_extractor("https://api-v2.example.com")  # remote moved on
REMOTE_V1_WITH_ROW = _http_extractor("https://api.example.com", rows=[_row("/users")])
REMOTE_V2_WITH_ROW = _http_extractor("https://api.example.com", rows=[_row("/people")])


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


def _svc(store, components: list | None = None) -> SyncService:
    return SyncService(
        config_store=store,
        client_factory=lambda url, token: _make_mock_client(
            verify_token_response=SAMPLE_VERIFY_TOKEN,
            components_response=components,
            branches_response=SAMPLE_BRANCHES,
        ),
    )


def _init_and_pull(tmp_config_dir: Path, project_root: Path, remote: list) -> object:
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


# ===================================================================
# Config-level
# ===================================================================


def test_force_pull_preserves_unpushed_local_edits(tmp_config_dir: Path, tmp_path: Path) -> None:
    """(b) force-pull, remote unchanged -> local edit preserved, still pushable."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_file = _config_yml(project_root, under_rows=False)
    _edit_param(config_file, "baseUrl", "https://changed.example.com")

    # Healthy before the force-pull.
    assert _svc(store, REMOTE_V1).diff("prod", project_root)["summary"]["modified"] == 1

    # Force-pull with the SAME remote (run to adopt some *other* config's state).
    _svc(store, REMOTE_V1).pull("prod", project_root, force=True)

    # The on-disk file still holds the edit (force did not revert it).
    after = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert after["parameters"]["baseUrl"] == "https://changed.example.com"

    # And the pending delta is STILL detected -- the bug stranded it silently.
    diff_after = _svc(store, REMOTE_V1).diff("prod", project_root)
    assert diff_after["summary"]["modified"] == 1, (
        "force-pull (remote unchanged) silently dropped the un-pushed local edit"
    )
    assert diff_after["changes"][0]["change_type"] == "modified"


def test_force_pull_aborts_on_true_conflict(tmp_config_dir: Path, tmp_path: Path) -> None:
    """(a) force-pull, remote ALSO changed -> abort with SyncConflictError."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_file = _config_yml(project_root, under_rows=False)
    _edit_param(config_file, "baseUrl", "https://changed.example.com")

    with pytest.raises(SyncConflictError) as excinfo:
        _svc(store, REMOTE_V2).pull("prod", project_root, force=True)

    conflicts = excinfo.value.conflicts
    assert len(conflicts) == 1
    assert conflicts[0]["scope"] == "config"
    assert conflicts[0]["component_id"] == "keboola.ex-http"
    assert conflicts[0]["config_id"] == "cfg-001"

    # Abort left the local edit intact (nothing was written).
    after = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert after["parameters"]["baseUrl"] == "https://changed.example.com"

    # The conflict remains visible to diff (base preserved, not corrupted).
    diff_after = _svc(store, REMOTE_V2).diff("prod", project_root)
    assert diff_after["summary"].get("conflict", 0) == 1


def test_force_pull_no_conflict_when_only_remote_changed(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Remote changed but local untouched is NOT a conflict -- force takes remote."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    # No local edit; remote moved to V2.
    _svc(store, REMOTE_V2).pull("prod", project_root, force=True)

    config_file = _config_yml(project_root, under_rows=False)
    after = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert after["parameters"]["baseUrl"] == "https://api-v2.example.com"


# ===================================================================
# Row-level (same 3-way rule per row)
# ===================================================================


def test_force_pull_preserves_unpushed_row_edits(tmp_config_dir: Path, tmp_path: Path) -> None:
    """(b) row edited, remote unchanged -> row preserved, still pushable."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1_WITH_ROW)

    row_file = _config_yml(project_root, under_rows=True)
    _edit_param(row_file, "path", "/changed")

    _svc(store, REMOTE_V1_WITH_ROW).pull("prod", project_root, force=True)

    after = yaml.safe_load(row_file.read_text(encoding="utf-8"))
    assert after["parameters"]["path"] == "/changed"

    diff_after = _svc(store, REMOTE_V1_WITH_ROW).diff("prod", project_root)
    assert diff_after["summary"]["modified"] == 1, "force-pull stranded the un-pushed row edit"


def test_force_pull_aborts_on_row_conflict(tmp_config_dir: Path, tmp_path: Path) -> None:
    """(a) row edited AND remote row changed -> abort with SyncConflictError."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1_WITH_ROW)

    row_file = _config_yml(project_root, under_rows=True)
    _edit_param(row_file, "path", "/changed")

    with pytest.raises(SyncConflictError) as excinfo:
        _svc(store, REMOTE_V2_WITH_ROW).pull("prod", project_root, force=True)

    scopes = {c["scope"] for c in excinfo.value.conflicts}
    assert "row" in scopes


# ===================================================================
# --all-projects keeps the conflict structured (not a flat string)
# ===================================================================


def test_force_pull_all_projects_emits_structured_conflict(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """`pull_all` must surface SYNC_CONFLICT + conflicts, not just `str(exc)`."""
    base_dir = tmp_path / "base"
    project_root = base_dir / "prod"
    project_root.mkdir(parents=True)
    store = _init_and_pull(tmp_config_dir, project_root, REMOTE_V1)

    config_file = _config_yml(project_root, under_rows=False)
    _edit_param(config_file, "baseUrl", "https://changed.example.com")

    # Remote moved on -> conflict. pull_all catches it per-project; assert it
    # keeps the structured payload a JSON consumer needs (not a flat string).
    result = _svc(store, REMOTE_V2).pull_all(base_dir, force=True)
    proj = result["projects"]["prod"]
    assert proj.get("error_code") == "SYNC_CONFLICT"
    assert proj.get("conflicts"), "conflicts list must be present on the error entry"
    assert proj["conflicts"][0]["config_id"] == "cfg-001"
    assert result["summary"]["failed"] == 1
    assert result["summary"]["success"] == 0
