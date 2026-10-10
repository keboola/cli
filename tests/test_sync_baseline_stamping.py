"""Regression tests for issue #686 -- push stamps API-derived baselines.

``pull_config_hash`` is the 3-way diff's base and is defined as the hash of
the config *as the API returns it*. ``sync push`` used to recompute it from
the files on disk instead, so any config whose local<->API round-trip is not
hash-stable was reported ``~ REMOTE MODIFIED`` by every later ``sync diff``,
forever, with the tree byte-identical to the remote (18 configs across a
21-project production repo in the field report).

Covered here:

* push -> diff is in sync for a multi-statement SQL transformation;
* push sends the same statement COUNT it pulled, semicolons or not;
* the ``is_disabled`` instance of the same class (remote disabled, local file
  without the key);
* create / row create / row update / Phase C all stamp API-derived hashes;
* a partial (id-only) mutation response triggers a detail fetch, and a failed
  fetch leaves the baseline unstamped with a warning -- never a disk hash;
* the ``config_hash_version`` migration: unversioned entries match leniently,
  versioned ones strictly;
* the legacy push guard that refuses a boundary-only rewrite.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from helpers import setup_single_project
from keboola_agent_cli.constants import (
    CONFIG_FILENAME,
    CONFIG_HASH_VERSION,
    CONFIG_HASH_VERSION_KEY,
)
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest

SQL_COMPONENT = "keboola.snowflake-transformation"

SAMPLE_VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)

SAMPLE_BRANCHES = [{"id": 12345, "name": "Main", "isDefault": True}]


# ---------------------------------------------------------------------------
# Remote fixtures
# ---------------------------------------------------------------------------


def _sql_config(script: list[str], config_id: str = "cfg-sql") -> dict[str, Any]:
    """One SQL transformation config whose single code holds *script*."""
    return {
        "id": config_id,
        "name": "Raw data processing",
        "description": "",
        "configuration": {
            "parameters": {
                "blocks": [{"name": "Block 1", "codes": [{"name": "Code 1", "script": script}]}]
            }
        },
        "rows": [],
    }


def _sql_components(script: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "id": SQL_COMPONENT,
            "type": "transformation",
            "configurations": [_sql_config(script)],
        }
    ]


def _http_components(
    *, is_disabled: bool = False, rows: list | None = None
) -> list[dict[str, Any]]:
    config: dict[str, Any] = {
        "id": "cfg-001",
        "name": "My HTTP Extractor",
        "description": "Fetches data",
        "configuration": {"parameters": {"baseUrl": "https://api.example.com"}},
        "rows": rows or [],
    }
    if is_disabled:
        config["isDisabled"] = True
    return [{"id": "keboola.ex-http", "type": "extractor", "configurations": [config]}]


# ---------------------------------------------------------------------------
# A mock client that behaves like the real API: writes update the state that
# subsequent reads (list / detail) return.
# ---------------------------------------------------------------------------


class FakeApi:
    """Minimal stateful Storage API double for config + row writes."""

    def __init__(self, components: list[dict[str, Any]]):
        self.components = components
        self.update_calls: list[dict[str, Any]] = []
        self.row_update_calls: list[dict[str, Any]] = []
        self.detail_calls = 0
        self.detail_fails = False
        self.partial_responses = False

    # -- lookup helpers -------------------------------------------------
    def _find(self, component_id: str, config_id: str) -> dict[str, Any]:
        for component in self.components:
            if component["id"] != component_id:
                continue
            for config in component["configurations"]:
                if str(config["id"]) == str(config_id):
                    return config
        raise KeyError(f"{component_id}/{config_id}")

    def _response(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"id": config["id"]} if self.partial_responses else dict(config)

    # -- API surface ----------------------------------------------------
    def list_components_with_configs(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        return self.components

    def list_dev_branches(self) -> list[dict[str, Any]]:
        return SAMPLE_BRANCHES

    def verify_token(self) -> TokenVerifyResponse:
        return SAMPLE_VERIFY_TOKEN

    def get_config_detail(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> dict[str, Any]:
        self.detail_calls += 1
        if self.detail_fails:
            raise RuntimeError("boom")
        return dict(self._find(component_id, config_id))

    def get_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        parent = self._find(component_id, config_id)
        for row in parent.get("rows", []):
            if str(row["id"]) == str(row_id):
                return dict(row)
        raise KeyError(row_id)

    def update_config(
        self,
        component_id: str,
        config_id: str,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        description: str | None = None,
        change_description: str = "",
        branch_id: int | None = None,
        is_disabled: bool | None = None,
    ) -> dict[str, Any]:
        config = self._find(component_id, config_id)
        self.update_calls.append({"component_id": component_id, "configuration": configuration})
        if configuration is not None:
            config["configuration"] = configuration
        if name is not None:
            config["name"] = name
        if description is not None:
            config["description"] = description
        if is_disabled is not None:
            config["isDisabled"] = is_disabled
        return self._response(config)

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "id": "cfg-new",
            "name": name,
            "description": description,
            "configuration": configuration,
            "rows": [],
        }
        if is_disabled:
            config["isDisabled"] = True
        for component in self.components:
            if component["id"] == component_id:
                component["configurations"].append(config)
                break
        else:
            self.components.append(
                {"id": component_id, "type": "transformation", "configurations": [config]}
            )
        return self._response(config)

    def create_config_row(
        self,
        component_id: str,
        config_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        is_disabled: bool = False,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        parent = self._find(component_id, config_id)
        row: dict[str, Any] = {
            "id": "row-new",
            "name": name,
            "description": description,
            "configuration": configuration,
        }
        if is_disabled:
            row["isDisabled"] = True
        parent.setdefault("rows", []).append(row)
        return {"id": row["id"]} if self.partial_responses else dict(row)

    def update_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        description: str | None = None,
        is_disabled: bool | None = None,
        change_description: str = "",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        parent = self._find(component_id, config_id)
        for row in parent.get("rows", []):
            if str(row["id"]) != str(row_id):
                continue
            self.row_update_calls.append({"row_id": row_id, "configuration": configuration})
            if configuration is not None:
                row["configuration"] = configuration
            if name is not None:
                row["name"] = name
            if is_disabled is not None:
                row["isDisabled"] = is_disabled
            return {"id": row["id"]} if self.partial_responses else dict(row)
        raise KeyError(row_id)


def _client_for(api: FakeApi) -> MagicMock:
    """Wrap a :class:`FakeApi` in a context-manager mock client."""
    client = MagicMock(wraps=api)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.encrypt_values = MagicMock(side_effect=lambda *a, **k: {})
    return client


def _service(store: Any, api: FakeApi) -> SyncService:
    return SyncService(config_store=store, client_factory=lambda url, token: _client_for(api))


def _init_and_pull(tmp_config_dir: Path, project_root: Path, api: FakeApi) -> Any:
    project_root.mkdir(exist_ok=True)
    store = setup_single_project(tmp_config_dir)
    _service(store, api).init_sync(alias="prod", project_root=project_root)
    _service(store, api).pull(
        alias="prod", project_root=project_root, no_storage=True, no_jobs=True
    )
    return store


def _sql_file(project_root: Path) -> Path:
    matches = list(project_root.rglob("transform.sql"))
    assert len(matches) == 1
    return matches[0]


def _config_file(project_root: Path, under_rows: bool = False) -> Path:
    matches = [f for f in project_root.rglob(CONFIG_FILENAME) if ("rows" in f.parts) == under_rows]
    assert len(matches) == 1
    return matches[0]


def _entry(project_root: Path, config_id: str = "cfg-sql") -> Any:
    manifest = load_manifest(project_root)
    return next(c for c in manifest.configurations if c.id == config_id)


def _remote_script(api: FakeApi) -> list[str]:
    config = api._find(SQL_COMPONENT, "cfg-sql")
    return config["configuration"]["parameters"]["blocks"][0]["codes"][0]["script"]


# ===================================================================
# The reporter's regression test: push -> diff must be in sync
# ===================================================================


def test_push_then_diff_in_sync_for_multi_statement_sql(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The #686 headline: no phantom drift after pushing a multi-statement SQL."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;", "SELECT 3;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 3;", "SELECT 4;"), encoding="utf-8"
    )

    push_result = _service(store, api).push(alias="prod", project_root=project_root)
    assert push_result["status"] == "pushed"
    assert push_result["errors"] == []

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0
    assert diff_result["summary"]["modified"] == 0
    assert diff_result["summary"]["conflict"] == 0


def test_push_preserves_statement_count_with_semicolons(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Mirror test: push sends the same number of statements it pulled."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2;", "SELECT 22;"), encoding="utf-8"
    )
    _service(store, api).push(alias="prod", project_root=project_root)

    assert _remote_script(api) == ["SELECT 1;", "SELECT 22;"]


def test_push_preserves_statement_count_without_semicolons(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The silent-rewrite half: no trailing ``;`` must not collapse to one."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1", "SELECT 2"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2", "SELECT 22"), encoding="utf-8"
    )
    _service(store, api).push(alias="prod", project_root=project_root)

    assert _remote_script(api) == ["SELECT 1", "SELECT 22"]

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0


def test_push_of_disabled_config_without_local_key_leaves_no_drift(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """is_disabled instance of the same class (issue #467 semantics)."""
    project_root = tmp_path / "project"
    api = FakeApi(_http_components(is_disabled=True))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    # Drop the sparse is_disabled key, as a hand-written/legacy tree has it,
    # and change something so the config is pushable.
    config_file = _config_file(project_root)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data.pop("is_disabled", None)
    data["parameters"]["baseUrl"] = "https://changed.example.com"
    config_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

    _service(store, api).push(alias="prod", project_root=project_root)

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0
    assert diff_result["summary"]["conflict"] == 0


# ===================================================================
# Stamping mechanics (create / rows / partial responses)
# ===================================================================


def test_update_stamps_api_derived_hash_and_version(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A pushed config records the API's hash plus the shape version."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    before = _entry(project_root).metadata["pull_config_hash"]
    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2;", "SELECT 9;"), encoding="utf-8"
    )
    _service(store, api).push(alias="prod", project_root=project_root)

    entry = _entry(project_root)
    assert entry.metadata["pull_config_hash"] != before
    assert entry.metadata[CONFIG_HASH_VERSION_KEY] == CONFIG_HASH_VERSION


def test_create_stamps_api_derived_hash(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A freshly created config gets an API-derived baseline, not a disk one."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    new_dir = project_root / "main" / "transformation" / SQL_COMPONENT / "new-transformation"
    new_dir.mkdir(parents=True)
    (new_dir / CONFIG_FILENAME).write_text(
        yaml.dump({"version": 2, "name": "New", "description": "", "parameters": {}}),
        encoding="utf-8",
    )
    (new_dir / "transform.sql").write_text("SELECT 100;\n\nSELECT 200;\n", encoding="utf-8")

    result = _service(store, api).push(alias="prod", project_root=project_root)
    assert result["created"] == 1

    entry = _entry(project_root, "cfg-new")
    assert entry.metadata["pull_config_hash"]
    assert entry.metadata[CONFIG_HASH_VERSION_KEY] == CONFIG_HASH_VERSION
    assert (
        _service(store, api).diff(alias="prod", project_root=project_root)["summary"][
            "remote_modified"
        ]
        == 0
    )


def test_row_push_stamps_api_derived_hash(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A disabled remote row whose local file lacks the key leaves no drift."""
    project_root = tmp_path / "project"
    row = {
        "id": "row-001",
        "name": "Users",
        "description": "",
        "configuration": {"parameters": {"path": "/users"}},
        "isDisabled": True,
    }
    api = FakeApi(_http_components(rows=[row]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    row_file = _config_file(project_root, under_rows=True)
    data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
    data.pop("is_disabled", None)
    data["parameters"]["path"] = "/people"
    row_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

    _service(store, api).push(alias="prod", project_root=project_root)

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0
    assert diff_result["summary"]["conflict"] == 0


def test_partial_mutation_response_triggers_detail_fetch(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """An id-only PUT response is not trusted -- the config is re-read."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    api.partial_responses = True
    api.detail_calls = 0
    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2;", "SELECT 9;"), encoding="utf-8"
    )
    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert api.detail_calls >= 1
    assert result["errors"] == []
    assert not result.get("warnings")
    assert _entry(project_root).metadata[CONFIG_HASH_VERSION_KEY] == CONFIG_HASH_VERSION


def test_detail_fetch_failure_leaves_baseline_unstamped(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """No API state, no stamp: the old baseline survives and a warning is raised."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    before = _entry(project_root).metadata["pull_config_hash"]
    api.partial_responses = True
    api.detail_fails = True
    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2;", "SELECT 9;"), encoding="utf-8"
    )
    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert len(result["warnings"]) == 1
    assert "sync pull" in result["warnings"][0]["message"]
    # The baseline is left exactly as the pull wrote it -- never recomputed
    # from disk, which is the asymmetry #686 is about.
    assert _entry(project_root).metadata["pull_config_hash"] == before


# ===================================================================
# Migration: manifests written before the shape change
# ===================================================================


SQL_HEADER = "/* ===== BLOCK: Block 1 ===== */\n\n/* ===== CODE: Code 1 ===== */\n"


def _downgrade_to_legacy(
    project_root: Path, api: FakeApi, marker_less_body: str | None = None
) -> None:
    """Rewrite the tree the way a pre-#686 kbagent would have left it.

    The manifest entry loses its ``config_hash_version`` and carries the old
    collapsed-shape hash. When *marker_less_body* is given, ``transform.sql`` is
    rewritten without boundary markers (the pre-#686 rendering) and its recorded
    companion hash is refreshed, so the file still counts as untouched.
    """
    import hashlib

    from keboola_agent_cli.sync.config_format import api_config_to_local
    from keboola_agent_cli.sync.diff_engine import config_hash
    from keboola_agent_cli.sync.manifest import save_manifest

    sql_hash = ""
    if marker_less_body is not None:
        sql_file = _sql_file(project_root)
        sql_file.write_text(SQL_HEADER + marker_less_body, encoding="utf-8")
        sql_hash = hashlib.sha256(sql_file.read_bytes()).hexdigest()

    manifest = load_manifest(project_root)
    raw = api._find(SQL_COMPONENT, "cfg-sql")
    legacy = config_hash(api_config_to_local(SQL_COMPONENT, raw, "cfg-sql", legacy_scripts=True))
    for cfg in manifest.configurations:
        if cfg.id == "cfg-sql":
            cfg.metadata["pull_config_hash"] = legacy
            cfg.metadata.pop(CONFIG_HASH_VERSION_KEY, None)
            if sql_hash:
                cfg.metadata.setdefault("pull_extra_hashes", {})["transform.sql"] = sql_hash
    save_manifest(project_root, manifest)


def test_unversioned_legacy_hash_diffs_as_in_sync(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A pre-#686 baseline is accepted leniently instead of showing drift."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api)

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0
    assert diff_result["summary"]["conflict"] == 0


def test_versioned_entry_is_compared_strictly(tmp_config_dir: Path, tmp_path: Path) -> None:
    """With the version key present, a legacy-shaped hash is real drift."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api)

    from keboola_agent_cli.sync.manifest import save_manifest

    manifest = load_manifest(project_root)
    for cfg in manifest.configurations:
        cfg.metadata[CONFIG_HASH_VERSION_KEY] = CONFIG_HASH_VERSION
    save_manifest(project_root, manifest)

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 1


def test_real_remote_drift_is_still_reported_on_unversioned_entry(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Leniency covers the script shape only -- genuine remote edits still show."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api)

    api._find(SQL_COMPONENT, "cfg-sql")["name"] = "Renamed in the UI"

    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 1


def test_pull_migrates_unversioned_entry(tmp_config_dir: Path, tmp_path: Path) -> None:
    """One ``sync pull`` re-stamps the entry with the new shape + version."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1", "SELECT 2"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    # A tree pulled before boundary markers existed.
    _downgrade_to_legacy(project_root, api, "SELECT 1\n\nSELECT 2\n")

    _service(store, api).pull(
        alias="prod", project_root=project_root, no_storage=True, no_jobs=True
    )

    entry = _entry(project_root)
    assert entry.metadata[CONFIG_HASH_VERSION_KEY] == CONFIG_HASH_VERSION
    # Extraction re-ran, so the boundary markers are now on disk.
    assert "STATEMENT" in _sql_file(project_root).read_text(encoding="utf-8")


def test_pull_migration_preserves_locally_edited_code_file(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A migration pull must not clobber an edited transform.sql (R4)."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1", "SELECT 2"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api, "SELECT 1\n\nSELECT 2\n")

    sql_file = _sql_file(project_root)
    edited = SQL_HEADER + "SELECT 1\n\nSELECT 999\n"
    sql_file.write_text(edited, encoding="utf-8")

    _service(store, api).pull(
        alias="prod", project_root=project_root, no_storage=True, no_jobs=True
    )

    assert sql_file.read_text(encoding="utf-8") == edited
    assert CONFIG_HASH_VERSION_KEY not in _entry(project_root).metadata


def test_legacy_boundary_push_is_refused(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A pre-markers tree that would collapse statements is aborted, not pushed."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1", "SELECT 2"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    # Pre-markers rendering: the boundary between the two statements is lost.
    _downgrade_to_legacy(project_root, api, "SELECT 1\n\nSELECT 2\n")
    # Touch _config.yml so the config is classified as locally modified.
    config_file = _config_file(project_root)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["description"] = "edited"
    config_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error_code"] == "SYNC_LEGACY_BOUNDARY"
    assert "sync pull" in result["errors"][0]["message"]
    # The remote statement array is untouched.
    assert _remote_script(api) == ["SELECT 1", "SELECT 2"]


def test_genuine_edit_on_legacy_tree_still_pushes(tmp_config_dir: Path, tmp_path: Path) -> None:
    """The guard is boundary-specific: a real SQL edit is not blocked."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2;", "SELECT 42;"), encoding="utf-8"
    )
    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["updated"] == 1
    assert _remote_script(api) == ["SELECT 1;", "SELECT 42;"]


def test_force_pull_does_not_abort_on_legacy_shape(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A shape-only baseline is not a remote change, so --force must not abort."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _downgrade_to_legacy(project_root, api)

    # Local edit + a legacy baseline: strict comparison would read the remote
    # as "changed" and raise SyncConflictError.
    config_file = _config_file(project_root)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["description"] = "edited locally"
    config_file.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")

    _service(store, api).pull(
        alias="prod", project_root=project_root, force=True, no_storage=True, no_jobs=True
    )

    # The un-pushed edit survives (force preserves a locally-modified config
    # whose remote did not move).
    assert (
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["description"] == "edited locally"
    )
