"""A config's companion files are one set: pull, diff and push agree on it (group 2).

The world is the seeded random-walk world (``test_sync_random_walk``): an
ex-http extractor 1000 with a description and two rows, a SQL transformation
1006 without a description, a Python app 1009 with code and packages.

What is pinned here, per the group-2 design (D1-D6, D10, D11):

- the manifest records the companion SET (``pull_extra_hashes_version: 1``):
  a companion created after the pull is a local change, and a version-1 entry
  still reads clean under the comparison older kbagent versions make;
- pull removes a companion the remote no longer has, but only one it can
  prove unchanged -- an edited, unknown-baseline or racing file is kept and
  reported, never deleted;
- a file the component does not own is never hashed, compared or deleted;
- empty, deleted and inline companions have one defined meaning each.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.services import _sync_companions
from keboola_agent_cli.sync.code_extraction import (
    companions_of,
    merge_code_files,
    owned_companions,
    render_code_files,
)
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_random_walk import (
    APP,
    APP_1,
    HTTP,
    HTTP_1,
    PROD,
    SQL,
    SQL_1,
    _changes,
    _dir,
    _proj,
    _pull,
    _push,
    _world,
    read_yaml,
    write_yaml,
)

HTTP_DIR = "extractor/keboola.ex-http/name-tk6z"
SQL_DIR = "transformation/keboola.snowflake-transformation/name-tk16z"
APP_DIR = "application/kds-team.app-custom-python/name-tk25z"


def _entry(sim: Any, key: tuple[str, str]) -> Any:
    (entry,) = [
        c
        for c in load_manifest(sim.root).configurations
        if (c.component_id, c.id) == key and (c.branch_id or PROD) == PROD
    ]
    return entry


def _legacy_extras_modified(config_dir: Path, extra_hashes: dict[str, Any]) -> bool:
    """The companion check of every kbagent before the companion registry.

    Kept verbatim (``_sync_baseline.extras_modified`` up to 0.100.x): every
    recorded key must exist with its hash. A version-1 entry must read clean
    under it, or an older kbagent sharing the tree would see every config as
    locally modified and stop pulling it.
    """
    for fname, stored_hash in (extra_hashes or {}).items():
        fpath = config_dir / fname
        if not fpath.exists() or hashlib.sha256(fpath.read_bytes()).hexdigest() != stored_hash:
            return True
    return False


# ---------------------------------------------------------------------------
# D2 / D3: the baseline format and its compatibility
# ---------------------------------------------------------------------------


def test_version_1_entries_read_clean_under_the_legacy_comparison(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    for entry in load_manifest(sim.root).configurations:
        assert entry.metadata["pull_extra_hashes_version"] == 1
        recorded = entry.metadata["pull_extra_hashes"]
        assert None not in recorded.values(), "known absence is never written as null"
        assert not _legacy_extras_modified(sim.root / "main" / entry.path, recorded), entry.path


def test_new_companion_beside_a_legacy_entry_is_a_local_change(tmp_path: Path) -> None:
    """D3: a legacy map (no version) is a snapshot of the five scanned names."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        entry.metadata.pop("pull_extra_hashes_version")
    save_manifest(sim.root, manifest)
    assert _changes(sim) == []
    (_dir(sim, SQL_DIR) / "_description.md").write_text("Description tk900z", encoding="utf-8")
    assert ("modified", "1006") in _changes(sim)


