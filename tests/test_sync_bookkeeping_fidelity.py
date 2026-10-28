"""Bookkeeping fidelity: re-enable by deleting the line, pyproject identity, row stamps.

Three findings of the bookkeeping differential and the random walk:

- Re-enabling a pulled disabled config or row by deleting the
  ``is_disabled: true`` line pull wrote reported ``updated``, sent nothing and
  left diff clean, so it silently stayed disabled. The manifest now records
  which accepted baseline file held the line (``pulled_disabled``); a missing
  key then re-enables, a legacy file without the record keeps the old
  "leave the remote alone" meaning.
- ``pyproject.toml`` carries config-derived fields (``[project] name``,
  ``[tool.keboola] config_id``) that push and clone never refreshed.
- Push stamped ``config_hash_version`` on ROW entries; pull never does and
  nothing reads it there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from keboola_agent_cli.constants import CONFIG_HASH_VERSION_KEY
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services._sync_disabled import DISABLED_BASELINE_KEY, baseline_disabled
from keboola_agent_cli.sync.config_format import dump_config_yaml
from keboola_agent_cli.sync.manifest import ManifestConfiguration, load_manifest, save_manifest
from test_sync_bookkeeping_differential import (
    CONFIG_FILE,
    DEV,
    PROD,
    VARIANTS,
    Variant,
    World,
    _rows,
    author_config,
    author_row,
    dirty,
    semantic_edit,
)

PYPROJECT = "pyproject.toml"


# ===========================================================================
# Helpers
# ===========================================================================


def _pulled(
    tmp: Path, v: Variant, *, disabled: bool = False, row_disabled: bool = False
) -> tuple[World, str]:
    """A World with one seeded config (optionally disabled) pulled into main/."""
    w = World(tmp)
    cid = w.api.seed(v)
    cfg = w.api.get(PROD, v.component_id, cid)
    cfg["isDisabled"] = disabled
    if row_disabled:
        cfg["rows"][0]["isDisabled"] = True
    w.init()
    w.pull()
    return w, cid


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(dump_config_yaml(data), encoding="utf-8", newline="")


def _drop_flag(path: Path) -> None:
    data = _read(path)
    assert data.pop("is_disabled") is True, f"pull wrote no is_disabled: true into {path}"
    _write(path, data)


def _entry(w: World, component_id: str, config_id: str, branch: int | None = None):
    (entry,) = [
        c
        for c in load_manifest(w.root).configurations
        if c.component_id == component_id
        and c.id == config_id
        and (c.branch_id or PROD) == (branch or PROD)
    ]
    return entry


def _disabled_row_file(w: World, config_id: str) -> Path:
    (row_file,) = [
        f for f in w.config_dir(config_id).glob("rows/*/_config.yml") if "is_disabled" in _read(f)
    ]
    return row_file


def _remote_row(w: World, v: Variant, config_id: str, row_file: Path) -> dict[str, Any]:
    row_id = str(_read(row_file)["_keboola"]["row_id"])
    (row,) = [r for r in w.api.get(PROD, v.component_id, config_id)["rows"] if r["id"] == row_id]
    return row


def _rename(config_file: Path, name: str) -> None:
    data = _read(config_file)
    data["name"] = name
    _write(config_file, data)


def _boom(*_: Any, **__: Any) -> dict:
    raise KeboolaApiError("Storage refused the write", status_code=400)


# ===========================================================================
# A. Re-enable by deleting the line pull wrote
# ===========================================================================


def test_r1_removed_flag_survives_a_pull_and_the_push_reenables(tmp_path: Path) -> None:
    """R1: pull disabled -> delete the line -> plain pull keeps it -> push re-enables."""
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v, disabled=True)
    cfg = w.config_dir(cid) / CONFIG_FILE
    _drop_flag(cfg)

    result = w.pull()
    assert [d["reason"] for d in result["details"] if d["action"] == "skipped"] == [
        "locally modified"
    ]
    assert "is_disabled" not in _read(cfg), "pull restored the deleted line"
    assert w.diff()["summary"]["modified"] == 1

    w.push()
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is False
    assert dirty(w.diff()["summary"]) == {}


def test_r2_removed_row_flag_survives_a_pull_and_the_push_reenables(tmp_path: Path) -> None:
    """R2: the same for a row (pull rebuilds row entries, so the record must travel)."""
    v = VARIANTS["rows"]
    w, cid = _pulled(tmp_path, v, row_disabled=True)
    row_file = _disabled_row_file(w, cid)
    _drop_flag(row_file)

    w.pull()
    assert "is_disabled" not in _read(row_file), "pull restored the deleted line"

    w.push()
    assert _remote_row(w, v, cid, row_file)["isDisabled"] is False
    assert dirty(w.diff()["summary"]) == {}


def _make_legacy(w: World, component_id: str, config_id: str, file: Path) -> None:
    """Drop the line and record the file as the baseline, as an older push stamped it.

    Older kbagent pushed a file without the line next to a remote disabled in
    the UI and recorded that file, the remote's (disabled) state and no marker.
    """
    data = _read(file)
    data.pop("is_disabled")
    _write(file, data)
    manifest = load_manifest(w.root)
    for entry in manifest.configurations:
        if entry.component_id != component_id or entry.id != config_id:
            continue
        targets = [entry.metadata] if file.parent.parent.name != "rows" else []
        targets += [
            r.metadata for r in entry.rows if (w.config_dir(config_id) / r.path) == file.parent
        ]
        for metadata in targets:
            metadata["pull_hash"] = w.svc._file_hash(file)
            metadata.pop(DISABLED_BASELINE_KEY, None)
    save_manifest(w.root, manifest)


def test_r3_legacy_file_without_record_never_reenables(tmp_path: Path) -> None:
    """R3: a legacy file without the line, an unrelated edit, remote disabled: stays disabled.

    The record is never derived from the remote: reading "remote disabled, no
    line in the file" as "the user deleted the line" re-enabled the config on
    any unrelated push.
    """
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v, disabled=True)
    config_dir = w.config_dir(cid)
    _make_legacy(w, v.component_id, cid, config_dir / CONFIG_FILE)
    assert dirty(w.diff()["summary"]) == {}

    for _ in range(2):
        semantic_edit(config_dir / "transform.sql")
        w.push()
        assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is True
        assert DISABLED_BASELINE_KEY not in _entry(w, v.component_id, cid).metadata


def test_r3_legacy_row_file_without_record_never_reenables(tmp_path: Path) -> None:
    """R3, row variant: the legacy row's push sends no state."""
    v = VARIANTS["rows"]
    w, cid = _pulled(tmp_path, v, row_disabled=True)
    row_file = _disabled_row_file(w, cid)
    row_id = str(_read(row_file)["_keboola"]["row_id"])
    _make_legacy(w, v.component_id, cid, row_file)
    # No clean check here: a row diff compares the file itself (no unchanged-
    # file shortcut), so such a legacy row reads as modified (as before).

    semantic_edit(row_file)
    w.push()
    (row,) = [r for r in w.api.get(PROD, v.component_id, cid)["rows"] if r["id"] == row_id]
    assert row["isDisabled"] is True


