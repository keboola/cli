"""``kbagent config set-folder``: the UI folder of a configuration.

Bound onto ConfigService as ``set_config_folder``. It lives here rather than on
the service because ``config_service.py`` sits at its HARD file-size ceiling
(see ``_config_clone``). ``self`` is a ConfigService.
"""

from __future__ import annotations

from typing import Any

from ..constants import FOLDER_METADATA_KEY


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
    result keeps the keys it had when it wrote that key: ``key`` is frozen,
    ``value`` is the folder as stored ('' when cleared), ``result`` is a
    subset of the PUT response.
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
    # The API trims the name and stores an empty one as "no folder" (null).
    folder = response.get("folder", folder_name.strip()) or ""
    target = f"config '{component_id}/{config_id}' in project '{alias}'"
    return {
        "project_alias": alias,
        "component_id": component_id,
        "config_id": config_id,
        "branch_id": effective_branch_id,
        "key": FOLDER_METADATA_KEY,
        "value": folder,
        "result": {
            "id": response.get("id", config_id),
            "name": response.get("name"),
            "folder": response.get("folder"),
        },
        "folder": folder,
        "message": f"Folder '{folder}' set on {target}."
        if folder
        else f"Folder cleared on {target}.",
    }