def test_deleted_companion_is_a_local_change(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    (_dir(sim, HTTP_DIR) / "_description.md").unlink()
    assert ("modified", "1000") in _changes(sim)


# ---------------------------------------------------------------------------
# D4 / risks 1, 2, 4, 9, 11: pull reconciles the representation
# ---------------------------------------------------------------------------


def _poison(sim: Any, key: tuple[str, str], rel: str) -> None:
    """Re-create what a pull before the fix left: the stale file recorded as clean.

    The remote description is cleared, ``_config.yml`` is what that pull
    wrote (``description: ''``), the old ``_description.md`` stays, and the
    entry -- legacy, no version -- records it with its hash next to the
    cleared remote's config hash.
    """
    sim.api.remote[PROD][key]["description"] = ""
    config_dir = _dir(sim, rel)
    data = read_yaml(config_dir / "_config.yml")
    data = {"version": data["version"], "name": data["name"], "description": "", **data}
    write_yaml(config_dir / "_config.yml", data)
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if (entry.component_id, entry.id) != key:
            continue
        entry.metadata.pop("pull_extra_hashes_version")
        entry.metadata["pull_hash"] = sim.svc._file_hash(config_dir / "_config.yml")
        entry.metadata["pull_config_hash"] = config_hash(
            api_config_to_local(key[0], sim.api.remote[PROD][key], key[1])
        )
        entry.metadata["pull_extra_hashes"] = {
            "_description.md": sim.svc._file_hash(config_dir / "_description.md")
        }
    save_manifest(sim.root, manifest)


def test_pull_repairs_a_poisoned_tree_although_the_remote_is_unchanged(tmp_path: Path) -> None:
    """Risk 1: the stale file is recorded as clean and the remote hash matches."""
    sim = _world(tmp_path)
    _poison(sim, HTTP_1, HTTP_DIR)
    _pull(sim)
    assert not (_dir(sim, HTTP_DIR) / "_description.md").exists()
    sim._local_edit_slot(_dir(sim, HTTP_DIR), "value")  # an unrelated edit
    _push(sim)
    assert _proj(sim, PROD, HTTP_1)["desc"] == ()


def test_cleared_packages_with_an_edited_pyproject_keep_files_and_baseline(
    tmp_path: Path,
) -> None:
    """Risk 2: pull keeps the edited file AND the old baseline; push still conflicts."""
    sim = _world(tmp_path)
    sim._local_edit_slot(_dir(sim, APP_DIR), "pkg")
    edited = (_dir(sim, APP_DIR) / "pyproject.toml").read_text(encoding="utf-8")
    before = dict(_entry(sim, APP_1).metadata)
    sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] = []

    result = _pull(sim)

    assert (_dir(sim, APP_DIR) / "pyproject.toml").read_text(encoding="utf-8") == edited
    assert any(d.get("action") == "skipped" for d in result["details"])
    assert _entry(sim, APP_1).metadata == before
    sim._local_edit_slot(_dir(sim, APP_DIR), "code")  # a later, unrelated edit
    assert ("conflict", "1009") in _changes(sim)
    pushed = _push(sim)
    assert pushed.get("skipped")
    assert _proj(sim, PROD, APP_1)["pkg"] == ()


def test_missing_config_yml_with_an_edited_companion_is_not_overwritten(tmp_path: Path) -> None:
    """Risk 3 (D6): re-materializing _config.yml must not overwrite surviving edits."""
    sim = _world(tmp_path)
    config_dir = _dir(sim, SQL_DIR)
    sql = config_dir / "transform.sql"
    sql.write_text(sql.read_text(encoding="utf-8") + "SELECT 'tk900z';\n", encoding="utf-8")
    (config_dir / "_config.yml").unlink()
    blocks = [{"name": "B", "codes": [{"name": "C", "script": ["SELECT 'tk901z';"]}]}]
    sim.api.remote[PROD][SQL_1]["configuration"]["parameters"]["blocks"] = blocks

    result = _pull(sim)

    assert "tk900z" in sql.read_text(encoding="utf-8")
    assert {"action": "skipped", "path": SQL_DIR}.items() <= next(
        d for d in result["details"] if d.get("path") == SQL_DIR
    ).items()


@pytest.mark.parametrize("bookkeeping", ["no_pull_hash", "no_map"])
def test_unknown_companion_baseline_never_authorizes_a_delete(
    tmp_path: Path, bookkeeping: str
) -> None:
    """Risk 4: a checkpoint / adoption baseline proves nothing about a file."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if (entry.component_id, entry.id) == HTTP_1:
            entry.metadata.pop("pull_extra_hashes_version")
            if bookkeeping == "no_pull_hash":
                entry.metadata["pull_hash"] = ""
                entry.metadata["pull_extra_hashes"] = {}
            else:
                entry.metadata.pop("pull_extra_hashes")
    save_manifest(sim.root, manifest)
    sim.api.remote[PROD][HTTP_1]["description"] = ""

    result = _pull(sim)

    kept = _dir(sim, HTTP_DIR) / "_description.md"
    assert kept.exists()
    [warning] = result["companion_warnings"]
    assert (warning["code"], warning["path"]) == ("COMPANION_KEPT", f"{HTTP_DIR}/_description.md")
    _kept_file_is_a_conflict_until_it_is_gone(sim, kept)


def _kept_file_is_a_conflict_until_it_is_gone(sim: Any, kept: Path) -> None:
    """R4: a kept obsolete companion never reaches the remote; deleting it resolves it."""
    assert ("conflict", "1000") in _changes(sim)
    sim._local_edit_slot(_dir(sim, HTTP_DIR), "value")  # a later, unrelated edit
    pushed = _push(sim)
    assert pushed.get("skipped")
    assert _proj(sim, PROD, HTTP_1)["desc"] == ()
    kept.unlink()  # accept the remote's empty description
    assert ("modified", "1000") in _changes(sim)
    _push(sim)
    value = read_yaml(_dir(sim, HTTP_DIR) / "_config.yml")["parameters"]["value"]
    assert _proj(sim, PROD, HTTP_1)["desc"] == ()
    assert _proj(sim, PROD, HTTP_1)["value"] == (value,)
    assert _changes(sim) == []


def test_obsolete_companion_edited_during_pull_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk 11: the file is hashed again right before the delete."""
    sim = _world(tmp_path)
    sim.api.remote[PROD][HTTP_1]["description"] = ""
    stale = _dir(sim, HTTP_DIR) / "_description.md"
    render = _sync_companions.render_code_files

    def render_while_the_user_saves(component_id: str, data: dict[str, Any]) -> dict[str, str]:
        # Only while pull writes config 1000, i.e. after it planned the cleanup.
        if (data.get("_keboola") or {}).get("config_id") == HTTP_1[1]:
            stale.write_text("Saved meanwhile tk900z", encoding="utf-8")
        return render(component_id, data)

    monkeypatch.setattr(_sync_companions, "render_code_files", render_while_the_user_saves)
    result = _pull(sim)

    assert stale.read_text(encoding="utf-8") == "Saved meanwhile tk900z"
    assert [w["code"] for w in result["companion_warnings"]] == ["COMPANION_KEPT"]
    monkeypatch.undo()
    _kept_file_is_a_conflict_until_it_is_gone(sim, stale)


