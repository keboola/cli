"""Untracked files that carry a config id: diff, push and pull (issue #792 E).

An untracked ``_config.yml`` whose ``_keboola.config_id`` exists on the target
branch is *adopted*: diff compares it with that remote config instead of
creating a duplicate (#482). Such a file has no manifest entry, so it has no
baseline. The 2-way fallback read every difference as a local edit, and push
overwrote an edit made in the UI. Now:

- without a baseline, a difference is a ``conflict`` (push does not apply it);
- a ``config new --push --output-dir`` scaffold records its own baseline
  (``_keboola.base_config_hash``), so its diff is 3-way;
- two files that adopt one id are both a ``conflict``;
- pull keeps an adopted file with local changes (``skipped``); only
  ``--theirs`` overwrites it.

The replay of the original finding is
``test_sync_formal_counterexamples.py::test_e_adopted_scaffold_push_does_not_overwrite_remote_edit``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.services._sync_baseline import ADOPTED_SKIP_REASON
from keboola_agent_cli.services.component_service import (
    materialize_pushed_config,
    pushed_config_base_hash,
    stamp_scaffold_config_id,
)
from keboola_agent_cli.sync.config_format import BASE_CONFIG_HASH_KEY
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_formal_counterexamples import COMP, PROD, World, changes

ORDERS_DIR = "main/extractor/keboola.ex-http/orders"


def _pulled_then_untracked(tmp_path: Path) -> World:
    """Pull one config, then drop its manifest entry: the file only carries the id."""
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a")
    w.init()
    w.pull()
    manifest = load_manifest(w.root)
    manifest.configurations = []
    save_manifest(w.root, manifest)
    return w


def _scaffolded(tmp_path: Path) -> World:
    """A config created remotely and written the way `config new --push --output-dir` does it."""
    w = World(tmp_path)
    w.init()
    w.pull()
    w.api.put(PROD, "cfg-1", "Orders", "a")
    remote = w.api.remote[PROD]["cfg-1"]
    materialize_pushed_config(
        component_id=COMP,
        config_id="cfg-1",
        name=remote["name"],
        description=remote["description"],
        configuration=remote["configuration"],
        config_dir=w.root / ORDERS_DIR,
    )
    assert w.manifest() == []
    return w


def _set_value(config_dir: Path, value: str) -> None:
    config_file = config_dir / CONFIG_FILENAME
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["parameters"]["value"] = value
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")


def _value(config_dir: Path) -> str:
    return yaml.safe_load((config_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))["parameters"][
        "value"
    ]


def _updates(w: World) -> list[str]:
    return [line for line in w.api.log if line.startswith("UPDATE")]


# ---------------------------------------------------------------------------
# diff / push without a baseline
# ---------------------------------------------------------------------------


def test_adopted_file_without_base_is_a_conflict_when_it_differs(tmp_path: Path) -> None:
    """A local edit and a remote edit look the same without a baseline."""
    w = _pulled_then_untracked(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")

    assert changes(w.diff()) == [("conflict", "cfg-1")]
    result = w.push()

    assert (result["status"], result["skipped"]) == ("no_changes", 1)
    assert _updates(w) == []
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "a"


def test_adopted_file_equal_to_the_remote_is_unchanged(tmp_path: Path) -> None:
    w = _pulled_then_untracked(tmp_path)

    diff = w.diff()

    assert diff["changes"] == []
    assert diff["summary"]["unchanged"] == 1
    assert w.push()["status"] == "no_changes"


# ---------------------------------------------------------------------------
# diff / push of a `config new --push --output-dir` scaffold (3-way)
# ---------------------------------------------------------------------------


def test_scaffold_records_the_remote_as_its_base(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    meta = yaml.safe_load((w.root / ORDERS_DIR / CONFIG_FILENAME).read_text())["_keboola"]

    assert meta[BASE_CONFIG_HASH_KEY] == pushed_config_base_hash(
        COMP, "cfg-1", w.api.remote[PROD]["cfg-1"]
    )
    assert w.diff()["changes"] == []


def test_scaffold_edit_is_pushed_and_the_config_becomes_tracked(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")

    assert changes(w.diff()) == [("modified", "cfg-1")]
    result = w.push()

    assert result["updated"] == 1
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "LOCAL"
    assert [cid for _, cid, _ in w.manifest()] == ["cfg-1"]
    # The manifest holds the baseline now; the file drops its own copy.
    assert BASE_CONFIG_HASH_KEY not in (w.root / ORDERS_DIR / CONFIG_FILENAME).read_text()
    assert w.diff()["changes"] == []


def test_scaffold_with_a_remote_edit_is_remote_modified_and_not_pushed(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    assert changes(w.diff()) == [("remote_modified", "cfg-1")]
    w.push()

    assert _updates(w) == []
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "EDITED-IN-UI"


def test_scaffold_edited_on_both_sides_is_a_conflict(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    assert changes(w.diff()) == [("conflict", "cfg-1")]
    w.push()

    assert _updates(w) == []


def test_placeholder_scaffold_is_modified_and_push_applies_it(tmp_path: Path) -> None:
    """`config new --push --output-dir` without --configuration pushes an empty
    body and writes templates: the documented "edit, then sync push" flow."""
    w = World(tmp_path)
    w.init()
    w.pull()
    w.api.remote[PROD]["cfg-1"] = {
        "id": "cfg-1",
        "name": "Orders",
        "description": "",
        "configuration": {},
        "rows": [],
        "isDisabled": False,
    }
    scaffold = {
        "component_id": COMP,
        "files": [
            {
                "path": CONFIG_FILENAME,
                "content": (
                    'version: 2\nname: "Orders"\ndescription: ""\n'
                    "parameters:\n  value: TODO\n\n_keboola:\n  component_id: keboola.ex-http\n"
                ),
            }
        ],
    }
    stamped = stamp_scaffold_config_id(
        scaffold, "cfg-1", pushed_config_base_hash(COMP, "cfg-1", w.api.remote[PROD]["cfg-1"])
    )
    config_dir = w.root / ORDERS_DIR
    config_dir.mkdir(parents=True)
    (config_dir / CONFIG_FILENAME).write_text(stamped["files"][0]["content"])

    assert changes(w.diff()) == [("modified", "cfg-1")]
    assert w.push()["updated"] == 1
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "TODO"


def test_two_files_adopting_one_id_are_both_conflicts(tmp_path: Path) -> None:
    """Both copies carry the same base; neither may overwrite the remote."""
    w = _scaffolded(tmp_path)
    copy_dir = w.root / "main/extractor/keboola.ex-http/orders-copy"
    shutil.copytree(w.root / ORDERS_DIR, copy_dir)
    _set_value(w.root / ORDERS_DIR, "FIRST")
    _set_value(copy_dir, "SECOND")

    assert changes(w.diff()) == [("conflict", "cfg-1"), ("conflict", "cfg-1")]
    w.push()

    assert _updates(w) == []
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "a"


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_pull_keeps_an_adopted_file_with_local_changes(tmp_path: Path, force: bool) -> None:
    w = _pulled_then_untracked(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    result = w.pull(force=force)

    assert [(d["action"], d.get("reason")) for d in result["details"]] == [
        ("skipped", ADOPTED_SKIP_REASON)
    ]
    assert _value(w.root / ORDERS_DIR) == "LOCAL"
    assert w.manifest() == []


def test_pull_theirs_overwrites_an_adopted_file(tmp_path: Path) -> None:
    w = _pulled_then_untracked(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    w.pull(theirs=True)

    assert _value(w.root / ORDERS_DIR) == "EDITED-IN-UI"
    assert [cid for _, cid, _ in w.manifest()] == ["cfg-1"]
    assert w.diff()["changes"] == []


def test_pull_takes_the_remote_edit_of_an_unedited_scaffold(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "EDITED-IN-UI")

    w.pull()

    assert _value(w.root / ORDERS_DIR) == "EDITED-IN-UI"
    assert [cid for _, cid, _ in w.manifest()] == ["cfg-1"]
    assert w.diff()["changes"] == []


def test_pull_keeps_an_edited_scaffold_and_push_applies_the_edit(tmp_path: Path) -> None:
    w = _scaffolded(tmp_path)
    _set_value(w.root / ORDERS_DIR, "LOCAL")

    result = w.pull()

    assert [d["action"] for d in result["details"]] == ["skipped"]
    assert _value(w.root / ORDERS_DIR) == "LOCAL"
    assert w.push()["updated"] == 1
    assert w.api.remote[PROD]["cfg-1"]["configuration"]["parameters"]["value"] == "LOCAL"
