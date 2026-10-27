"""A config CREATED by `sync push` must track its companion code files.

`sync pull` records a hash for every companion file (`transform.sql`,
`code.py`, ...) under `pull_extra_hashes`; diff reads an edit to one of them
only through that record. The create path recorded `_config.yml` alone, so
after a push that created a config, a later edit to its SQL diffed as
"unchanged" and the next push sent nothing -- exit 0, the job kept running the
old code. Reported by a customer on a dev-branch loop (author in `main/`,
`sync push --branch <dev>`, edit `transform.sql`, push again).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import test_sync_formal_counterexamples as formal
from test_sync_formal_counterexamples import DEV, PROD, World

TRANSFORMATION = "keboola.snowflake-transformation"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A sync world whose fake remote lists configs under a SQL transformation."""
    monkeypatch.setattr(formal, "COMP", TRANSFORMATION)
    w = World(tmp_path)
    return w


def _author_transformation(root: Path) -> Path:
    """Write a new, untracked transformation under main/ (no config id yet)."""
    config_dir = root / "main" / "transformation" / TRANSFORMATION / "orders-model"
    config_dir.mkdir(parents=True)
    (config_dir / "_config.yml").write_text(
        "version: 2\nname: orders-model\ndescription: ''\nparameters: {}\n"
        f"_keboola:\n  component_id: {TRANSFORMATION}\n"
    )
    (config_dir / "transform.sql").write_text("SELECT 1 FROM VALUES (1);\n")
    return config_dir


def _edit_sql(config_dir: Path) -> None:
    sql = config_dir / "transform.sql"
    sql.write_text(sql.read_text().replace("VALUES", "VALUEZ"))


def _remote_sql(world: World, branch: int, config_id: str) -> str:
    blocks = world.api.remote[branch][config_id]["configuration"]["parameters"]["blocks"]
    return "".join(s for b in blocks for c in b["codes"] for s in c["script"])


def test_sql_edit_after_create_on_default_branch_is_pushed(world: World) -> None:
    world.init()
    world.pull()
    config_dir = _author_transformation(world.root)
    assert world.push().get("created", 0) == 1
    (config_id,) = world.api.ids(PROD)

    _edit_sql(config_dir)

    assert world.diff()["summary"]["modified"] == 1
    assert world.push().get("updated", 0) == 1
    assert "VALUEZ" in _remote_sql(world, PROD, config_id)


def test_sql_edit_after_promote_create_to_dev_branch_is_pushed(world: World) -> None:
    """The customer's loop: author in main/, push to a dev branch, edit, push."""
    world.init()
    world.pull()
    config_dir = _author_transformation(world.root)
    assert world.push(branch=DEV).get("created", 0) == 1
    (config_id,) = world.api.ids(DEV)

    _edit_sql(config_dir)

    assert world.diff(branch=DEV)["summary"]["modified"] == 1
    second = world.push(branch=DEV)
    assert second.get("updated", 0) == 1
    assert second.get("created", 0) == 0
    assert "VALUEZ" in _remote_sql(world, DEV, config_id)
    assert world.api.ids(PROD) == []


def test_sql_edit_after_promoting_a_pulled_config_is_pushed(world: World) -> None:
    """A production config promoted to a dev branch gets a NEW dev entry."""
    world.api.put(
        PROD,
        "cfg-1",
        "Orders",
        "a",
        extra={"blocks": [{"name": "B", "codes": [{"name": "C", "script": ["SELECT 1;"]}]}]},
    )
    world.init()
    world.pull()
    world.push(branch=DEV)
    (dev_id,) = world.api.ids(DEV)
    sql = world.config_dir("orders") / "transform.sql"
    sql.write_text(sql.read_text().replace("SELECT 1", "SELECT 2"))

    assert world.diff(branch=DEV)["summary"]["modified"] == 1
    assert world.push(branch=DEV).get("updated", 0) == 1
    assert "SELECT 2" in _remote_sql(world, DEV, dev_id)
    assert "SELECT 1" in _remote_sql(world, PROD, "cfg-1")


def test_unedited_config_stays_clean_after_create(world: World) -> None:
    """Recording the companion hashes must not turn a clean tree dirty."""
    world.init()
    world.pull()
    _author_transformation(world.root)
    world.push(branch=DEV)

    summary = world.diff(branch=DEV)["summary"]
    assert summary["modified"] == 0
    assert summary["added"] == 0
    assert world.push(branch=DEV).get("updated", 0) == 0


def test_force_pull_does_not_overwrite_sql_edit_after_create(world: World) -> None:
    """Without the companion hashes, pull read an edited `transform.sql` as
    untouched and `--force` replaced it with the remote code once the config
    changed remotely (here: renamed in the UI). It is now a conflict."""
    from keboola_agent_cli.errors import SyncConflictError

    world.init()
    world.pull()
    config_dir = _author_transformation(world.root)
    world.push()
    (config_id,) = world.api.ids(PROD)
    _edit_sql(config_dir)
    world.api.remote[PROD][config_id]["name"] = "renamed in the UI"

    with pytest.raises(SyncConflictError):
        world.pull(force=True)
    assert "VALUEZ" in (config_dir / "transform.sql").read_text()
