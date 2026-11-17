"""Stateful Storage API double + working tree for the sync replay tests (#827).

The replay tests re-enact interleavings the formal sync model
(``Sync.tla``, keboola/cli#827) found against the REAL ``SyncService``
(push / pull / diff on a real working tree in ``tmp_path``). Only the outer
boundary is faked: the Storage API client. The fake follows the Storage API
behaviour verified on a live stack:

* ``list_components_with_configs`` returns LIVE configs only, each with its
  ``version``.
* ``delete_config``: live -> trashed (version + 1); trashed -> PURGED
  (recorded in ``purged``); a dev branch refuses to delete a trashed config.
* ``update_config`` / ``delete_config`` honour ``expected_version`` (the
  ``If-Match`` header): a stale one answers 412. Every write -- to the config
  or to one of its rows -- raises the config's version.
* ``update_config`` on a config that is not live answers 404.
* ``create_config`` with the ``configuration_id`` of a trashed config revives
  it under the same id (version + 1); of a live one answers 400.
* A lost response is modelled by applying the write and THEN raising a
  TIMEOUT ``KeboolaApiError`` (:func:`timeout_error`).
* Responses carry ``configuration`` unless ``partial_responses`` is set.

Faults are injected with one-shot hooks (``api.on(method, when, fn)``): ``fn``
runs right before or right after the fake applies ``method``, can mutate the
fake (a concurrent human in the UI) or the working tree (a concurrent human
in the editor), or raise :class:`Crash` to simulate the process dying at that
exact point. ``Crash`` derives from ``BaseException`` so it escapes every
``except Exception`` in the code under test -- like Ctrl-C / SIGKILL, nothing
after it runs.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from helpers import setup_single_project
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest

SQL = "keboola.snowflake-transformation"
BRANCHES = [{"id": 12345, "name": "Main", "isDefault": True}]
VERIFY = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)


class Crash(BaseException):
    """The kbagent process dies here (Ctrl-C / kill). Escapes ``except Exception``."""


def timeout_error() -> KeboolaApiError:
    """What the caller sees when the request was delivered but the response was lost."""
    return KeboolaApiError(
        message="Request timed out", status_code=0, error_code=ErrorCode.TIMEOUT, retryable=True
    )


def not_found(what: str) -> KeboolaApiError:
    return KeboolaApiError(
        message=f"{what} not found", status_code=404, error_code=ErrorCode.NOT_FOUND
    )


def precondition_failed(config_id: str, version: int, expected: int) -> KeboolaApiError:
    return KeboolaApiError(
        message=f"Configuration {config_id} is at version {version}, expected version {expected}",
        status_code=412,
        error_code=ErrorCode.API_ERROR,
    )


def sql_configuration(script: list[str]) -> dict[str, Any]:
    return {
        "parameters": {
            "blocks": [{"name": "Block 1", "codes": [{"name": "Code 1", "script": script}]}]
        }
    }


class FakeStorage:
    """Minimal stateful Storage API (configs + rows) with fault hooks."""

    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.purged: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.update_kwargs: list[dict[str, Any]] = []
        self.partial_responses = False
        self._next_id = 101
        self._next_row = 501
        self._hooks: dict[tuple[str, str], list[Callable[..., None]]] = {}

    # ---------------------------------------------------------------- setup
    def add(
        self,
        name: str,
        script: list[str] | None = None,
        *,
        config_id: str | None = None,
        component_id: str = SQL,
        configuration: dict[str, Any] | None = None,
        is_disabled: bool = False,
    ) -> str:
        cid = config_id or self._alloc()
        self.configs[cid] = {
            "component_id": component_id,
            "id": cid,
            "name": name,
            "description": "",
            "configuration": configuration
            if configuration is not None
            else sql_configuration(script or ["SELECT 1"]),
            "rows": [],
            "isDisabled": is_disabled,
            "state": "live",
            "version": 1,
        }
        return cid

    def on(self, method: str, when: str, fn: Callable[..., None]) -> None:
        """Register a ONE-SHOT hook run ``before`` or ``after`` ``method`` is applied."""
        self._hooks.setdefault((method, when), []).append(fn)

    def _fire(self, method: str, when: str, *args: Any) -> None:
        hooks = self._hooks.get((method, when), [])
        if hooks:
            hooks.pop(0)(*args)

    def _alloc(self) -> str:
        cid = str(self._next_id)
        self._next_id += 1
        return cid

    # ------------------------------------------------- human in the Keboola UI
    def ui_edit(self, cid: str, script: list[str]) -> None:
        cfg = self.configs[cid]
        assert cfg["state"] == "live"
        cfg["configuration"] = sql_configuration(script)
        cfg["version"] += 1

    def ui_describe(self, cid: str, text: str) -> None:
        cfg = self.configs[cid]
        assert cfg["state"] == "live"
        cfg["description"] = text
        cfg["version"] += 1

    def ui_trash(self, cid: str) -> None:
        assert self.configs[cid]["state"] == "live"
        self.configs[cid]["state"] = "trashed"
        self.configs[cid]["version"] += 1

    def ui_restore(self, cid: str) -> None:
        assert self.configs[cid]["state"] == "trashed"
        self.configs[cid]["state"] = "live"
        self.configs[cid]["version"] += 1

    def ui_rename(self, cid: str, name: str) -> None:
        self.configs[cid]["name"] = name
        self.configs[cid]["version"] += 1

    def ui_edit_row(self, cid: str) -> None:
        """A row write in the UI: it raises the parent config's version."""
        self.configs[cid]["version"] += 1

    # ------------------------------------------------------------- queries
    def live(self) -> list[dict[str, Any]]:
        return [c for c in self.configs.values() if c["state"] == "live"]

    def live_named(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.live() if c["name"] == name]

    def script(self, cid: str) -> list[str]:
        return self.configs[cid]["configuration"]["parameters"]["blocks"][0]["codes"][0]["script"]

    def updates_of(self, cid: str) -> int:
        return sum(1 for call in self.calls if call == ("update_config", cid))

    def _view(self, cfg: dict[str, Any]) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in cfg.items() if k not in ("state", "component_id")}

    def _response(self, cfg: dict[str, Any]) -> dict[str, Any]:
        return {"id": cfg["id"]} if self.partial_responses else self._view(cfg)

    # ------------------------------------------------------------- API surface
    def verify_token(self) -> TokenVerifyResponse:
        return VERIFY

    def list_dev_branches(self) -> list[dict[str, Any]]:
        return BRANCHES

    def list_config_folder_metadata(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def set_config_metadata(self, *args: Any, **kwargs: Any) -> Any:
        return []

    def encrypt_values(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    def list_components_with_configs(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list", ""))
        self._fire("list_components_with_configs", "before")
        by_component: dict[str, list[dict[str, Any]]] = {}
        for cfg in self.live():
            by_component.setdefault(cfg["component_id"], []).append(self._view(cfg))
        out = [
            {"id": comp, "type": "transformation", "configurations": cfgs}
            for comp, cfgs in by_component.items()
        ]
        self._fire("list_components_with_configs", "after")
        return out

    def get_config_detail(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("get_config_detail", str(config_id)))
        cfg = self.configs.get(str(config_id))
        if cfg is None or cfg["state"] != "live":
            raise not_found(f"config {config_id}")
        return self._view(cfg)

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
        **kw: Any,
    ) -> dict[str, Any]:
        self._fire("create_config", "before", name)
        wanted = kw.get("configuration_id")
        if wanted is not None and self.configs.get(str(wanted), {}).get("state") == "live":
            raise KeboolaApiError(
                message="configurationAlreadyExists",
                status_code=400,
                error_code=ErrorCode.API_ERROR,
            )
        cid = str(wanted) if wanted is not None else self._alloc()
        prior = self.configs.get(cid)
        revived_version = prior["version"] + 1 if prior and prior["state"] == "trashed" else 1
        self.configs[cid] = {
            "component_id": component_id,
            "id": cid,
            "name": name,
            "description": description,
            "configuration": copy.deepcopy(configuration),
            "rows": [],
            "isDisabled": bool(is_disabled),
            "state": "live",
            "version": revived_version,
        }
        self.calls.append(("create_config", cid))
        self._fire("create_config", "after", cid)
        return self._response(self.configs[cid])

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
        **kw: Any,
    ) -> dict[str, Any]:
        self._fire("update_config", "before", str(config_id))
        self.update_kwargs.append(dict(kw))
        cfg = self.configs.get(str(config_id))
        if cfg is None or cfg["state"] != "live":
            raise not_found(f"config {config_id}")
        expected = kw.get("expected_version")
        if expected is not None and expected != cfg["version"]:
            raise precondition_failed(str(config_id), cfg["version"], expected)
        if configuration is not None:
            cfg["configuration"] = copy.deepcopy(configuration)
        if name is not None:
            cfg["name"] = name
        if description is not None:
            cfg["description"] = description
        if is_disabled is not None:
            cfg["isDisabled"] = bool(is_disabled)
        cfg["version"] += 1
        self.calls.append(("update_config", str(config_id)))
        self._fire("update_config", "after", str(config_id))
        return self._response(cfg)

    def delete_config(
        self, component_id: str, config_id: str, branch_id: int | None = None, **kw: Any
    ) -> None:
        self._fire("delete_config", "before", str(config_id))
        cfg = self.configs.get(str(config_id))
        if cfg is None or cfg["state"] == "purged":
            raise not_found(f"config {config_id}")
        if cfg["state"] == "trashed" and branch_id not in (None, BRANCHES[0]["id"]):
            raise KeboolaApiError(
                message="Deleting configuration from trash is not allowed in development branches",
                status_code=400,
                error_code=ErrorCode.API_ERROR,
            )
        expected = kw.get("expected_version")
        if expected is not None and expected != cfg["version"]:
            raise precondition_failed(str(config_id), cfg["version"], expected)
        if cfg["state"] == "live":
            cfg["state"] = "trashed"
            cfg["version"] += 1
        else:
            cfg["state"] = "purged"
            self.purged.append(str(config_id))
        self.calls.append(("delete_config", str(config_id)))
        self._fire("delete_config", "after", str(config_id))

    def create_config_row(
        self,
        component_id: str,
        config_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        is_disabled: bool = False,
        branch_id: int | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        parent = self.configs.get(str(config_id))
        if parent is None or parent["state"] != "live":
            raise not_found(f"config {config_id}")
        wanted = kw.get("row_id")
        if wanted is not None and any(r["id"] == str(wanted) for r in parent["rows"]):
            raise KeboolaApiError(
                message="rowAlreadyExists", status_code=400, error_code=ErrorCode.API_ERROR
            )
        if wanted is not None:
            rid = str(wanted)
        else:
            rid = str(self._next_row)
            self._next_row += 1
        parent["version"] += 1
        row = {
            "id": rid,
            "name": name,
            "description": description,
            "configuration": copy.deepcopy(configuration),
        }
        parent["rows"].append(row)
        self.calls.append(("create_config_row", rid))
        self._fire("create_config_row", "after", rid)
        return {"id": rid} if self.partial_responses else copy.deepcopy(row)

    def get_config_row(
        self, component_id: str, config_id: str, row_id: str, branch_id: int | None = None
    ) -> dict[str, Any]:
        for row in self.configs[str(config_id)]["rows"]:
            if row["id"] == str(row_id):
                return copy.deepcopy(row)
        raise not_found(f"row {row_id}")

    def update_config_row(self, component_id: str, config_id: str, row_id: str, **kw: Any) -> Any:
        for row in self.configs[str(config_id)]["rows"]:
            if row["id"] == str(row_id):
                if kw.get("configuration") is not None:
                    row["configuration"] = copy.deepcopy(kw["configuration"])
                self.configs[str(config_id)]["version"] += 1
                return copy.deepcopy(row)
        raise not_found(f"row {row_id}")

    def delete_config_row(self, component_id: str, config_id: str, row_id: str, **_: Any) -> None:
        self.calls.append(("delete_config_row", str(row_id)))
        rows = self.configs[str(config_id)]["rows"]
        self.configs[str(config_id)]["rows"] = [r for r in rows if r["id"] != str(row_id)]
        self.configs[str(config_id)]["version"] += 1


def client_for(api: FakeStorage) -> MagicMock:
    client = MagicMock(wraps=api)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class Workspace:
    """A real working tree + a real SyncService wired to one FakeStorage.

    *push_force* makes every :meth:`push` a ``sync push --force``: the replay
    tests run each scenario both ways.
    """

    def __init__(
        self, tmp_config_dir: Path, root: Path, api: FakeStorage, *, push_force: bool = False
    ) -> None:
        self.root = root
        self.api = api
        self.push_force = push_force
        root.mkdir(parents=True, exist_ok=True)
        self.store = setup_single_project(tmp_config_dir)

    def service(self) -> SyncService:
        return SyncService(
            config_store=self.store, client_factory=lambda url, token: client_for(self.api)
        )

    # ------------------------------------------------------------ commands
    def init_and_pull(self) -> dict[str, Any]:
        self.service().init_sync(alias="prod", project_root=self.root)
        return self.pull()

    def pull(self, **kw: Any) -> dict[str, Any]:
        return self.service().pull(
            alias="prod", project_root=self.root, no_storage=True, no_jobs=True, **kw
        )

    def push(self) -> dict[str, Any]:
        return self.service().push(alias="prod", project_root=self.root, force=self.push_force)

    def diff(self) -> dict[str, Any]:
        return self.service().diff(alias="prod", project_root=self.root)

    def change_of(self, config_id: str) -> str:
        """The change_type ``sync diff`` reports for a config ('unchanged' if absent)."""
        for change in self.diff().get("changes", []):
            if change.get("config_id") == config_id and not change.get("is_row"):
                return change["change_type"]
        return "unchanged"

    # ------------------------------------------------------------ tree access
    def manifest(self) -> Any:
        return load_manifest(self.root)

    def entry(self, cid: str) -> Any | None:
        return next((c for c in self.manifest().configurations if c.id == cid), None)

    def dir_of(self, cid: str) -> Path:
        """Dir of a config, located by the manifest entry's path."""
        e = self.entry(cid)
        assert e is not None, f"no manifest entry for {cid}"
        return self.root / "main" / e.path

    def config_dirs(self) -> list[Path]:
        return sorted(p.parent for p in self.root.rglob(CONFIG_FILENAME) if "rows" not in p.parts)

    def read_yaml(self, d: Path) -> dict[str, Any]:
        return yaml.safe_load((d / CONFIG_FILENAME).read_text(encoding="utf-8"))

    def write_yaml(self, d: Path, data: dict[str, Any]) -> None:
        (d / CONFIG_FILENAME).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def edit_description(self, d: Path, text: str) -> None:
        """A local edit of _config.yml (the human in the editor)."""
        data = self.read_yaml(d)
        data["description"] = text
        self.write_yaml(d, data)

    def edit_sql(self, d: Path, sql: str) -> None:
        """A local edit of the companion file transform.sql only."""
        (d / "transform.sql").write_text(sql, encoding="utf-8")

    def new_local_config(self, template: Path, name: str) -> Path:
        """A brand-new local config dir (no id), shaped like a pulled one."""
        target = template.parent / name.lower().replace(" ", "-")
        shutil.copytree(template, target)
        data = self.read_yaml(target)
        data["name"] = name
        data.get("_keboola", {}).pop("config_id", None)
        self.write_yaml(target, data)
        return target