def test_unowned_file_is_never_hashed_compared_or_deleted(tmp_path: Path) -> None:
    """Risk 9 (D1): a user's pyproject.toml beside an extractor."""
    sim = _world(tmp_path)
    user_file = _dir(sim, HTTP_DIR) / "pyproject.toml"
    user_file.write_text('[project]\nname = "mine"\n', encoding="utf-8")
    sim.api.remote[PROD][HTTP_1]["description"] = ""

    _pull(sim)

    assert user_file.read_text(encoding="utf-8") == '[project]\nname = "mine"\n'
    assert "pyproject.toml" not in _entry(sim, HTTP_1).metadata["pull_extra_hashes"]
    user_file.write_text('[project]\nname = "edited"\n', encoding="utf-8")
    assert _changes(sim) == []


def test_theirs_removes_a_locally_added_companion(tmp_path: Path) -> None:
    """D5: remote wins, also over the companion set."""
    sim = _world(tmp_path)
    added = _dir(sim, SQL_DIR) / "_description.md"
    added.write_text("Description tk900z", encoding="utf-8")
    _pull(sim, theirs=True)
    assert not added.exists()
    assert _changes(sim) == []


# ---------------------------------------------------------------------------
# Risks 12 / 13: empty, deleted and inline companions
# ---------------------------------------------------------------------------


def _merged(
    tmp_path: Path, component_id: str, inline: dict[str, Any], files: dict[str, str]
) -> dict[str, Any]:
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return merge_code_files(component_id, dict(inline), tmp_path)


@pytest.mark.parametrize(
    ("inline", "files", "expected"),
    [
        ({}, {"_description.md": ""}, ""),  # empty file: empty description
        ({}, {}, ""),  # deleted file, no inline value: empty description
        ({"description": ""}, {}, ""),  # explicit empty inline field
        ({"description": "inline"}, {"_description.md": "file"}, "file"),  # file wins
        ({"description": "inline"}, {}, "inline"),  # no file: the inline value
    ],
)
def test_description_payload(
    tmp_path: Path, inline: dict[str, Any], files: dict[str, str], expected: str
) -> None:
    assert _merged(tmp_path, HTTP, inline, files)["description"] == expected


@pytest.mark.parametrize(
    ("inline", "files", "expected"),
    [
        # S1: only a dependency list the file states replaces the packages.
        # No [project].dependencies key: the file says nothing -- the inline
        # list applies (Astra's input), never an empty list.
        ({}, {"pyproject.toml": "[project]\nname = 'x'\n"}, None),
        ({"packages": ["pandas"]}, {"pyproject.toml": '[project]\nname = "app"\n'}, ["pandas"]),
        # A one-line list is TOML too (Astra's second input).
        ({}, {"pyproject.toml": '[project]\ndependencies = ["pandas"]\n'}, ["pandas"]),
        # An explicit empty list IS an empty package list.
        ({"packages": ["inline"]}, {"pyproject.toml": "[project]\ndependencies = []\n"}, []),
        # Not valid TOML: the inline value stays.
        ({"packages": ["pandas"]}, {"pyproject.toml": "[project\ndependencies = [\n"}, ["pandas"]),
        ({}, {}, None),  # deleted: no packages sent
        ({"packages": []}, {}, []),  # explicit empty inline list (what pull writes)
        (
            {"packages": ["inline"]},
            {"pyproject.toml": '[project]\ndependencies = [\n    "file",\n]\n'},
            ["file"],
        ),
    ],
)
def test_packages_payload(
    tmp_path: Path, inline: dict[str, Any], files: dict[str, str], expected: Any
) -> None:
    merged = _merged(tmp_path, APP, {"parameters": dict(inline)}, files)
    assert merged["parameters"].get("packages") == expected


