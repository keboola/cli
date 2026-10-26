"""``kbagent config set-folder``: the UI folder of a configuration.

Bound onto ConfigService as ``set_config_folder``. It lives here rather than on
the service because ``config_service.py`` sits at its HARD file-size ceiling
(see ``_config_clone``). ``self`` is a ConfigService.
"""

from __future__ import annotations

from typing import Any

from ..constants import FOLDER_METADATA_KEY
from ..errors import FeatureNotEnabledError

# Shared by set-folder and the sync push warning: a stack without the field
# accepts and ignores the ``folder`` param, so the response is the only probe.
FOLDER_NOT_SUPPORTED_MESSAGE = (
    "The Storage API on this stack does not support the configuration folder field "
    "yet (its response carries no 'folder'), so the folder was not changed."
)


def set_config_folder_method(
    self,
    alias: str,
    component_id: str,
    config_id: str,
    folder_name: str,
    branch_id: int | None = None,
) -> dict[str, Any]:
    """Set the UI folder of a configuration; an empty name clears it.

    Writes the configuration's ``folder`` field (PUT, creates no version),
    never the ``KBC.configuration.folderName`` metadata key (DMD-2348). The
    result keeps the keys and shapes it had when it wrote that key: ``key``
    is frozen, ``value`` is the folder as stored ('' when cleared), and
    ``result`` stays a one-entry list of ``{key, value}`` (it was the metadata
    POST response). The PUT response subset is the new ``configuration``.

    Raises:
        FeatureNotEnabledError: the PUT response has no ``folder`` key. A stack
            without the field ignores the param, so reporting success there
            would be false. No metadata fallback: that is the write of the
            key this command exists to stop.
    """
    project = self.resolve_projects([alias])[alias]
    client = self._client_factory(project.stack_url, project.token)
    try:
        effective_branch_id = self._metadata_branch_id(alias, client, branch_id)
        response = client.update_config(
            component_id=component_id,
            config_id=config_id,
            folder=folder_name,
            branch_id=effective_branch_id,
        )
    finally:
        client.close()
    if "folder" not in response:
        raise FeatureNotEnabledError(
            f"{FOLDER_NOT_SUPPORTED_MESSAGE} Config '{component_id}/{config_id}' in project "
            f"'{alias}' is unchanged."
        )
    # The API trims the name and stores an empty one as "no folder" (null).
    folder = response["folder"] or ""
    target = f"config '{component_id}/{config_id}' in project '{alias}'"
    return {
        "project_alias": alias,
        "component_id": component_id,
        "config_id": config_id,
        "branch_id": effective_branch_id,
        "key": FOLDER_METADATA_KEY,
        "value": folder,
        "result": [{"key": FOLDER_METADATA_KEY, "value": folder}],
        "configuration": {
            "id": response.get("id", config_id),
            "name": response.get("name"),
            "folder": response.get("folder"),
        },
        "folder": folder,
        "message": f"Folder '{folder}' set on {target}."
        if folder
        else f"Folder cleared on {target}.",
    }
