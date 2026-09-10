"""Data-app create for the sync engine (CLI-8).

``sync push`` / ``sync clone`` create configs through the Storage API only.
A ``keboola.data-apps`` config has a second half, the Data Science ``/apps``
deployment record, and the runtime type (``python-js`` / ``streamlit`` / ...)
lives ONLY on that record. This module owns the one create path that carries
the type, kept out of ``data_app_service`` so that already-large module does
not grow past its file-size budget.
"""

from __future__ import annotations

import copy
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
    via ``update_config``. The cloned body still points ``parameters.id`` at
    the SOURCE project's app, so the create call drops that id and the update
    call writes the new app's id.

    If ``update_config`` fails after ``create_app`` already created the record,
    the record is deleted, so a failed sync create leaves no orphan app in the
    target (the same guard as ``DataAppService.create``).

    Returns the ``update_config`` response (the Storage config, whose ``id`` is
    the new config ULID) so the caller's manifest writeback is identical to the
    ``create_config`` path.
    """
    # The cloned body still points parameters.id at the source project's app.
    # Drop that stale id on create; the update call writes the correct one.
    create_body = copy.deepcopy(configuration)
    create_params = create_body.get("parameters")
    if isinstance(create_params, dict):
        create_params.pop("id", None)

    shell = ds_client.create_app(
        type_=type_,
        name=name,
        description="",  # full description goes onto the Storage config below
        config=create_body,
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

    params = configuration.setdefault("parameters", {})
    if isinstance(params, dict):
        params["id"] = app_id

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