@pytest.mark.parametrize(
    ("inline", "files", "expected"),
    [
        ({}, {"code.py": ""}, ""),  # empty file: empty code
        ({}, {}, None),  # deleted, no inline: no code sent
        ({"code": ""}, {}, ""),  # explicit empty inline field
        ({"code": "inline"}, {"code.py": "file"}, "file"),  # file wins
    ],
)
def test_code_payload(
    tmp_path: Path, inline: dict[str, Any], files: dict[str, str], expected: Any
) -> None:
    merged = _merged(tmp_path, APP, {"parameters": dict(inline)}, files)
    assert merged["parameters"].get("code") == expected


@pytest.mark.parametrize(
    ("component_id", "filename"),
    [(SQL, "transform.sql"), ("keboola.python-transformation-v2", "transform.py")],
)
@pytest.mark.parametrize(
    ("inline", "file", "expected"),
    [
        ({}, "", []),  # empty file: no blocks
        ({}, None, None),  # deleted, no inline: no blocks sent
        ({"blocks": []}, None, []),  # explicit empty inline field
        ({"blocks": [{"name": "inline", "codes": []}]}, "", []),  # file wins, also empty
    ],
)
def test_transformation_code_payload(
    tmp_path: Path,
    component_id: str,
    filename: str,
    inline: dict[str, Any],
    file: str | None,
    expected: Any,
) -> None:
    """R10: the same matrix for the SQL and Python transformation code files."""
    files = {} if file is None else {filename: file}
    merged = _merged(tmp_path, component_id, {"parameters": dict(inline)}, files)
    assert merged["parameters"].get("blocks") == expected


def test_registry_drives_extraction_and_merge(tmp_path: Path) -> None:
    """R8 (D1): every registry entry renders its file and merges it back into its field."""
    data = {
        "description": "d",
        "parameters": {"code": "print(1)\n", "packages": ["a"]},
        "_keboola": {"component_id": APP, "config_id": "1"},
    }
    files = render_code_files(APP, data)
    assert set(files) == set(owned_companions(APP))
    assert [c.filename for c in companions_of(APP)] == list(owned_companions(APP))
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    merged = merge_code_files(APP, data, tmp_path)
    assert (merged["description"], merged["parameters"]) == (
        "d",
        {"code": "print(1)\n", "packages": ["a"]},
    )


def test_deleting_a_pulled_description_does_not_resurrect_anything(tmp_path: Path) -> None:
    """Risk 13: pull never writes a value both inline and in the file."""
    sim = _world(tmp_path)
    config_dir = _dir(sim, HTTP_DIR)
    assert "description" not in read_yaml(config_dir / "_config.yml")
    (config_dir / "_description.md").unlink()
    _push(sim)
    assert _proj(sim, PROD, HTTP_1)["desc"] == ()
    # L2: the written file has the layout a fresh pull writes.
    assert read_yaml(config_dir / "_config.yml")["description"] == ""
    assert list(read_yaml(config_dir / "_config.yml"))[:3] == ["version", "name", "description"]


