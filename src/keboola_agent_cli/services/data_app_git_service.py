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
* Git auth switch (``git-auth-set``) -- moves an *external* repo's
  ``parameters.dataApp.git`` to organization auth in one Storage write (see
  ``_data_app_git_source``).
"""

from __future__ import annotations

from typing import Any

from ..data_science_client import DataScienceClient
from ..errors import ErrorCode, KeboolaApiError
from ._data_app_bodies import (
    DATA_APP_COMPONENT_ID,
    _coerce_config_dict,
    _redact_git_block,
    _redact_storage_config,
)
from ._data_app_git_source import (
    VALID_GIT_AUTH_TYPES,
    GitAuthSwitch,
    describe_git_auth,
    plan_organization_switch,
)
from .base import BaseService, ClientFactory, make_session_aware_client_factory

DataScienceClientFactory = Any  # Callable[[str, str], DataScienceClient]


class DataAppGitService(BaseService):
    """Service for the data-app git-repo endpoints (sandboxes-service)."""

    def __init__(
        self,
        config_store: Any,
        client_factory: ClientFactory | None = None,
        ds_client_factory: DataScienceClientFactory | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._ds_client_factory = ds_client_factory or make_session_aware_client_factory(
            config_store, DataScienceClient
        )

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

    def set_data_app_git_auth(
        self,
        *,
        alias: str,
        app_id: str,
        git_auth: str,
        git_repo: str | None = None,
        git_branch: str | None = None,
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Switch an external-repo data app to organization GitHub auth.

        Read-modify-write of the linked Storage config: ``authType`` is set
        and ``username`` / ``#password`` / ``#sshKey`` are removed in the SAME
        write -- the backend rejects a config that carries both, so a
        ``config update --merge`` (which can only add keys) cannot do it. An
        app already on organization auth with the same repo and branch is
        left untouched (``status: unchanged``, no write). Never deploys: the
        running container keeps the old config until ``data-app deploy``.
        """
        if git_auth not in VALID_GIT_AUTH_TYPES:
            raise KeboolaApiError(
                message=(
                    f"Invalid --git-auth '{git_auth}'. Valid values: "
                    f"{', '.join(VALID_GIT_AUTH_TYPES)}."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        project = self.resolve_projects([alias])[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)
        try:
            config_id, envelope = self._load_external_app_config(
                ds_client, storage_client, str(app_id), branch_id
            )
            body = _coerce_config_dict(envelope.get("configuration"))
            switch = plan_organization_switch(body, repo=git_repo, branch=git_branch)
            result = self._git_auth_result(alias, str(app_id), config_id, switch)
            if not switch.changed:
                return result | {"status": "unchanged", "deploy_required": False}
            if dry_run:
                return result | {
                    "status": "dry_run",
                    "dry_run": True,
                    "put_storage_config_preview": _redact_storage_config(
                        {"configuration": switch.body}
                    ),
                    "message": "Dry run -- no write. Inspect the Storage PUT body above.",
                }
            response = storage_client.update_config(
                component_id=DATA_APP_COMPONENT_ID,
                config_id=config_id,
                configuration=switch.body,
                change_description="Switch git auth to organization via kbagent data-app git-auth-set",
                branch_id=branch_id,
            )
        finally:
            ds_client.close()
            storage_client.close()
        return result | {
            "status": "switched",
            "config_version_before": str(envelope.get("version", "") or ""),
            "config_version_after": str(response.get("version", "") or ""),
        }

    @staticmethod
    def _load_external_app_config(
        ds_client: Any, storage_client: Any, app_id: str, branch_id: int | None
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(config_id, storage_envelope)`` of an app that clones an external repo."""
        app = ds_client.get_app(app_id)
        if app.get("hasManagedGitRepo"):
            raise KeboolaApiError(
                message=(
                    f"Data app {app_id} uses a Keboola-managed git repository; "
                    "organization GitHub auth applies only to an external GitHub repo."
                ),
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        config_id = str(app.get("configId") or "")
        if not config_id:
            raise KeboolaApiError(
                message=f"Data app {app_id} has no associated configId",
                status_code=500,
                error_code=ErrorCode.API_ERROR,
                retryable=False,
            )
        envelope = storage_client.get_config_detail(
            DATA_APP_COMPONENT_ID, config_id, branch_id=branch_id
        )
        return config_id, envelope if isinstance(envelope, dict) else {}

    @staticmethod
    def _git_auth_result(
        alias: str, app_id: str, config_id: str, switch: GitAuthSwitch
    ) -> dict[str, Any]:
        next_step = f"kbagent data-app deploy --project {alias} --app-id {app_id} --wait"
        return {
            "project_alias": alias,
            "app_id": app_id,
            "config_id": config_id,
            "git_auth_before": describe_git_auth(switch.before),
            "git_auth": describe_git_auth(switch.after),
            "removed_credentials": switch.removed_credentials,
            "git": _redact_git_block(switch.after),
            "deploy_required": True,
            "next_step": next_step,
            "message": (
                "No change: the app already clones through organization GitHub auth."
                if not switch.changed
                else "Git auth set to organization; the stored credentials are removed. "
                "The running container keeps the old config until you redeploy."
            ),
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
