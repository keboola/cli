"""Git-repository introspection + managed-repo credential management for data apps.

Split out of ``data_app_service.py`` to respect the file-size budget
(CONTRIBUTING.md "File-size budgets"): the lifecycle service was already over
its hard ceiling, so this distinct concern (the sandboxes-service
``/apps/{id}/git-repo/*`` surface) lives in its own service.

See :class:`~keboola_agent_cli.data_science_client.DataScienceClient` for the
response-shape gotchas. Two functional groups:

* Repo introspection (git-repo) -- works for any configured repo (managed or
  external); needs only the project storage token. Returns 409 until the app has
  been deployed at least once (the git block is synced from the Storage config
  into the Data Science app record at deploy time).
* Credential management (credentials GET + POST) -- only for a *managed* git
  repo (``app.managedGitRepoId`` set); apps created via
  ``data-app create --git-repo <url>`` are external, so these return 409. Needs
  an admin storage token.
"""

from __future__ import annotations

from typing import Any

from ..auth.sentinel import require_static_token
from ..data_science_client import DataScienceClient
from ..errors import ErrorCode, KeboolaApiError
from .base import BaseService, ClientFactory

DataScienceClientFactory = Any  # Callable[[str, str], DataScienceClient]


def _default_ds_client_factory(stack_url: str, token: str) -> DataScienceClient:
    """Static-token-only (v1 scope is Storage + Manage): fails fast on a
    session sentinel rather than sending it as a literal credential."""
    require_static_token(token, feature="The Data Science Service (data apps)")
    return DataScienceClient(stack_url=stack_url, token=token)


class DataAppGitService(BaseService):
    """Service for the data-app git-repo endpoints (sandboxes-service)."""

    def __init__(
        self,
        config_store: Any,
        client_factory: ClientFactory | None = None,
        ds_client_factory: DataScienceClientFactory | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._ds_client_factory = ds_client_factory or _default_ds_client_factory

    def get_data_app_git_repo(self, alias: str, app_id: str) -> dict[str, Any]:
        """Return the clone URLs of a data app's configured git repository."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            repo = ds_client.get_git_repo(app_id)
        finally:
            ds_client.close()
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "ssh_url": repo.get("sshUrl"),
            "https_url": repo.get("httpsUrl"),
            "is_managed_git_repo": bool(repo.get("isManagedGitRepo", False)),
        }

    def list_data_app_git_credentials(self, alias: str, app_id: str) -> dict[str, Any]:
        """List the credentials of a data app's MANAGED git repository.

        The ``secret`` is never returned by this endpoint. A 409 from the
        server (no managed git repo) propagates as a ``KeboolaApiError`` the
        command layer maps to a non-zero exit.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            payload = ds_client.list_git_credentials(app_id)
        finally:
            ds_client.close()
        raw = payload.get("credentials", []) if isinstance(payload, dict) else []
        credentials = [self._normalize_git_credential(c) for c in raw if isinstance(c, dict)]
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "credentials": credentials,
            "count": len(credentials),
        }

    def create_data_app_git_credential(
        self,
        *,
        alias: str,
        app_id: str,
        type_: str,
        permissions: str,
        public_key: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create a credential for a data app's MANAGED git repository.

        Validates the ``type_`` / ``public_key`` combination locally (the same
        mutex the command layer enforces with a clean exit-2) so the
        ``kbagent serve`` route does not round-trip a bare 400. The one-time
        ``secret`` is returned only for ``http_token``. A 409 (no managed git
        repo) propagates as a ``KeboolaApiError``.
        """
        if type_ not in ("ssh_key", "http_token"):
            raise KeboolaApiError(
                message=("git credential 'type' must be 'ssh_key' or 'http_token'."),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        if permissions not in ("readOnly", "readWrite"):
            raise KeboolaApiError(
                message=("git credential 'permissions' must be 'readOnly' or 'readWrite'."),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        if type_ == "ssh_key" and not public_key:
            raise KeboolaApiError(
                message="git credential type 'ssh_key' requires a public key.",
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        if type_ == "http_token" and public_key:
            raise KeboolaApiError(
                message=("git credential type 'http_token' must not carry a public key."),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            created = ds_client.create_git_credential(
                app_id,
                type_=type_,
                permissions=permissions,
                public_key=public_key,
                name=name,
            )
        finally:
            ds_client.close()
        credential = self._normalize_git_credential(created)
        secret = created.get("secret") if isinstance(created, dict) else None
        if secret:
            # Keep the one-time secret on the normalized credential so the
            # command layer can surface it; it is never retrievable again.
            credential["secret"] = secret
        message = f"Created {type_} credential for data app {app_id} in '{alias}'." + (
            " The one-time secret is shown below and cannot be retrieved again." if secret else ""
        )
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "credential": credential,
            "message": message,
        }

    @staticmethod
    def _normalize_git_credential(raw: dict[str, Any]) -> dict[str, Any]:
        """Translate a sandboxes-service credential record to snake_case.

        Deliberately omits ``secret`` -- callers that need the one-time secret
        read it from the raw create response, never from this map.
        """
        return {
            "id": raw.get("id", ""),
            "type": raw.get("type", ""),
            "name": raw.get("name", ""),
            "permissions": raw.get("permissions", ""),
            "owner_admin_id": raw.get("ownerAdminId", ""),
            "created_at": raw.get("createdAt", ""),
        }