def test_deleting_a_pulled_pyproject_clears_the_packages(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    (_dir(sim, APP_DIR) / "pyproject.toml").unlink()
    assert ("modified", "1009") in _changes(sim)
    _push(sim)
    assert _proj(sim, PROD, APP_1)["pkg"] == ()


# ---------------------------------------------------------------------------
# D11: diagnostics
# ---------------------------------------------------------------------------


def test_companion_of_another_component_is_reported(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    (_dir(sim, HTTP_DIR) / "code.py").write_text("print('tk900z')\n", encoding="utf-8")
    result = sim.svc.diff(alias="prod", project_root=sim.root)
    [warning] = result["companion_warnings"]
    assert (warning["code"], warning["path"]) == ("UNOWNED_COMPANION", f"{HTTP_DIR}/code.py")
    assert result["changes"] == []


def test_component_id_change_is_refused(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    config_file = _dir(sim, HTTP_DIR) / "_config.yml"
    data = read_yaml(config_file)
    data["_keboola"]["component_id"] = SQL
    config_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot change its component"):
        sim.svc.diff(alias="prod", project_root=sim.root)
    with pytest.raises(ConfigError, match="cannot change its component"):
        _push(sim)
    assert _proj(sim, PROD, HTTP_1)["name"] == "Name tk6z"


# ---------------------------------------------------------------------------
# R12 / R13 / R7
# ---------------------------------------------------------------------------


def test_remote_delete_keeps_an_edited_companion_whose_config_yml_is_gone(tmp_path: Path) -> None:
    """R12: the stale sweep applies the D6 rule before deleting the directory."""
    sim = _world(tmp_path)
    config_dir = _dir(sim, SQL_DIR)
    sql = config_dir / "transform.sql"
    sql.write_text(sql.read_text(encoding="utf-8") + "SELECT 'tk900z';\n", encoding="utf-8")
    (config_dir / "_config.yml").unlink()
    del sim.api.remote[PROD][SQL_1]

    _pull(sim)

    assert "tk900z" in sql.read_text(encoding="utf-8")


def test_old_writer_over_a_version_1_entry_reads_clean_and_sees_a_new_file(
    tmp_path: Path,
) -> None:
    """R13: an older kbagent's push keeps the marker and rewrites the map its way."""
    sim = _world(tmp_path)
    config_dir = _dir(sim, SQL_DIR)
    (config_dir / "code.py").write_text("print('mine')\n", encoding="utf-8")  # not owned
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if (entry.component_id, entry.id) == SQL_1:
            # origin/main's stamp_updated_config: pull_hash and the map from the
            # five names found on disk, every other key -- the marker -- kept.
            entry.metadata["pull_hash"] = sim.svc._file_hash(config_dir / "_config.yml")
            entry.metadata["pull_extra_hashes"] = {
                name: sim.svc._file_hash(config_dir / name)
                for name in sorted(_sync_companions.LEGACY_COMPANIONS)
                if (config_dir / name).exists()
            }
            assert entry.metadata["pull_extra_hashes_version"] == 1
    save_manifest(sim.root, manifest)

    assert _changes(sim) == []
    (config_dir / "_description.md").write_text("Description tk900z", encoding="utf-8")
    assert ("modified", "1006") in _changes(sim)


def test_companion_warnings_reach_push_results(tmp_path: Path) -> None:
    """R7: also when push has nothing to push, and on a dry run."""
    sim = _world(tmp_path)
    (_dir(sim, HTTP_DIR) / "code.py").write_text("print('tk900z')\n", encoding="utf-8")
    for result in (
        _push(sim),
        sim.svc.push(alias="prod", project_root=sim.root, dry_run=True),
    ):
        assert result["status"] == "no_changes"
        [warning] = result["companion_warnings"]
        assert warning["code"] == "UNOWNED_COMPANION"
    sim._local_edit_slot(_dir(sim, HTTP_DIR), "value")
    for result in (sim.svc.push(alias="prod", project_root=sim.root, dry_run=True), _push(sim)):
        assert [w["code"] for w in result["companion_warnings"]] == ["UNOWNED_COMPANION"]


def _cli_diff(tmp_path: Path, diff_result: dict[str, Any], *flags: str) -> Any:
    from typer.testing import CliRunner

    from keboola_agent_cli.cli import app
    from keboola_agent_cli.services.project_service import ProjectService
    from test_sync_cli import TEST_TOKEN, _make_sync_service_mock, _setup_config

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
    mock_sync = _make_sync_service_mock()
    mock_sync.diff.return_value = diff_result
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as store_cls,
        patch("keboola_agent_cli.cli.ProjectService") as project_cls,
        patch("keboola_agent_cli.cli.SyncService") as sync_cls,
    ):
        store_cls.return_value = store
        project_cls.return_value = ProjectService(config_store=store)
        sync_cls.return_value = mock_sync
        return CliRunner().invoke(
            app,
            [*flags, "sync", "diff", "--project", "prod", "--directory", str(tmp_path)],
        )


_IN_SYNC_WITH_A_WARNING: dict[str, Any] = {
    "changes": [],
    "summary": {"added": 0, "modified": 0, "deleted": 0, "unchanged": 1},
    "companion_warnings": [
        {
            "code": "UNOWNED_COMPANION",
            "component_id": HTTP,
            "path": f"{HTTP_DIR}/code.py",
            "message": f"{HTTP_DIR}/code.py is not part of a {HTTP} configuration",
        }
    ],
}


def test_human_diff_prints_the_warning_also_when_in_sync(tmp_path: Path) -> None:
    result = _cli_diff(tmp_path, copy.deepcopy(_IN_SYNC_WITH_A_WARNING))
    assert result.exit_code == 0, result.output
    assert "No differences found" in result.stdout
    assert f"{HTTP_DIR}/code.py is not part of" in result.stderr


def test_json_diff_carries_the_warning_in_the_envelope(tmp_path: Path) -> None:
    result = _cli_diff(tmp_path, copy.deepcopy(_IN_SYNC_WITH_A_WARNING), "--json")
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    [warning] = [w for w in envelope["warnings"] if w["code"] == "UNOWNED_COMPANION"]
    assert warning["path"] == f"{HTTP_DIR}/code.py"
    assert "companion_warnings" not in envelope["data"]


# ---------------------------------------------------------------------------
# S1: a pyproject.toml that states no dependencies never clears the packages
# ---------------------------------------------------------------------------


def test_pyproject_without_dependencies_never_clears_the_remote_packages(tmp_path: Path) -> None:
    """S1 (Astra's P1): inline ``packages`` + a pyproject.toml with no dependencies key."""
    sim = _world(tmp_path)
    sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] = ["pandas"]
    _pull(sim)
    config_dir = _dir(sim, APP_DIR)
    (config_dir / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    data = read_yaml(config_dir / "_config.yml")
    data["parameters"]["packages"] = ["pandas"]
    data["name"] = "Name tk900z"  # the unrelated edit that makes the config pushable
    write_yaml(config_dir / "_config.yml", data)

    _push(sim)

    remote = sim.api.remote[PROD][APP_1]
    assert remote["name"] == "Name tk900z"
    assert remote["configuration"]["parameters"]["packages"] == ["pandas"]


def test_unreadable_pyproject_is_reported_and_keeps_the_inline_packages(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    config_dir = _dir(sim, APP_DIR)
    (config_dir / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    data = read_yaml(config_dir / "_config.yml")
    data["parameters"]["packages"] = ["pkgtk24z"]
    write_yaml(config_dir / "_config.yml", data)

    result = sim.svc.diff(alias="prod", project_root=sim.root)

    assert [w["code"] for w in result["companion_warnings"]] == ["COMPANION_UNREADABLE"]
    _push(sim)
    assert sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] == ["pkgtk24z"]


def test_pyproject_render_is_valid_toml_for_a_quoted_marker(tmp_path: Path) -> None:
    """S1: a package spec with quotes made the written file invalid TOML."""
    spec = 'pandas; python_version < "3.12"'
    data = {"name": "App", "parameters": {"packages": [spec]}, "_keboola": {}}
    files = render_code_files(APP, data)
    (tmp_path / "pyproject.toml").write_text(files["pyproject.toml"], encoding="utf-8")
    assert merge_code_files(APP, {"parameters": {}}, tmp_path)["parameters"]["packages"] == [spec]


def test_all_projects_results_emit_each_projects_companion_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """S4: ``--all-projects`` diff/push/pull results name the project of each entry."""
    from keboola_agent_cli.commands._sync_push_render import emit_companion_warnings
    from keboola_agent_cli.output import OutputFormatter

    def bulk() -> dict[str, Any]:
        return {
            "projects": {"prod": copy.deepcopy(_IN_SYNC_WITH_A_WARNING), "dev": {"error": "x"}},
            "summary": {"total": 2},
        }

    formatter = OutputFormatter(json_mode=True, no_color=True)
    data = bulk()
    emit_companion_warnings(formatter, data)
    [warning] = formatter.warnings
    assert (warning["code"], warning["project"]) == ("UNOWNED_COMPANION", "prod")
    assert "companion_warnings" not in data["projects"]["prod"]

    emit_companion_warnings(OutputFormatter(json_mode=False, no_color=True), bulk())
    assert f"prod: {HTTP_DIR}/code.py is not part of" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# T1 / T2: a pyproject.toml that states no package list never decides them
# ---------------------------------------------------------------------------

_SILENT_PYPROJECTS = {
    "no-dependencies-key": '[project]\nname = "app"\n',  # Astra's input
    "invalid-toml": "[project\n",
    "poetry-only": '[tool.poetry.dependencies]\npython = "^3.11"\npandas = "*"\n',
}


def _rename_app(sim: Any, name: str) -> None:
    config_file = _dir(sim, APP_DIR) / "_config.yml"
    data = read_yaml(config_file)
    data["name"] = name
    write_yaml(config_file, data)


@pytest.mark.parametrize("pyproject", list(_SILENT_PYPROJECTS), ids=list(_SILENT_PYPROJECTS))
def test_silent_pyproject_keeps_the_remote_packages(tmp_path: Path, pyproject: str) -> None:
    """T1: remote ["pandas"], no inline packages, a pyproject.toml without a usable list."""
    sim = _world(tmp_path)
    sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] = ["pandas"]
    _pull(sim)
    assert "packages" not in read_yaml(_dir(sim, APP_DIR) / "_config.yml")["parameters"]
    (_dir(sim, APP_DIR) / "pyproject.toml").write_text(
        _SILENT_PYPROJECTS[pyproject], encoding="utf-8"
    )
    _rename_app(sim, "Name tk900z")

    diff = sim.svc.diff(alias="prod", project_root=sim.root)
    [change] = [c for c in diff["changes"] if c["config_id"] == "1009"]
    assert not any("packages" in line for line in change["details"])
    result = _push(sim)

    assert result["errors"] == []
    remote = sim.api.remote[PROD][APP_1]
    assert remote["name"] == "Name tk900z"
    assert remote["configuration"]["parameters"]["packages"] == ["pandas"]


def test_new_config_with_a_silent_pyproject_and_no_inline_packages_is_refused(
    tmp_path: Path,
) -> None:
    """T1: no inline value and no remote to take the packages from -- fail closed."""
    sim = _world(tmp_path)
    src = _dir(sim, APP_DIR)
    dst = src.parent / "new-app"
    shutil.copytree(src, dst)
    data = read_yaml(dst / "_config.yml")
    data["_keboola"].pop("config_id")
    data["name"] = "Name tk901z"
    write_yaml(dst / "_config.yml", data)
    (dst / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    result = _push(sim)

    [error] = result["errors"]
    assert error["error_code"] == "COMPANION_UNREADABLE"
    assert "pyproject.toml" in error["message"]
    assert not [c for c in sim.api.remote[PROD].values() if c["name"] == "Name tk901z"]


@pytest.mark.parametrize(
    ("remote_packages", "pyproject"),
    [
        # Astra's legacy file: the old writer did not escape the marker's quotes.
        (
            ['pandas; python_version < "3.12"'],
            (
                "[project]\n"
                'name = "name-tk25z"\n'
                'requires-python = ">=3.11"\n'
                "dependencies = [\n"
                '    "pandas; python_version < "3.12"",\n'
                "]\n"
            ),
        ),
        # A tool section's dependencies are never read, also not after a syntax error.
        (["pkgtk24z"], '[tool.other]\ndependencies = [\n    "numpy",\n]\n[broken\n'),
    ],
    ids=["legacy-quoted-marker", "tool-section-and-broken-toml"],
)
def test_invalid_pyproject_leaves_the_remote_packages_byte_for_byte(
    tmp_path: Path, remote_packages: list[str], pyproject: str
) -> None:
    """T2: no line-reader fallback; invalid TOML takes the T1 path."""
    sim = _world(tmp_path)
    sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] = remote_packages
    _pull(sim)
    (_dir(sim, APP_DIR) / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    _rename_app(sim, "Name tk900z")

    result = _push(sim)

    assert result["errors"] == []
    packages = sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"]
    assert packages == remote_packages


# ---------------------------------------------------------------------------
# Round 5: adopted comparisons settle packages; dry runs report the refusal
# ---------------------------------------------------------------------------


def _silent_app_pyproject_without_its_entry(sim: Any) -> None:
    sim.api.remote[PROD][APP_1]["configuration"]["parameters"]["packages"] = ["pandas"]
    _pull(sim)
    (_dir(sim, APP_DIR) / "pyproject.toml").write_text(
        '[project]\nname = "app"\n', encoding="utf-8"
    )
    manifest = load_manifest(sim.root)
    manifest.configurations = [
        c for c in manifest.configurations if (c.component_id, c.id) != APP_1
    ]
    save_manifest(sim.root, manifest)


def test_adopted_diff_settles_undecided_packages_from_the_remote(tmp_path: Path) -> None:
    """Round 5 P2: no false conflict 'parameters.packages removed' on an adopted dir."""
    sim = _world(tmp_path)
    _silent_app_pyproject_without_its_entry(sim)
    assert _changes(sim) == []


def test_adopted_pull_comparison_settles_undecided_packages(tmp_path: Path) -> None:
    """Round 5 P2: pull does not keep the adopted dir as 'locally modified'."""
    sim = _world(tmp_path)
    _silent_app_pyproject_without_its_entry(sim)
    result = _pull(sim)
    assert not [d for d in result["details"] if d.get("action") == "skipped"]


def test_push_dry_run_reports_the_create_a_real_push_refuses(tmp_path: Path) -> None:
    """Round 5 P2: the dry run runs the same COMPANION_UNREADABLE check."""
    sim = _world(tmp_path)
    src = _dir(sim, APP_DIR)
    dst = src.parent / "new-app"
    shutil.copytree(src, dst)
    data = read_yaml(dst / "_config.yml")
    data["_keboola"].pop("config_id")
    data["name"] = "Name tk901z"
    write_yaml(dst / "_config.yml", data)
    (dst / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")

    dry = sim.svc.push(alias="prod", project_root=sim.root, dry_run=True)
    real = _push(sim)

    assert [e["error_code"] for e in dry["errors"]] == ["COMPANION_UNREADABLE"]
    assert [e["error_code"] for e in real["errors"]] == ["COMPANION_UNREADABLE"]


def test_clone_dry_run_reports_the_create_a_real_clone_refuses(tmp_path: Path) -> None:
    from test_sync_bookkeeping_differential import VARIANTS, Api, World

    variant = VARIANTS["app"]
    src = World(tmp_path, name="source")
    cid = src.api.seed(variant)
    src.init()
    src.pull()
    (src.config_dir(cid) / "pyproject.toml").write_text(
        '[project]\nname = "app"\n', encoding="utf-8"
    )
    target_api = Api(first_id=5000)
    target_api.types = dict(src.api.types)
    target = World(tmp_path, target_api, name="target")
    target.root.rmdir()

    result = target.svc.clone_project(src.root, "prod", target.root, dry_run=True)

    assert [e["error_code"] for e in result["errors"]] == ["COMPANION_UNREADABLE"]
    assert target_api.remote[PROD] == {}


# ---------------------------------------------------------------------------
# Round 6 P2: human dry-run output prints each refusal and counts separately
# ---------------------------------------------------------------------------

_REFUSAL = {
    "change_type": "added",
    "component_id": APP,
    "config_id": "",
    "message": "Refusing to push: new-app/pyproject.toml states no usable list. Fix pyproject.toml.",
    "error_code": "COMPANION_UNREADABLE",
}


def test_push_dry_run_renderer_prints_refusals_and_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keboola_agent_cli.commands._sync_push_render import push_summary, push_total
    from keboola_agent_cli.commands.sync import _render_push_result
    from keboola_agent_cli.output import OutputFormatter

    result = {
        "status": "dry_run",
        "changes": [
            {"change_type": "added", "component_id": APP, "config_id": "", "path": "new-app"},
            {"change_type": "added", "component_id": HTTP, "config_id": "", "path": "new-x"},
        ],
        "summary": {"added": 2, "modified": 0, "deleted": 0},
        "errors": [_REFUSAL],
    }
    _render_push_result(OutputFormatter(json_mode=False, no_color=True), result)
    out = capsys.readouterr().out
    assert "Fix pyproject.toml." in out
    assert push_total(result) == 2
    assert push_summary(result) == (
        "2 config(s) planned, 1 refused, 1 would be pushed: 1 created, 0 updated, 0 deleted."
    )


def test_clone_dry_run_renderer_prints_refusals_and_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keboola_agent_cli.commands._sync_clone_render import (
        clone_summary,
        clone_total,
        print_clone_result,
    )
    from keboola_agent_cli.output import OutputFormatter

    result = {
        "status": "dry_run",
        "target_alias": "prod",
        "summary": {"added": 2},
        "errors": [_REFUSAL],
        "warnings": [],
    }
    print_clone_result(OutputFormatter(json_mode=False, no_color=True), result)
    assert "Fix pyproject.toml." in capsys.readouterr().out
    assert clone_total(result, failed=1) == 2
    assert clone_summary(result) == "2 config(s) planned, 1 refused, 1 would be cloned into prod."


def test_all_projects_dry_run_lines_count_only_executable_creates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round 7 P2: a refused create is never 'Would create 1' / 'would: +1'."""
    from keboola_agent_cli.commands._sync_push_render import push_summary
    from keboola_agent_cli.commands.sync import _format_push_result, _push_one_liner
    from keboola_agent_cli.output import OutputFormatter

    result = {
        "status": "dry_run",
        "changes": [{"change_type": "added", "component_id": APP, "path": "new-app"}],
        "summary": {"added": 1, "modified": 0, "deleted": 0},
        "errors": [_REFUSAL],
    }
    _format_push_result(OutputFormatter(json_mode=False, no_color=True), result)
    out = capsys.readouterr().out
    assert "Would create 0, update 0, delete 0, 1 refused" in out
    assert "Fix pyproject.toml." in out
    assert _push_one_liner(result).startswith("would: +0 ~0 -0, [red]1 refused[/red]")
    assert push_summary(result) == (
        "1 config(s) planned, 1 refused, 0 would be pushed: 0 created, 0 updated, 0 deleted."
    )
