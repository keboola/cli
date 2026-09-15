"""Data-app runtime type for the sync engine (CLI-8).

A ``keboola.data-apps`` config has a second half, the Data Science ``/apps``
deployment record, and the runtime type (``python-js`` / ``streamlit`` / ...)
lives ONLY on that record, never in the Storage config body. This module owns
the type on both sides of sync: ``load_data_app_types`` / ``resolve_pull_type``
read it on pull, and ``create_synced_data_app`` sends it on push. Kept out of
``sync_service`` and ``data_app_service`` so neither grows past its file-size
budget.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..client import KeboolaClient
from ..data_science_client import DataScienceClient
from ..errors import ErrorCode, KeboolaApiError
from .data_app_service import DATA_APP_COMPONENT_ID

logger = logging.getLogger(__name__)


def load_data_app_types(
    ds_client_factory: Callable[[str, str], DataScienceClient],
    project: Any,
    components: list[dict[str, Any]],
) -> dict[str, str]:
    """Return ``{config_id: runtime type}`` from the DS ``/apps`` list.

    Data-app records only -- the list also carries sandbox/workspace records
    whose id can collide with an unrelated config. Empty when the tree has no
    data apps, or when the lookup fails (logged, not raised): the caller then
    keeps whatever type is already on disk.
    """
    if not any(comp.get("id") == DATA_APP_COMPONENT_ID for comp in components):
        return {}
    try:
        ds_client = ds_client_factory(project.stack_url, project.token)
        with ds_client:
            return {
                str(app.get("configId")): str(app.get("type"))
                for app in ds_client.list_apps()
                if app.get("componentId") == DATA_APP_COMPONENT_ID
                and app.get("configId")
                and app.get("type")
            }
    except Exception:
        logger.warning("Failed to fetch data-app types from Data Science API", exc_info=True)
        return {}


def resolve_pull_type(
    read_config_file: Callable[[Path], dict[str, Any] | None],
    config_dir: Path,
    component_id: str,
    data_app_types: dict[str, str],
    config_id: str,
) -> tuple[str | None, str | None]:
    """Return ``(da_type, on_disk_da_type)`` for a config on pull.

    Both are ``None`` for a non-data-app. For a data app, the DS list is the
    source of truth ONLY for the configs it names: a config the list omits (the
    call failed, the response left it out, or the DS record is gone while the
    Storage config remains) keeps its on-disk type. The type therefore changes
    only when the list reports a different one; an absence never strips it.
    ``config_hash`` ignores ``_keboola``, so the caller compares the two values
    to decide whether to rewrite an otherwise-unchanged config.
    """
    if component_id != DATA_APP_COMPONENT_ID:
        return None, None
    existing = read_config_file(config_dir)
    on_disk = (existing.get("_keboola") or {}).get("data_app_type") if existing else None
    return data_app_types.get(config_id, on_disk), on_disk


def type_needs_rewrite(component_id: str, da_type: str | None, on_disk_da_type: str | None) -> bool:
    """True when a data app's resolved type differs from the on-disk one.

    The type lives in ``_keboola``, which ``config_hash`` ignores, so a config
    with an otherwise-unchanged body must still be rewritten to record it.
    """
    return component_id == DATA_APP_COMPONENT_ID and da_type != on_disk_da_type


def create_synced_data_app(
    storage_client: KeboolaClient,
    ds_client: DataScienceClient,
    *,
    name: str,
    description: str,
    type_: str,
    configuration: dict[str, Any],
    branch_id: int | None,
    is_disabled: bool = False,
) -> dict[str, Any]:
    """Create a ``keboola.data-apps`` config together with its Data Science
    deployment record, carrying the runtime ``type_`` (CLI-8).

    ``sync push`` / ``sync clone`` create configs through the Storage API only
    (``create_config``). A ``keboola.data-apps`` config has a second half, the
    Data Science ``/apps`` deployment record, and the runtime type
    (``python-js`` / ``streamlit`` / ...) lives ONLY on that record, never in
    the Storage config body. A plain ``create_config`` therefore leaves the
    platform to create the DS record under its default type, so a cloned
    ``python-js`` app deploys as ``streamlit``.

    This routes creation through ``create_app`` (which creates BOTH the DS
    record with ``type_`` and its Storage config), then fills the full body
    via ``update_config``. ``POST /apps`` validates its ``config``: it wants
    the create-shell shape (``parameters.size`` / ``autoSuspendAfterSeconds``
    / ``dataApp.slug`` + ``authorization``), not the full Storage body, which
    carries ``runtime.backend.size`` instead of ``parameters.size``. So the
    create call sends the minimal shell (the same shape as
    ``DataAppService.create``) and the update call sends the full body with the
    new app's ``parameters.id``. This mutates the passed ``configuration``:
    ``parameters.id`` is set to the new app id, so the caller can persist it to
    the local file (else the next push reverts the back-pointer).

    If ``update_config`` fails after ``create_app`` already created the record,
    the record is deleted, so a failed sync create leaves no orphan app in the
    target (the same guard as ``DataAppService.create``).

    Returns the ``update_config`` response (the Storage config, whose ``id`` is
    the new config ULID) so the caller's manifest writeback is identical to the
    ``create_config`` path.
    """
    # Build the minimal shell POST /apps accepts (see docstring). The full
    # Storage body -- runtime.backend.size, the git block, the source app id --
    # goes on the update_config call below, not here.
    params = configuration.get("parameters") or {}
    data_app = params.get("dataApp") or {}
    backend = (configuration.get("runtime") or {}).get("backend") or {}
    initial_parameters: dict[str, Any] = {"dataApp": {"slug": data_app.get("slug", "")}}
    if "size" in backend:
        initial_parameters["size"] = backend["size"]
    if "autoSuspendAfterSeconds" in params:
        initial_parameters["autoSuspendAfterSeconds"] = params["autoSuspendAfterSeconds"]
    initial_config: dict[str, Any] = {"parameters": initial_parameters}
    if "authorization" in configuration:
        initial_config["authorization"] = configuration["authorization"]

    shell = ds_client.create_app(
        type_=type_,
        name=name,
        description="",  # full description goes onto the Storage config below
        config=initial_config,
        branch_id=branch_id,
    )
    app_id = str(shell.get("id", ""))
    config_id = str(shell.get("configId", ""))
    if not app_id or not config_id:
        # An id without a configId still leaves a shell behind. Delete it so a
        # failed create leaves no orphan app, as the docstring promises.
        if app_id:
            try:
                ds_client.delete_app(app_id)
            except Exception:
                logger.warning(
                    "Failed to delete orphan data app %s after an incomplete create response",
                    app_id,
                )
        raise KeboolaApiError(
            message="POST /apps response missing id or configId",
            status_code=500,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )

    target_params = configuration.setdefault("parameters", {})
    if isinstance(target_params, dict):
        target_params["id"] = app_id

    try:
        return storage_client.update_config(
            component_id=DATA_APP_COMPONENT_ID,
            config_id=config_id,
            name=name,
            description=description,
            configuration=configuration,
            change_description="Created via kbagent sync",
            branch_id=branch_id,
            is_disabled=is_disabled,
        )
    except Exception:
        # The DS record and its bare Storage config exist, but the full body
        # did not land. Delete the record so the target keeps no orphan app.
        try:
            ds_client.delete_app(app_id)
        except Exception:
            logger.warning("Failed to delete orphan data app %s after a failed sync create", app_id)
        raise
