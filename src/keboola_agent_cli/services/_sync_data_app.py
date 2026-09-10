"""Data-app create for the sync engine (CLI-8).

``sync push`` / ``sync clone`` create configs through the Storage API only.
A ``keboola.data-apps`` config has a second half, the Data Science ``/apps``
deployment record, and the runtime type (``python-js`` / ``streamlit`` / ...)
lives ONLY on that record. This module owns the one create path that carries
the type, kept out of ``data_app_service`` so that already-large module does
not grow past its file-size budget.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client import KeboolaClient
from ..data_science_client import DataScienceClient
from ..errors import ErrorCode, KeboolaApiError
from .data_app_service import DATA_APP_COMPONENT_ID

logger = logging.getLogger(__name__)


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
    new app's ``parameters.id``.

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