def test_r4_no_stale_record_after_a_reenable_push(tmp_path: Path) -> None:
    """R4: re-enable push, then a parameter edit pushed: still enabled, no phantom diff."""
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v, disabled=True)
    cfg = w.config_dir(cid) / CONFIG_FILE
    _drop_flag(cfg)
    w.push()
    assert DISABLED_BASELINE_KEY not in _entry(w, v.component_id, cid).metadata

    semantic_edit(cfg)
    w.push()
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is False
    assert dirty(w.diff()["summary"]) == {}
    assert DISABLED_BASELINE_KEY not in _entry(w, v.component_id, cid).metadata

    # Disabled in the UI again: the next pull writes the line and the record.
    w.api.get(PROD, v.component_id, cid)["isDisabled"] = True
    w.pull()
    assert _read(cfg)["is_disabled"] is True
    assert baseline_disabled(_entry(w, v.component_id, cid).metadata)


def test_r5_promote_first_update_takes_the_record_of_the_source_entry(tmp_path: Path) -> None:
    """R5: main/ read for ``push --branch DEV``, no dev entry yet: the production
    entry's baseline (the one the dev diff compared against) says the line was there."""
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v, disabled=True)
    w.api.branch_from_prod()
    _drop_flag(w.config_dir(cid) / CONFIG_FILE)

    w.push(branch=DEV)
    assert w.api.get(DEV, v.component_id, cid)["isDisabled"] is False
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is True
    assert baseline_disabled(_entry(w, v.component_id, cid).metadata), (
        "the production record changed, but production never got the push"
    )
    assert dirty(w.diff(branch=DEV)["summary"]) == {}
    assert w.diff()["summary"]["modified"] == 1
    w.push()
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is False


