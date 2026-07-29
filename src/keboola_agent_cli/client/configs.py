"""Components, configurations, config rows and their metadata.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import json
from typing import Any
from urllib.parse import quote

from ._core import _CoreClient


class _ConfigsMixin(_CoreClient):
    """Components, configurations, config rows and their metadata."""

    def list_components(
        self,
        component_type: str | None = None,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List components with their configurations.

        Args:
            component_type: Optional filter (extractor, writer, transformation, application).
            branch_id: If set, list components from a specific dev branch.

        Returns:
            List of component dicts from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        params: dict[str, str] = {"include": "configuration"}
        if component_type:
            params["componentType"] = component_type

        response = self._request("GET", f"{prefix}/components", params=params)
        return response.json()

    def list_components_with_configs(
        self,
        branch_id: int | None = None,
        component_type: str | None = None,
        include_state: bool = False,
    ) -> list[dict[str, Any]]:
        """List all components with full configuration bodies and rows.

        Makes a single API call to fetch everything needed for sync pull and
        for deep search (row-level configuration). Uses the
        include=configuration,rows parameter to get full config bodies and
        config rows in one request. When ``include_state`` is True, the
        response also embeds each configuration's runtime ``state`` dict
        (same data as ``get_config_state``) so bulk-state retrieval stays a
        single request instead of N+1. Also used by the bulk-detail caller
        in ``ConfigService`` when ``--with-state`` is set on
        ``config detail`` without a specific ``--config-id``.

        Args:
            branch_id: If set, target a specific dev branch.
            component_type: Optional filter (extractor, writer, transformation,
                application). Passed to the API as ``componentType``.
            include_state: When True, adds ``state`` to the ``include``
                resource list so each returned configuration carries its
                runtime state dict.

        Returns:
            List of component dicts, each containing a 'configurations' list
            with full config bodies and nested 'rows'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        include_parts = ["configuration", "rows"]
        if include_state:
            include_parts.append("state")
        params: dict[str, str] = {"include": ",".join(include_parts)}
        if component_type:
            params["componentType"] = component_type
        resp = self._request(
            "GET",
            f"{prefix}/components",
            params=params,
        )
        return resp.json()

    def list_component_configs(
        self,
        component_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all configurations for a specific component.

        Args:
            component_id: Component identifier (e.g. 'keboola.sandboxes').
            branch_id: If set, target a specific dev branch.

        Returns:
            List of configuration dicts (id, name, description, etc.).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id, safe='')}/configs",
        )
        return resp.json()

    def list_config_rows(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List all rows for a specific configuration.

        Args:
            component_id: Component identifier (e.g. 'keboola.ex-http').
            config_id: Configuration ID.
            branch_id: If set, target a specific dev branch.

        Returns:
            List of config row dicts.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows",
        )
        return resp.json()

    def get_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get a single configuration row by ID.

        Args:
            component_id: Component identifier.
            config_id: Configuration ID.
            row_id: Row ID.
            branch_id: If set, target a specific dev branch.

        Returns:
            Row detail dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        resp = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
        )
        return resp.json()

    def get_config_detail(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.
            branch_id: If set, get detail from a specific dev branch.

        Returns:
            Configuration detail dict from the API.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"{prefix}/components/{safe_component_id}/configs/{safe_config_id}",
        )
        return response.json()

    def get_config_state(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get the runtime state dict of a specific configuration.

        Convenience wrapper over
        ``get_config_detail(...).get("state", {})``: Storage API does not
        expose a standalone ``GET .../state`` resource (production returns
        404, branch-scoped returns 501 Not Implemented), so the state is
        only served inline as a field inside the configuration detail
        response. This wrapper is retained for API discoverability, but
        callers that already have a detail response should read ``state``
        from it directly instead of issuing this second identical request
        -- the service layer's single-mode ``--with-state`` does exactly
        that (see ``ConfigService.get_config_detail``).

        For bulk state retrieval across many configs, prefer the
        ``include=state`` query param on
        ``list_components_with_configs(include="configuration,rows,state")``
        -- one request serves every config's state instead of N requests.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.
            branch_id: If set, fetch state from a specific dev branch.

        Returns:
            The state dict (empty ``{}`` when the config has no saved state).
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"{prefix}/components/{safe_component_id}/configs/{safe_config_id}",
        )
        body = response.json()
        state = body.get("state")
        return state if isinstance(state, dict) else {}

    def list_config_folder_metadata(self, branch_id: int) -> dict[str, str]:
        """Fetch folder names for all configurations via metadata search.

        Uses the search/component-configurations endpoint to find configs
        with ``KBC.configuration.folderName`` metadata.

        Note: This endpoint requires a branch ID (branch-only route).

        Args:
            branch_id: Branch ID (required — use default branch for production).

        Returns:
            Dict mapping ``"{component_id}/{config_id}"`` to folder name.
        """
        prefix = f"/v2/storage/branch/{branch_id}"
        resp = self._request(
            "GET",
            f"{prefix}/search/component-configurations",
            params={
                "metadataKeys[]": "KBC.configuration.folderName",
                "include": "filteredMetadata",
            },
        )
        folder_map: dict[str, str] = {}
        for item in resp.json():
            comp_id = item.get("idComponent", "")
            config_id = str(item.get("configurationId", ""))
            meta = next(
                (m for m in item.get("metadata", []) if m["key"] == "KBC.configuration.folderName"),
                None,
            )
            if meta:
                folder_map[f"{comp_id}/{config_id}"] = meta["value"]
        return folder_map

    def list_config_metadata(
        self,
        component_id: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List metadata entries on a configuration.

        GET /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "GET",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata",
        )
        return response.json()

    def set_config_metadata(
        self,
        component_id: str,
        config_id: str,
        entries: list[tuple[str, str]],
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Bulk-set metadata key/value pairs on a configuration.

        POST /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata
        Same PHP-style indexed form as set_branch_metadata.
        """
        form: dict[str, str] = {}
        for i, (key, value) in enumerate(entries):
            form[f"metadata[{i}][key]"] = key
            form[f"metadata[{i}][value]"] = value
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata",
            data=form,
        )
        return response.json()

    def delete_config_metadata(
        self,
        component_id: str,
        config_id: str,
        metadata_id: int | str,
        branch_id: int | None = None,
    ) -> None:
        """Delete a single metadata entry on a configuration by its numeric ID.

        DELETE /v2/storage/[branch/{b}/]components/{c}/configs/{id}/metadata/{mid}
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{quote(component_id, safe='')}/configs/{quote(config_id, safe='')}/metadata/{metadata_id}",
        )

    def create_config(
        self,
        component_id: str,
        name: str,
        configuration: dict[str, Any],
        description: str = "",
        branch_id: int | None = None,
        is_disabled: bool = False,
    ) -> dict[str, Any]:
        """Create a new configuration for a component.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs

        Args:
            component_id: Component identifier.
            name: Configuration name.
            configuration: Configuration body (parameters, storage, etc.).
            description: Optional description.
            branch_id: If set, target a specific dev branch.
            is_disabled: When True, the configuration is created in disabled
                state (mirrors ``create_config_row``).

        Returns:
            Created configuration dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {
            "name": name,
            "description": description,
            "configuration": json.dumps(configuration),
        }
        if is_disabled:
            data["isDisabled"] = "1"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs",
            data=data,
        )
        return resp.json()

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
        """Update an existing configuration.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}

        Only provided (non-None) fields are sent in the request.
        ``is_disabled=None`` leaves the remote enabled/disabled state
        untouched (mirrors ``update_config_row``).

        Returns:
            Updated configuration dict.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if configuration is not None:
            data["configuration"] = json.dumps(configuration)
        if is_disabled is not None:
            data["isDisabled"] = "1" if is_disabled else "0"
        if change_description:
            data["changeDescription"] = change_description
        resp = self._request(
            "PUT",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}",
            data=data,
        )
        return resp.json()

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
        """Create a new configuration row.

        POST /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows

        Args:
            component_id: The component ID.
            config_id: The parent configuration ID.
            name: Row name.
            configuration: Row-level configuration dict.
            description: Optional row description.
            is_disabled: When True, the row is created in disabled state and
                excluded from job runs until re-enabled.
            branch_id: Optional dev branch ID.

        Returns:
            Created row dict including the assigned 'id'.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {
            "name": name,
            "description": description,
            "configuration": json.dumps(configuration),
        }
        if is_disabled:
            data["isDisabled"] = "1"
        resp = self._request(
            "POST",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows",
            data=data,
        )
        return resp.json()

    def update_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        name: str | None = None,
        configuration: dict[str, Any] | None = None,
        description: str | None = None,
        is_disabled: bool | None = None,
        change_description: str = "",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Update an existing configuration row.

        PUT /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows/{row_id}

        Args:
            is_disabled: When True, disable the row; when False, enable it;
                when None, leave the current state unchanged.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if configuration is not None:
            data["configuration"] = json.dumps(configuration)
        if is_disabled is not None:
            data["isDisabled"] = "1" if is_disabled else "0"
        if change_description:
            data["changeDescription"] = change_description
        resp = self._request(
            "PUT",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
            data=data,
        )
        return resp.json()

    def delete_config_row(
        self,
        component_id: str,
        config_id: str,
        row_id: str,
        branch_id: int | None = None,
    ) -> None:
        """Delete a configuration row.

        DELETE /v2/storage/[branch/{id}/]components/{comp_id}/configs/{config_id}/rows/{row_id}
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{quote(component_id)}/configs/{quote(config_id)}/rows/{quote(row_id)}",
        )

    def delete_config(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> None:
        """Delete a component configuration.

        Args:
            component_id: Component ID.
            config_id: Configuration ID.
            branch_id: Branch ID. If provided, deletes config in that branch.
        """
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request(
            "DELETE",
            f"{prefix}/components/{safe_component}/configs/{safe_config}",
        )
