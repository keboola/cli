"""Sync reads local ``_config.yml`` files strictly (customer feedback 2.2-3.6, #827 item 9).

Every test drives the real ``SyncService`` (init + pull into a tmp tree with a
mocked Storage client), edits files the way a user does, then runs diff/push.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services._sync_baseline import adopted_skip_detail
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.config_format import api_row_to_local, unknown_local_keys
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


# ---------------------------------------------------------------------------
# Item 1 (#827 item 9): an unparseable _config.yml is an error, never a delete
# ---------------------------------------------------------------------------

BROKEN_YAML = "name: [unclosed\nparameters: {db: x\n"


def _invalid(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in result["errors"] if e.get("error_code") == "SYNC_LOCAL_FILE_INVALID"]


def test_o_unparseable_config_yaml_is_not_deleted_remotely(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.delete_config.assert_not_called()
    client.delete_config_row.assert_not_called()
    client.update_config.assert_not_called()
    client.create_config.assert_not_called()
    assert result["deleted"] == 0
    (error,) = _invalid(result)
    assert error["config_id"] == "cfg-001"
    assert error["path"].startswith("main/") and error["path"].endswith(CONFIG_FILENAME)
    assert "not valid YAML" in error["message"]


def test_unparseable_config_yaml_diff_reports_error_not_delete(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    assert result["changes"] == []  # neither the config nor its row is "deleted"
    assert result["summary"]["deleted"] == 0
    assert result["summary"]["invalid"] == 1
    assert len(_invalid(result)) == 1
    assert result["remote_only"] == []


def test_non_mapping_config_yaml_is_an_error(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text("- just\n- a list\n", encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.delete_config.assert_not_called()
    (error,) = _invalid(result)
    assert "must be a YAML mapping" in error["message"]


def test_empty_config_yaml_is_an_error(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text("", encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.delete_config.assert_not_called()
    assert len(_invalid(result)) == 1


def test_unparseable_config_yaml_dry_run_reports_error(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True, dry_run=True)

    # Nothing to plan once the config is held: no delete in any form.
    assert result.get("summary", {}).get("deleted", 0) == 0
    assert not [c for c in result.get("changes", []) if c["change_type"] == "deleted"]
    assert len(_invalid(result)) == 1
    client.delete_config.assert_not_called()


def test_unparseable_row_yaml_is_not_deleted_remotely(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_row_dir(root) / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.delete_config_row.assert_not_called()
    client.update_config_row.assert_not_called()
    (error,) = _invalid(result)
    assert "/rows/" in error["path"]
    assert error["config_id"] == "cfg-001"


def test_unparseable_untracked_config_is_reported_not_skipped(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    new_dir = root / "main" / "extractor" / COMPONENT / "brand-new"
    new_dir.mkdir(parents=True)
    (new_dir / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.create_config.assert_not_called()
    (error,) = _invalid(result)
    assert "brand-new" in error["path"]


def test_pull_keeps_unparseable_tracked_file(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    config_file = _config_dir(root) / CONFIG_FILENAME
    config_file.write_text(BROKEN_YAML, encoding="utf-8")

    svc, _ = _service(store, components)
    svc.pull(alias="prod", project_root=root)

    assert config_file.read_text(encoding="utf-8") == BROKEN_YAML


def test_pull_does_not_overwrite_unparseable_untracked_file(tmp_path: Path) -> None:
    """Pull's adopt guard keeps a broken file where it would write a new config."""
    components = _components()
    store, _root = _pulled_tree(tmp_path, components)
    config_dir = tmp_path / "scratch" / "extractor" / COMPONENT / "my-extractor"
    config_dir.mkdir(parents=True)
    (config_dir / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")
    svc, _ = _service(store, components)
    remote = components[0]["configurations"][0]

    skipped = adopted_skip_detail(svc, config_dir, COMPONENT, remote, "rel/path")

    assert skipped is not None
    assert skipped["action"] == "skipped"
    assert "cannot be parsed" in skipped["reason"]


# ---------------------------------------------------------------------------
# Item 2: an unknown top-level key is an error, never silently dropped
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_in_config_is_an_error(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(
        _config_dir(root) / CONFIG_FILENAME,
        lambda d: d.update(storage={"input": {"tables": []}}, runtime={"backend": "large"}),
    )

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.update_config.assert_not_called()
    client.delete_config.assert_not_called()
    (error,) = _invalid(result)
    assert "runtime, storage" in error["message"]
    assert "_configuration_extra" in error["message"]
    assert error["config_id"] == "cfg-001"


def test_unknown_top_level_key_in_row_is_an_error(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(_row_dir(root) / CONFIG_FILENAME, lambda d: d.update(tables=["orders"]))

    svc, client = _service(store, components)
    result = svc.push(alias="prod", project_root=root, force=True)

    client.update_config_row.assert_not_called()
    client.update_config.assert_not_called()
    (error,) = _invalid(result)
    assert "/rows/" in error["path"]
    assert "tables" in error["message"]


def test_key_under_configuration_extra_is_not_an_error(tmp_path: Path) -> None:
    """The documented escape hatch still works: no error, the change is planned."""
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(
        _config_dir(root) / CONFIG_FILENAME,
        lambda d: d.update(_configuration_extra={"runtime": {"backend": "large"}}),
    )

    svc, _ = _service(store, components)
    result = svc.diff(alias="prod", project_root=root)

    assert result["errors"] == []
    assert [c["change_type"] for c in result["changes"]] == ["modified"]


def test_hoisted_row_keys_are_not_unknown() -> None:
    """A keboola.variables row keeps ``values`` at the top level, as pull writes it."""
    row = api_row_to_local(
        {"id": "r", "name": "V", "configuration": {"values": [{"name": "x", "value": "1"}]}},
        "keboola.variables",
    )
    assert "values" in row
    assert unknown_local_keys(row, "keboola.variables", is_row=True) == []
    assert unknown_local_keys({"values": []}, COMPONENT, is_row=True) == ["values"]


VARIED_COMPONENTS: list[dict[str, Any]] = [
    *_components(),
    {
        "id": "keboola.snowflake-transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "tr-001",
                "name": "Transform",
                "description": "Long text",
                "isDisabled": True,
                "configuration": {
                    "parameters": {
                        "blocks": [{"name": "B", "codes": [{"name": "C", "script": ["SELECT 1;"]}]}]
                    },
                    "storage": {
                        "input": {"tables": [{"source": "in.c-a.t", "destination": "t"}]},
                        "output": {"tables": [{"source": "o", "destination": "out.c-a.o"}]},
                    },
                    "processors": {"after": []},
                    "runtime": {"backend": {"size": "small"}},
                },
                "rows": [],
            }
        ],
    },
    {
        "id": "keboola.flow",
        "type": "other",
        "configurations": [
            {
                "id": "flow-001",
                "name": "Flow",
                "description": "",
                "configuration": {"phases": [{"id": "p1", "name": "P1"}], "tasks": []},
                "rows": [],
            }
        ],
    },
    {
        "id": "keboola.variables",
        "type": "other",
        "configurations": [
            {
                "id": "var-001",
                "name": "Vars",
                "description": "",
                "configuration": {"variables": [{"name": "x", "type": "string"}]},
                "rows": [
                    {
                        "id": "vrow-001",
                        "name": "Values",
                        "description": "",
                        "configuration": {"values": [{"name": "x", "value": "1"}]},
                        "isDisabled": False,
                    }
                ],
            }
        ],
    },
]


def test_every_file_pull_writes_passes_the_check(tmp_path: Path) -> None:
    """Round trip: no key pull writes (configs, rows, hoisted rows) is unknown."""
    store, root = _pulled_tree(tmp_path, VARIED_COMPONENTS)
    assert len(list((root / "main").rglob(CONFIG_FILENAME))) >= 6

    svc, _ = _service(store, VARIED_COMPONENTS)
    result = svc.diff(alias="prod", project_root=root)

    assert result["errors"] == []


# ---------------------------------------------------------------------------
# The commands: errors[] -> exit 1, the result still printed
# ---------------------------------------------------------------------------


def _run_cli(store: ConfigStore, svc: SyncService, args: list[str]) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.SyncService") as mock_sync,
    ):
        mock_store.return_value = store
        mock_projects.return_value = ProjectService(config_store=store)
        mock_sync.return_value = svc
        return CliRunner().invoke(app, args)


def test_cli_push_force_with_unparseable_file_exits_1_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    (_config_dir(root) / CONFIG_FILENAME).write_text(BROKEN_YAML, encoding="utf-8")
    svc, client = _service(store, components)

    result = _run_cli(
        store,
        svc,
        ["--json", "sync", "push", "--project", "prod", "--directory", str(root), "--force"],
    )

    assert result.exit_code == 1, result.output
    envelope = json.loads(result.output)
    assert envelope["exit_code"] == 1
    assert len(_invalid(envelope["data"])) == 1
    client.delete_config.assert_not_called()


def test_cli_diff_with_unknown_key_exits_1(tmp_path: Path) -> None:
    components = _components()
    store, root = _pulled_tree(tmp_path, components)
    _edit_yaml(_config_dir(root) / CONFIG_FILENAME, lambda d: d.update(storage={}))
    svc, _ = _service(store, components)

    result = _run_cli(
        store, svc, ["--json", "sync", "diff", "--project", "prod", "--directory", str(root)]
    )

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)["data"]
    assert data["summary"]["invalid"] == 1
    assert "storage" in data["errors"][0]["message"]