def test_r5_promote_takes_the_record_of_the_dev_entry_not_production(tmp_path: Path) -> None:
    """R5: production and dev baselines differ. The dev entry (promote association
    to this main/ directory) is what the dev diff used; production's says nothing."""
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v)
    w.api.branch_from_prod()
    cfg = w.config_dir(cid) / CONFIG_FILE
    original = cfg.read_bytes()
    data = _read(cfg)
    data["is_disabled"] = True
    _write(cfg, data)
    w.push(branch=DEV)
    assert w.api.get(DEV, v.component_id, cid)["isDisabled"] is True
    assert baseline_disabled(_entry(w, v.component_id, cid, DEV).metadata)
    assert not baseline_disabled(_entry(w, v.component_id, cid).metadata)

    cfg.write_bytes(original)  # delete the line again
    w.push(branch=DEV)
    assert w.api.get(DEV, v.component_id, cid)["isDisabled"] is False
    assert w.api.get(PROD, v.component_id, cid)["isDisabled"] is False
    assert dirty(w.diff(branch=DEV)["summary"]) == {}
    assert dirty(w.diff()["summary"]) == {}


def test_r6_ui_reenabled_meanwhile_is_no_change_and_a_rename_is_a_conflict(
    tmp_path: Path,
) -> None:
    """R6: three-way semantics are unchanged; the record never bypasses a conflict."""
    v = VARIANTS["sql"]
    w, cid = _pulled(tmp_path, v, disabled=True)
    remote = w.api.get(PROD, v.component_id, cid)
    _drop_flag(w.config_dir(cid) / CONFIG_FILE)
    remote["isDisabled"] = False  # re-enabled in the UI too
    assert dirty(w.diff()["summary"]) == {}
    assert w.push()["updated"] == 0

    remote["name"] = "Renamed In UI"
    assert w.diff()["summary"]["conflict"] == 1
    result = w.push()
    assert result["updated"] == 0 and result.get("skipped")
    assert (remote["name"], remote["isDisabled"]) == ("Renamed In UI", False)


# ===========================================================================
# C. pyproject.toml identity fields
# ===========================================================================


@pytest.mark.parametrize("variant", ["python", "app"])
def test_r8_rename_push_refreshes_only_owned_pyproject_fields(tmp_path: Path, variant: str) -> None:
    """R8: user content of pyproject.toml survives byte for byte; ``name`` follows."""
    v = VARIANTS[variant]
    w, cid = _pulled(tmp_path, v)
    config_dir = w.config_dir(cid)
    pyproject = config_dir / PYPROJECT
    pulled = pyproject.read_bytes().decode("utf-8")
    assert 'name = "orders"' in pulled
    custom = (
        pulled.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"  # pinned')
        + "\n# lint settings, kept\n[tool.ruff]\nline-length = 100\n"
    )
    pyproject.write_bytes(custom.encode("utf-8"))
    _rename(config_dir / CONFIG_FILE, "Renamed Twice")

    w.push()
    assert pyproject.read_bytes().decode("utf-8") == custom.replace(
        'name = "orders"', 'name = "renamed-twice"'
    )
    if variant == "app":
        assert f'config_id = "{cid}"' in custom
    assert dirty(w.diff()["summary"]) == {}


def test_r9_no_pyproject_is_created(tmp_path: Path) -> None:
    """R9: a config pull wrote no pyproject.toml for (no packages) gets none from push."""
    body = VARIANTS["python"].body
    v = Variant(
        "python-nopkg",
        "keboola.python-transformation-v2",
        "transformation",
        lambda: {"parameters": {"blocks": body()["parameters"]["blocks"]}},
    )
    w, cid = _pulled(tmp_path, v)
    config_dir = w.config_dir(cid)
    assert not (config_dir / PYPROJECT).exists()
    _rename(config_dir / CONFIG_FILE, "Renamed")
    w.push()
    assert not (config_dir / PYPROJECT).exists()


