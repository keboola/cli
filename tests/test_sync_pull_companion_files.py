"""`sync pull` protects local edits in companion files, not only _config.yml.

Issue #792 finding B: pull decided "locally modified" from ``_config.yml``
alone, so an edited ``transform.sql`` / ``_description.md`` was silently
overwritten when the remote changed -- plain pull AND ``--force`` alike.
The check now covers every file recorded in ``pull_extra_hashes`` (the set
diff/push merge back into the config).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import SyncConflictError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest

SQL_COMP = "keboola.snowflake-transformation"


def _sql_components(sql: str, description: str = "", row_value: str | None = None) -> list:
    rows: list[dict[str, Any]] = []
    if row_value is not None:
        rows = [
            {
                "id": "r1",
                "name": "Row One",
                "description": "",
                "configuration": {"parameters": {"value": row_value}},
                "isDisabled": False,
            }
        ]
    return [
        {
            "id": SQL_COMP,
            "type": "transformation",
            "configurations": [
                {
                    "id": "t1",
                    "name": "My SQL",
                    "description": description,
                    "rows": rows,
                    "configuration": {
                        "parameters": {
                            "blocks": [{"name": "B", "codes": [{"name": "C", "script": [sql]}]}]
                        }
                    },
                }
            ],
        }
    ]


def _client(components: list | None = None) -> MagicMock:
    c = MagicMock()
    c.__enter__ = MagicMock(return_value=c)
    c.__exit__ = MagicMock(return_value=False)
    c.verify_token.return_value = TokenVerifyResponse(
        token_id="t", token_description="d", project_id=258, project_name="P", owner_name="O"
    )
    c.list_dev_branches.return_value = [{"id": 12345, "name": "Main", "isDefault": True}]
    if components is not None:
        c.list_components_with_configs.return_value = components
    return c


def _svc(store: ConfigStore, components: list | None = None) -> SyncService:
    client = _client(components)
    return SyncService(config_store=store, client_factory=lambda url, token: client)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[ConfigStore, Path]:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    store = setup_single_project(cfg_dir)
    _svc(store).init_sync(alias="prod", project_root=root)
    return store, root


def _pull(store: ConfigStore, root: Path, components: list, **kw: Any) -> dict:
    return _svc(store, components).pull(alias="prod", project_root=root, **kw)


def _detail(result: dict) -> dict:
    return next(d for d in result["details"] if d["component_id"] == SQL_COMP)


def test_plain_pull_preserves_edited_sql_and_keeps_it_pushable(
    tree: tuple[ConfigStore, Path],
) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))
    sql_file = next(root.rglob("transform.sql"))
    sql_file.write_text(sql_file.read_text().replace("SELECT 1", "SELECT 42"))

    result = _pull(store, root, _sql_components("SELECT 100;"))

    assert "SELECT 42" in sql_file.read_text()
    assert _detail(result)["action"] == "skipped"
    assert _detail(result)["reason"] == "locally modified"
    # The companion baseline survives the preserving pull, so diff/push still
    # see the edit as a pending local change instead of "unchanged".
    entry = next(c for c in load_manifest(root).configurations if c.id == "t1")
    assert "transform.sql" in entry.metadata["pull_extra_hashes"]
    status = _svc(store).status(project_root=root)
    assert any(m["config_id"] == "t1" for m in status["modified"])


def test_force_pull_reports_conflict_for_edited_sql(tree: tuple[ConfigStore, Path]) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))
    sql_file = next(root.rglob("transform.sql"))
    sql_file.write_text(sql_file.read_text().replace("SELECT 1", "SELECT 42"))

    with pytest.raises(SyncConflictError) as exc:
        _pull(store, root, _sql_components("SELECT 100;"), force=True)

    assert [c["config_id"] for c in exc.value.conflicts] == ["t1"]
    assert "SELECT 42" in sql_file.read_text()


def test_force_pull_preserves_edited_sql_when_remote_unchanged(
    tree: tuple[ConfigStore, Path],
) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))
    sql_file = next(root.rglob("transform.sql"))
    sql_file.write_text(sql_file.read_text().replace("SELECT 1", "SELECT 42"))

    result = _pull(store, root, _sql_components("SELECT 1;"), force=True)

    assert "SELECT 42" in sql_file.read_text()
    assert _detail(result)["action"] == "skipped"


def test_theirs_still_overwrites_edited_sql(tree: tuple[ConfigStore, Path]) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))
    sql_file = next(root.rglob("transform.sql"))
    sql_file.write_text(sql_file.read_text().replace("SELECT 1", "SELECT 42"))

    _pull(store, root, _sql_components("SELECT 100;"), theirs=True)

    assert "SELECT 100" in sql_file.read_text()


def test_plain_pull_preserves_edited_description(tree: tuple[ConfigStore, Path]) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;", description="old"))
    desc_file = next(root.rglob("_description.md"))
    desc_file.write_text("my local description")

    result = _pull(store, root, _sql_components("SELECT 1;", description="remote new"))

    assert desc_file.read_text() == "my local description"
    assert _detail(result)["action"] == "skipped"


def test_unedited_companion_files_take_remote_change(tree: tuple[ConfigStore, Path]) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))

    result = _pull(store, root, _sql_components("SELECT 100;"))

    assert "SELECT 100" in next(root.rglob("transform.sql")).read_text()
    assert _detail(result)["action"] == "updated"


def test_deleted_config_dir_is_rematerialized(tree: tuple[ConfigStore, Path]) -> None:
    """A missing _config.yml is not a local edit to protect (#472)."""
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;"))
    config_dir = next(root.rglob("transform.sql")).parent
    for f in config_dir.iterdir():
        if f.is_file():
            f.unlink()

    _pull(store, root, _sql_components("SELECT 100;"))

    assert (config_dir / CONFIG_FILENAME).exists()
    assert "SELECT 100" in (config_dir / "transform.sql").read_text()


def test_edited_row_file_is_preserved_and_conflicts_under_force(
    tree: tuple[ConfigStore, Path],
) -> None:
    store, root = tree
    _pull(store, root, _sql_components("SELECT 1;", row_value="a"))
    row_file = next(p for p in root.rglob(CONFIG_FILENAME) if "rows" in p.parts)
    row_file.write_text(row_file.read_text().replace("value: a", "value: local"))

    with pytest.raises(SyncConflictError) as exc:
        _pull(store, root, _sql_components("SELECT 1;", row_value="remote"), force=True)
    assert [c.get("row_id") for c in exc.value.conflicts] == ["r1"]

    _pull(store, root, _sql_components("SELECT 1;", row_value="remote"))
    assert "value: local" in row_file.read_text()
