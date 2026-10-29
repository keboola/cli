"""Sync reads local ``_config.yml`` files strictly (customer feedback 2.2-3.6, #827 item 9).

Every test drives the real ``SyncService`` (init + pull into a tmp tree with a
mocked Storage client), edits files the way a user does, then runs diff/push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest

VERIFY = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)
BRANCHES = [{"id": 12345, "name": "Main", "isDefault": True}]
COMPONENT = "keboola.ex-db-snowflake"


def _components(description: str = "Fetches data") -> list[dict[str, Any]]:
    return [
        {
            "id": COMPONENT,
            "type": "extractor",
            "configurations": [
                {
                    "id": "cfg-001",
                    "name": "My Extractor",
                    "description": description,
                    "configuration": {"parameters": {"db": "x"}},
                    "rows": [
                        {
                            "id": "row-001",
                            "name": "Orders",
                            "description": "",
                            "configuration": {"parameters": {"table": "orders"}},
                            "isDisabled": False,
                        }
                    ],
                }
            ],
        }
    ]


def _client(components: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.verify_token.return_value = VERIFY
    client.list_dev_branches.return_value = BRANCHES
    client.list_components_with_configs.return_value = components
    client.list_buckets_with_metadata.return_value = []
    client.list_tables_with_metadata.return_value = []
    client.list_jobs_grouped.return_value = []
    client.list_config_metadata.return_value = []
    return client


def _pulled_tree(tmp_path: Path, components: list[dict[str, Any]]) -> tuple[ConfigStore, Path]:
    root = tmp_path / "project"
    root.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    store = setup_single_project(cfg_dir)
    client = _client(components)
    svc = SyncService(config_store=store, client_factory=lambda url, token: client)
    svc.init_sync(alias="prod", project_root=root)
    svc.pull(alias="prod", project_root=root)
    return store, root


def _service(store: ConfigStore, components: list[dict[str, Any]]) -> tuple[SyncService, MagicMock]:
    client = _client(components)
    return SyncService(config_store=store, client_factory=lambda url, token: client), client


def _config_dir(root: Path) -> Path:
    cfg = next(c for c in load_manifest(root).configurations if c.id == "cfg-001")
    return root / "main" / cfg.path


def _row_dir(root: Path) -> Path:
    cfg = next(c for c in load_manifest(root).configurations if c.id == "cfg-001")
    return root / "main" / cfg.path / cfg.rows[0].path


def _edit_yaml(path: Path, edit: Any) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Item 4: a missing description is an empty one; a shadowed one is reported
# ---------------------------------------------------------------------------


def test_row_without_description_key_is_not_a_change(tmp_path: Path) -> None:
    """A row ``_config.yml`` with no ``description:`` diffs clean (remote has "")."""
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(_row_dir(root) / CONFIG_FILENAME, lambda d: d.pop("description", None))

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    assert result["changes"] == []


def test_config_without_description_key_matches_empty_remote(tmp_path: Path) -> None:
    components = _components(description="")
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(_config_dir(root) / CONFIG_FILENAME, lambda d: d.pop("description", None))

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    assert result["changes"] == []


def test_description_md_and_differing_inline_description_warn(tmp_path: Path) -> None:
    """``_description.md`` still wins, and the ignored inline text is reported."""
    components = _components(description="Fetches data")
    store, root = _pulled_tree(tmp_path, components)
    config_dir = _config_dir(root)
    assert (config_dir / "_description.md").is_file()
    _edit_yaml(config_dir / CONFIG_FILENAME, lambda d: d.update(description="Something else"))

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    warnings = result.get("companion_warnings", [])
    shadowed = [w for w in warnings if w["code"] == "DESCRIPTION_SHADOWED"]
    assert len(shadowed) == 1
    assert "_description.md" in shadowed[0]["message"]
    assert CONFIG_FILENAME in shadowed[0]["message"]
    # The file wins: the description equals the remote's.
    assert all("description" not in d for c in result["changes"] for d in c.get("details", []))


def test_matching_inline_description_does_not_warn(tmp_path: Path) -> None:
    components = _components(description="Fetches data")
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(_config_dir(root) / CONFIG_FILENAME, lambda d: d.update(description="Fetches data"))

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    assert not result.get("companion_warnings")