def _pyproject_config_id(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("config_id = "):
            return yaml.safe_load(line.split("=", 1)[1])
    return None


def test_r10_promote_never_writes_the_dev_id_into_main(tmp_path: Path) -> None:
    """R10: promote app P to a dev branch without it (new id D): main/ keeps P."""
    v = VARIANTS["app"]
    w, cid = _pulled(tmp_path, v)
    pyproject = w.config_dir(cid) / PYPROJECT
    w.push(branch=DEV)
    ((_, dev_cfg),) = w.api.remote[DEV].items()
    assert dev_cfg["id"] != cid
    assert _pyproject_config_id(pyproject) == cid

    _rename(w.config_dir(cid) / CONFIG_FILE, "Renamed")
    w.push(branch=DEV)
    assert _pyproject_config_id(pyproject) == cid
    assert 'name = "renamed"' in pyproject.read_text(encoding="utf-8")
    assert dirty(w.diff(branch=DEV)["summary"]) == {}


def test_r10_promote_of_an_authored_app_writes_no_id_into_main(tmp_path: Path) -> None:
    """R10: an app authored in main/ and created on DEV gets no config_id in main/."""
    v = VARIANTS["app"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    config_dir = author_config(w.root / "main", v)
    w.push(branch=DEV)
    assert _pyproject_config_id(config_dir / PYPROJECT) is None
    w.push()  # now created on production: its own id lands in main/
    ((_, prod_cfg),) = w.api.remote[PROD].items()
    assert _pyproject_config_id(config_dir / PYPROJECT) == prod_cfg["id"]


def test_r11_clone_writes_the_destination_id(tmp_path: Path) -> None:
    """R11: ``sync clone`` of an app: the target's pyproject names the target's id."""
    from test_sync_bookkeeping_differential import Api

    v = VARIANTS["app"]
    src = World(tmp_path, name="source")
    source_id = src.api.seed(v)
    src.init()
    src.pull()
    target_api = Api(first_id=5000)
    target_api.types = dict(src.api.types)
    w = World(tmp_path, target_api, name="target")
    w.root.rmdir()
    assert w.svc.clone_project(src.root, "prod", w.root).get("errors", []) == []
    ((_, new_cfg),) = target_api.remote[PROD].items()
    (pyproject,) = (w.root / "main").rglob(PYPROJECT)
    assert _pyproject_config_id(pyproject) == new_cfg["id"] != source_id


def _snapshot(w: World, config_dir: Path) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    files = {p.name: p.read_bytes() for p in config_dir.iterdir() if p.is_file()}
    return files, [c.model_dump() for c in load_manifest(w.root).configurations]


def test_r12_failed_update_leaves_files_and_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: a refused update changes neither pyproject.toml nor the manifest."""
    v = VARIANTS["app"]
    w, cid = _pulled(tmp_path, v)
    config_dir = w.config_dir(cid)
    _rename(config_dir / CONFIG_FILE, "Renamed")
    before = _snapshot(w, config_dir)
    monkeypatch.setattr(w.api, "_update", _boom)
    result = w.svc.push(alias="prod", project_root=w.root)
    assert result["errors"]
    assert _snapshot(w, config_dir) == before


def test_r12_failed_create_leaves_files_and_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: a refused create writes no id into pyproject.toml and records nothing."""
    v = VARIANTS["app"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    config_dir = author_config(w.root / "main", v)
    before = _snapshot(w, config_dir)
    monkeypatch.setattr(w.api, "_create", _boom)
    result = w.svc.push(alias="prod", project_root=w.root)
    assert result["errors"]
    assert _snapshot(w, config_dir) == before


# ===========================================================================
# D. Row entries carry no config_hash_version
# ===========================================================================


def _rows_of(entry: ManifestConfiguration) -> dict[str, dict[str, Any]]:
    return {r.id: r.metadata for r in entry.rows}


def test_r13_row_entries_carry_no_hash_version_after_push(tmp_path: Path) -> None:
    """R13: an old row marker is removed when the row's baseline is refreshed; a
    created row gets none; config entries keep theirs (it has readers)."""
    v = VARIANTS["rows"]
    w, cid = _pulled(tmp_path, v)
    manifest = load_manifest(w.root)
    (entry,) = manifest.configurations
    pulled_rows = {row.id for row in entry.rows}
    for row in entry.rows:
        row.metadata[CONFIG_HASH_VERSION_KEY] = 2  # written by an older push
    save_manifest(w.root, manifest)
    (first, *_) = sorted(w.config_dir(cid).glob("rows/*/_config.yml"))
    semantic_edit(first)
    author_row(w.config_dir(cid), v, {**_rows()[0], "name": "Row 3"})

    w.push()
    entry = _entry(w, v.component_id, cid)
    first_id = str(_read(first)["_keboola"]["row_id"])
    rows = _rows_of(entry)
    assert CONFIG_HASH_VERSION_KEY not in rows[first_id]
    created = set(rows) - pulled_rows
    assert len(created) == 1 and all(CONFIG_HASH_VERSION_KEY not in rows[r] for r in created)
    assert entry.metadata.get(CONFIG_HASH_VERSION_KEY)

    w.pull()  # a pull refreshes the untouched row too
    assert all(
        CONFIG_HASH_VERSION_KEY not in m for m in _rows_of(_entry(w, v.component_id, cid)).values()
    )
