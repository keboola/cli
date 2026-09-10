"""sync pull / push carry a data app's runtime type (CLI-8).

A ``keboola.data-apps`` config has two halves: the Storage config (body) and
the Data Science ``/apps`` deployment record. The runtime type (``python-js``
/ ``streamlit`` / ...) lives ONLY on the DS record, never in the Storage body.
So a plain pull drops it and a plain push (``create_config``) never sends it --
a cloned ``python-js`` app then deploys under the platform default
(``streamlit``).

pull stamps the type into the ``_keboola`` footer; push routes a data-app
CREATE through the DS ``create_app`` so the type travels.
"""

from pathlib import Path
from typing import Any, Self

import yaml

from helpers import setup_single_project
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.services.sync_service import SyncService
from test_sync_baseline_stamping import (
    FakeApi,
    _client_for,
    _config_file,
    _init_and_pull,
    _sql_components,
)

DATA_APP_COMPONENT = "keboola.data-apps"


class FakeDs:
    """Data Science double.

    ``create_app`` also appends the Storage config to the wrapped ``FakeApi``,
    exactly as the real ``POST /apps`` does -- otherwise the follow-up
    ``update_config`` could not resolve the server-assigned config id.
    """

    def __init__(self, api: FakeApi, list_result: list[dict[str, Any]] | None = None):
        self.api = api
        self.create_app_calls: list[dict[str, Any]] = []
        self._list_result = list_result or []
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    def list_apps(self) -> list[dict[str, Any]]:
        return self._list_result

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
        self.create_app_calls.append({"type_": type_, "name": name, "config": config})
        new_config_id = "cfg-da-new"
        new_app_id = "77777"
        record = {"id": new_config_id, "name": name, "configuration": config, "rows": []}
        for comp in self.api.components:
            if comp["id"] == DATA_APP_COMPONENT:
                comp["configurations"].append(record)
                break
        else:
            self.api.components.append(
                {"id": DATA_APP_COMPONENT, "type": "application", "configurations": [record]}
            )
        return {"id": new_app_id, "configId": new_config_id}


MYSQL_COMPONENT = "keboola.ex-db-mysql"


def _mixed_components(config_id: str = "cfg-da") -> list[dict[str, Any]]:
    """A data-app config and a NON-data-app config that shares its id.

    Config ids are unique only per component, so a data app and (say) a MySQL
    extractor can legally hold the same id. The MySQL config must never receive
    a data_app_type.
    """
    return [
        {
            "id": DATA_APP_COMPONENT,
            "type": "application",
            "configurations": [
                {
                    "id": config_id,
                    "name": "api-test",
                    "description": "A JS data app",
                    # parameters.id is the DS back-pointer; no runtime type here.
                    "configuration": {
                        "parameters": {"id": "99999", "dataApp": {"slug": "api-test"}}
                    },
                    "rows": [],
                }
            ],
        },
        {
            "id": MYSQL_COMPONENT,
            "type": "extractor",
            "configurations": [
                {
                    "id": config_id,  # same id, different component
                    "name": "mysql-ex",
                    "description": "",
                    "configuration": {"parameters": {"host": "db.example.com"}},
                    "rows": [],
                }
            ],
        },
    ]


def _service(store: Any, api: FakeApi, ds: Any) -> SyncService:
    # ``ds`` is a structural DataScienceClient double (FakeDs), typed Any so the
    # factory signature accepts it.
    return SyncService(
        config_store=store,
        client_factory=lambda url, token: _client_for(api),
        ds_client_factory=lambda url, token: ds,
    )


def _find_config(project_root: Path, component_id: str) -> dict[str, Any]:
    """Return the parsed _config.yml of the pulled config for *component_id*."""
    for path in project_root.rglob(CONFIG_FILENAME):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (data.get("_keboola") or {}).get("component_id") == component_id:
            return data
    raise AssertionError(f"no {component_id} _config.yml in the pulled tree")


