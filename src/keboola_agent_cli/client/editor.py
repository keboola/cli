"""Editor Service: SQL editor sessions of ``keboola.sandboxes`` workspaces (CLI-25).

A SQL workspace (Snowflake / BigQuery) created in the UI is only a
``keboola.sandboxes`` configuration. Its backend workspace belongs to a SQL
editor *session*, which the editor service creates when a user first opens
the workspace. ``sync push`` deletes a workspace the way the official CLI does
(``kbc remote workspace delete``): the sessions first, then the configuration.
Deleting a session also drops its Storage workspace (asynchronously, on the
service side).

Request and response shapes, from the service's OpenAPI spec (keboola/editor-service
``docs/swagger.yaml``) and keboola-sdk-go ``pkg/keboola/editor_session.go``:

- ``GET /sql/sessions`` lists the sessions of the CURRENT user only.
  ``listAll=1`` lists every session in the project; ``branchId`` limits the
  list to one branch. There is no filter by configuration.
- ``DELETE /sql/sessions/{id}`` answers 204. The service refuses (4xx) a
  session that is still ``initializing``.
- A session carries ``snowflakePrivateKey`` only when ``includeCredentials``
  is sent. This mixin never sends it.

The session list fails closed: a body that is not a JSON array of objects
raises instead of reading as "no sessions", because the caller would then
delete the workspace config and leave its sessions and workspaces behind.
"""

from typing import Any
from urllib.parse import quote

from ..errors import ErrorCode, KeboolaApiError
from ._core import _CoreClient


class _EditorMixin(_CoreClient):
    """Editor Service: list and delete SQL editor sessions."""

    def list_editor_sessions(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        """List the SQL editor sessions of every user in the project.

        Args:
            branch_id: If set, only the sessions of this branch (sent as
                ``branchId``). The production branch is its numeric id too.

        Returns:
            List of session dicts as the API returns them.

        Raises:
            KeboolaApiError: ``API_ERROR`` when the body is not valid JSON or
                not an array of objects (see the module docstring).
        """
        params: dict[str, str] = {"listAll": "1"}
        if branch_id is not None:
            params["branchId"] = str(branch_id)
        response = self._editor_request("GET", "/sql/sessions", params=params)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            raise KeboolaApiError(
                message=(
                    "Editor service returned an unexpected SQL editor session list "
                    f"(HTTP {response.status_code}, not a JSON array of sessions)."
                ),
                status_code=response.status_code,
                error_code=ErrorCode.API_ERROR,
                retryable=False,
            )
        return body

    def delete_editor_session(self, session_id: str) -> None:
        """Delete one SQL editor session (and, service-side, its workspace)."""
        self._editor_request("DELETE", f"/sql/sessions/{quote(str(session_id), safe='')}")
