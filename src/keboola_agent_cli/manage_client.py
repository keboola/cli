"""Keboola Manage API client with retry, timeouts, and token masking.

This module communicates with the Keboola Manage API for organization-level
operations like listing projects and creating Storage API tokens.
Uses a different auth header (X-KBC-ManageApiToken) than the Storage API client.

Inherits shared retry/error logic from BaseHttpClient.
"""

from typing import Any

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

    def verify_token(self) -> dict[str, Any]:
        """Verify the manage token and return token/user metadata.

        Calls GET /manage/tokens/verify to retrieve information about
        the manage token owner, including user name and email.

        Returns:
            Dict with token info including 'user' block (id, name, email).

        Raises:
            KeboolaApiError: On API errors.
        """
        response = self._do_request("GET", "/manage/tokens/verify")
        return response.json()

    def get_project(self, project_id: int) -> dict[str, Any]:
        """Get project details by ID.

        Works with Personal Access Tokens (PAT) for projects where the
        token owner is a member -- does NOT require organization admin.

        Args:
            project_id: The project ID.

        Returns:
            Project dict with at least 'id', 'name', and 'organization' fields.

        Raises:
            KeboolaApiError: On API errors (e.g. 403 if not a member).
        """
        response = self._do_request("GET", f"/manage/projects/{project_id}")
        return response.json()

    def get_organization(self, org_id: int) -> dict[str, Any]:
        """Get organization details by ID.

        Used to resolve the organization name from its ID (e.g. during
        `org setup`, where only the org_id is known up front).

        Args:
            org_id: The organization ID.

        Returns:
            Organization dict with at least 'id' and 'name' fields.

        Raises:
            KeboolaApiError: On API errors (e.g. 403 if not an org member).
        """
        response = self._do_request("GET", f"/manage/organizations/{org_id}")
        return response.json()

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
        can_read_all_project_events: bool = True,
        can_manage_dev_branches: bool = True,
        can_manage_tokens: bool = True,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        """Create a new Storage API token for a project.

        Args:
            project_id: The project ID.
            description: Token description.
            can_manage_buckets: Whether the token can manage buckets.
            can_read_all_file_uploads: Whether the token can read all file uploads.
            can_read_all_project_events: Whether the token can read all project events.
            can_manage_dev_branches: Whether the token can manage development branches.
            can_manage_tokens: Whether the token can create/manage other tokens.
                Required for Scheduler (Orchestrator) to create run tokens.
            expires_in: Token lifetime in seconds. None means the token never expires.

        Returns:
            Token dict including the 'token' field (shown only once).

        Raises:
            KeboolaApiError: On API errors.
        """
        payload: dict[str, Any] = {
            "description": description,
            "canManageBuckets": can_manage_buckets,
            "canReadAllFileUploads": can_read_all_file_uploads,
            "canReadAllProjectEvents": can_read_all_project_events,
            "canManageDevBranches": can_manage_dev_branches,
            "canManageTokens": can_manage_tokens,
        }
        if expires_in is not None:
            payload["expiresIn"] = expires_in
        response = self._do_request("POST", f"/manage/projects/{project_id}/tokens", json=payload)
        return response.json()

    # ------------------------------------------------------------------
    # Project members & invitations (verified 2026-05-01 against the
    # us-east4.gcp.keboola.com Manage API; see plan-of-record §"Verifications").
    # ------------------------------------------------------------------

    def create_project_invitation(
        self,
        project_id: int,
        email: str,
        role: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Send an invitation email to add ``email`` as a project member.

        Returns the invitation object on success (HTTP 201). On HTTP 400 with
        the error message ``"This user has already been invited..."`` or
        ``"...is already a member..."`` the caller should treat the call as a
        no-op rather than an error -- the higher layer encodes that policy.

        Args:
            project_id: Numeric project ID.
            email: Email of the user to invite.
            role: One of ``admin``, ``guest``, ``readOnly``, ``share``.
            reason: Optional human-readable note attached to the invitation.

        Returns:
            Invitation dict: ``{id, created, expires, reason, role, user, creator}``.
        """
        payload: dict[str, Any] = {"email": email, "role": role}
        if reason:
            payload["reason"] = reason
        response = self._do_request(
            "POST", f"/manage/projects/{project_id}/invitations", json=payload
        )
        return response.json()

    def list_project_invitations(self, project_id: int) -> list[dict[str, Any]]:
        """List pending (not-yet-accepted) invitations for a project.

        Returns a plain list. Each item has shape
        ``{id, created, expires, reason, role, user: {id, name, email}, creator: {...}}``.
        """
        response = self._do_request("GET", f"/manage/projects/{project_id}/invitations")
        return response.json()

    def cancel_project_invitation(self, project_id: int, invitation_id: int) -> None:
        """Cancel a pending invitation by ID. Returns 204 No Content on success."""
        self._do_request("DELETE", f"/manage/projects/{project_id}/invitations/{invitation_id}")

    def list_project_members(self, project_id: int) -> list[dict[str, Any]]:
        """List active project members.

        Returns a plain list. Each user dict carries the project role at the
        top level (``role`` field) -- not nested under a ``user`` key.
        """
        response = self._do_request("GET", f"/manage/projects/{project_id}/users")
        return response.json()

    def remove_project_member(self, project_id: int, user_id: int) -> None:
        """Remove a member from a project. Returns 204 No Content on success."""
        self._do_request("DELETE", f"/manage/projects/{project_id}/users/{user_id}")

    def update_project_member_role(
        self, project_id: int, user_id: int, role: str
    ) -> dict[str, Any]:
        """Change an existing member's role.

        The Manage API uses **PATCH** here -- ``PUT`` returns 404 even on real
        members. Returns the updated user dict on success (HTTP 200).
        """
        response = self._do_request(
            "PATCH",
            f"/manage/projects/{project_id}/users/{user_id}",
            json={"role": role},
        )
        return response.json()