def _author_data_app(project_root: Path, *, with_type: bool) -> None:
    """Write an untracked data-app _config.yml so push sees it as CREATE."""
    keboola: dict[str, Any] = {"component_id": DATA_APP_COMPONENT}
    if with_type:
        keboola["data_app_type"] = "python-js"
    new_dir = _config_file(project_root).parent.parent / "new-data-app"
    new_dir.mkdir(parents=True)
    (new_dir / CONFIG_FILENAME).write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": "api-test",
                "description": "A JS data app",
                "parameters": {"id": "99999", "dataApp": {"slug": "api-test"}},
                "_keboola": keboola,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ===================================================================
# pull: the DS runtime type lands in the local _config.yml
# ===================================================================


def test_pull_stamps_data_app_type(tmp_config_dir: Path, tmp_path: Path) -> None:
    """The data-app gets its type; a non-data-app sharing the id does not.

    The DS /apps list mixes real data apps with sandbox/workspace records that
    carry another component's id and a backend `type` (e.g. `snowflake`). The
    map must be built from data-app records only, and only data-app configs may
    be stamped -- these are the two live-observed failure modes (CLI-8).
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    api = FakeApi(_mixed_components("cfg-da"))
    ds = FakeDs(
        api,
        list_result=[
            {"configId": "cfg-da", "componentId": DATA_APP_COMPONENT, "type": "python-js"},
            # A sandbox record: same config id, another component, a backend type.
            # It must not overwrite the data-app's type nor reach the map.
            {"configId": "cfg-da", "componentId": MYSQL_COMPONENT, "type": "snowflake"},
        ],
    )
    store = setup_single_project(tmp_config_dir)

    service = _service(store, api, ds)
    service.init_sync(alias="prod", project_root=project_root)
    service.pull(alias="prod", project_root=project_root, no_storage=True, no_jobs=True)

    # The data app took its own type, not the sandbox record's `snowflake`.
    assert (
        _find_config(project_root, DATA_APP_COMPONENT)["_keboola"]["data_app_type"] == "python-js"
    )
    # The MySQL config -- same id -- was never stamped.
    assert "data_app_type" not in _find_config(project_root, MYSQL_COMPONENT)["_keboola"]


def test_pull_without_data_apps_never_calls_ds(tmp_config_dir: Path, tmp_path: Path) -> None:
    """No data-apps in the tree => the DS API is not touched at all."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))

    # A DS double that fails if used proves pull skips it when unneeded.
    class ExplodingDs(FakeDs):
        def list_apps(self) -> list[dict[str, Any]]:
            raise AssertionError("list_apps must not be called without data apps")

    store = setup_single_project(tmp_config_dir)
    service = _service(store, api, ExplodingDs(api))
    service.init_sync(alias="prod", project_root=project_root)
    service.pull(alias="prod", project_root=project_root, no_storage=True, no_jobs=True)


# ===================================================================
# push: a data-app CREATE routes through the DS client with the type
# ===================================================================


def test_push_create_data_app_sends_type(tmp_config_dir: Path, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _author_data_app(project_root, with_type=True)

    ds = FakeDs(api)
    result = _service(store, api, ds).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["created"] == 1
    # The type reached the DS record -- the fix.
    assert len(ds.create_app_calls) == 1
    assert ds.create_app_calls[0]["type_"] == "python-js"
    # The Storage body was filled and the stale back-pointer repointed at the
    # newly assigned app id.
    da_updates = [c for c in api.update_calls if c["component_id"] == DATA_APP_COMPONENT]
    assert da_updates, "expected an update_config for the data-app"
    assert da_updates[-1]["configuration"]["parameters"]["id"] == "77777"
    assert ds.closed is True


def test_push_create_data_app_without_type_falls_back(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A tree pulled before the fix (no recorded type) keeps the old behavior:
    a plain create_config, no DS record -- no regression, no wrong type sent."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _author_data_app(project_root, with_type=False)

    ds = FakeDs(api)
    result = _service(store, api, ds).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["created"] == 1
    # No type recorded => DS is never asked to create the app.
    assert ds.create_app_calls == []
    # It went through the plain Storage create instead (FakeApi mints "cfg-new").
    created = next(
        c
        for comp in api.components
        if comp["id"] == DATA_APP_COMPONENT
        for c in comp["configurations"]
    )
    assert created["id"] == "cfg-new"
