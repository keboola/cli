"""Workspaces / sandboxes and their config-backed provisioning.

Extracted verbatim from the former single-file ``client.py`` (issue #520).
"""

import json
from typing import Any
from urllib.parse import quote

from ._core import _CoreClient


class _WorkspacesMixin(_CoreClient):
    """Workspaces / sandboxes and their config-backed provisioning."""

    # --- Workspace CRUD ---

    def list_workspaces(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        """List all workspaces in the project."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("GET", f"{prefix}/workspaces")
        return response.json()

    def get_workspace(self, workspace_id: int, branch_id: int | None = None) -> dict[str, Any]:
        """Get workspace details (note: password is NOT included)."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("GET", f"{prefix}/workspaces/{workspace_id}")
        return response.json()

    def delete_workspace(self, workspace_id: int, branch_id: int | None = None) -> None:
        """Delete a workspace (synchronous)."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        self._request("DELETE", f"{prefix}/workspaces/{workspace_id}")

    def reset_workspace_password(
        self, workspace_id: int, branch_id: int | None = None
    ) -> dict[str, Any]:
        """Reset workspace password. Returns new password."""
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request("POST", f"{prefix}/workspaces/{workspace_id}/password")
        return response.json()

    def create_sandbox_config(
        self,
        name: str,
        description: str = "",
        backend_size: str = "small",
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a keboola.sandboxes configuration.

        This is needed to make workspaces visible in the Keboola UI.
        The UI only shows workspaces tied to a sandboxes config.

        Args:
            name: Human-readable name for the workspace.
            description: Optional description.
            backend_size: Backend size (small, medium, large).
            branch_id: Branch ID. If provided, creates config in that branch.

        Returns:
            Configuration dict with id, name, etc.
        """
        config = {
            "parameters": {
                "runtime": {"shared": False},
                "storage": {"input": {"tables": []}, "output": {"tables": []}},
                "parameters": {"id": "", "blocks": []},
                "backendSize": backend_size,
            },
            "runtime": {"shared": False},
        }
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        response = self._request(
            "POST",
            f"{prefix}/components/keboola.sandboxes/configs",
            data={
                "name": name,
                "description": description,
                "configuration": json.dumps(config),
            },
        )
        return response.json()

    def create_config_workspace(
        self,
        branch_id: int,
        component_id: str,
        config_id: str,
        backend: str = "snowflake",
        login_type: str | None = None,
        public_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a workspace tied to a specific configuration.

        Args:
            branch_id: Branch ID (use main branch ID for production).
            component_id: Component ID (e.g. keboola.snowflake-transformation).
            config_id: Configuration ID.
            backend: Workspace backend.
            login_type: Optional Storage API loginType. Omitted when None.
            public_key: Optional public key for key-pair workspaces. Omitted when None.

        Returns:
            Workspace dict including connection credentials.
        """
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        payload: dict[str, Any] = {"backend": backend}
        if login_type is not None:
            payload["loginType"] = login_type
        if public_key is not None:
            payload["publicKey"] = public_key

        response = self._request(
            "POST",
            f"/v2/storage/branch/{branch_id}/components/{safe_component}/configs/{safe_config}/workspaces",
            json=payload,
        )
        return response.json()

    def list_config_workspaces(
        self,
        branch_id: int,
        component_id: str,
        config_id: str,
    ) -> list[dict[str, Any]]:
        """List workspaces tied to a specific configuration."""
        safe_component = quote(component_id, safe="")
        safe_config = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"/v2/storage/branch/{branch_id}/components/{safe_component}/configs/{safe_config}/workspaces",
        )
        return response.json()

    def load_workspace_tables(
        self,
        workspace_id: int,
        tables: list[dict[str, Any]],
        branch_id: int | None = None,
        preserve: bool = False,
        max_wait: float | None = None,
    ) -> dict[str, Any]:
        """Load tables into a workspace (async operation).

        Args:
            workspace_id: Target workspace ID.
            tables: List of table load definitions, each with at minimum:
                - source: table ID (e.g. "in.c-bucket.table")
                - destination: target table name in workspace
                Optionally also ``loadType`` ("COPY" / "CLONE" / "VIEW" --
                uppercase; the request validator matches the uppercase enum).
                Omitting it means COPY. Items are forwarded verbatim.
            branch_id: Branch ID. Required for workspaces on dev branches.
            preserve: If True, keep existing tables in the workspace. Default is False
                (workspace is cleared before loading).
            max_wait: Seconds to poll the load job before giving up. ``None``
                falls back to the shared STORAGE_JOB_MAX_WAIT; the service
                layer passes WORKSPACE_LOAD_JOB_MAX_WAIT, because a real COPY
                routinely outlives the 60s default.

        Returns:
            Completed storage job dict (polls until done).

        Raises:
            KeboolaApiError: If the load job fails or times out.
        """
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        body: dict[str, Any] = {"input": tables, "preserve": preserve}
        response = self._request(
            "POST",
            f"{prefix}/workspaces/{workspace_id}/load",
            json=body,
        )
        return self._wait_for_storage_job(response.json(), max_wait=max_wait)
