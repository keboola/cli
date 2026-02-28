"""Keboola Manage API client with retry, timeouts, and token masking.

This module communicates with the Keboola Manage API for organization-level
operations like listing projects and creating Storage API tokens.
Uses a different auth header (X-KBC-ManageApiToken) than the Storage API client.

Inherits shared retry/error logic from BaseHttpClient.
"""

from typing import Any

import httpx

from . import __version__
from .constants import DEFAULT_TIMEOUT
from .http_base import BaseHttpClient


class ManageClient(BaseHttpClient):
    """HTTP client for the Keboola Manage API.

    Provides methods to list organization projects and create Storage API
    tokens, with built-in retry logic (exponential backoff for 429/5xx),
    timeouts, and automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

    def __init__(self, stack_url: str, manage_token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        headers = {
            "X-KBC-ManageApiToken": manage_token,
            "User-Agent": f"keboola-agent-cli/{__version__}",
        }
        super().__init__(
            base_url=self._stack_url,
            token=manage_token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    def __enter__(self) -> "ManageClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def list_organization_projects(self, org_id: int) -> list[dict[str, Any]]:
        """List all projects in an organization.

        Args:
            org_id: The organization ID.

        Returns:
            List of project dicts with at least 'id' and 'name' fields.

        Raises:
            KeboolaApiError: On API errors.
        """
        response = self._do_request("GET", f"/manage/organizations/{org_id}/projects")
        return response.json()

    def create_project_token(
        self,
        project_id: int,
        description: str,
        can_manage_buckets: bool = True,
        can_read_all_file_uploads: bool = True,
    ) -> dict[str, Any]:
        """Create a new Storage API token for a project.

        Args:
            project_id: The project ID.
            description: Token description.
            can_manage_buckets: Whether the token can manage buckets.
            can_read_all_file_uploads: Whether the token can read all file uploads.

        Returns:
            Token dict including the 'token' field (shown only once).

        Raises:
            KeboolaApiError: On API errors.
        """
        payload = {
            "description": description,
            "canManageBuckets": can_manage_buckets,
            "canReadAllFileUploads": can_read_all_file_uploads,
        }
        response = self._do_request(
            "POST", f"/manage/projects/{project_id}/tokens", json=payload
        )
        return response.json()
