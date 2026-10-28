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

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.services import _sync_companions
from keboola_agent_cli.sync.code_extraction import merge_code_files
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

    assert (_dir(sim, HTTP_DIR) / "_description.md").exists()
    [warning] = [w for w in result.get("warnings", []) if w["change_type"] == "companion_kept"]
    assert warning["path"] == f"{HTTP_DIR}/_description.md"
    # Kept, it is a visible local change -- never a silently clean file.
    assert ("modified", "1000") in _changes(sim)


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
    assert any(w["change_type"] == "companion_kept" for w in result.get("warnings", []))


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
        # A pyproject.toml with no dependencies names no packages: the
        # inline list (if any) applies, else the key is absent.
        ({}, {"pyproject.toml": "[project]\nname = 'x'\n"}, None),
        ({"packages": []}, {"pyproject.toml": "[project]\nname = 'x'\n"}, []),
        ({}, {}, None),  # deleted: no packages sent
        ({"packages": []}, {}, []),  # explicit empty inline list (what pull writes)
        (
            {"packages": ["inline"]},
            {"pyproject.toml": 'dependencies = [\n    "file",\n]\n'},
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
    [warning] = result["warnings"]
    assert (warning["change_type"], warning["path"]) == ("unowned_companion", f"{HTTP_DIR}/code.py")
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
