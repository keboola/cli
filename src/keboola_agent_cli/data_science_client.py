"""Keboola Data Science API client (data-app deployment records).

The Data Science API owns the *deployment* side of a data app — id, state,
desiredState, url, configVersion. The Storage API
(``keboola.data-apps`` configs) owns the *configuration* side — git block,
encrypted secrets, slug, runtime size. Both must stay in sync; see
``services/data_app_service.py`` for the orchestration.

URL derivation: ``https://data-science.<stack-suffix>`` from the project's
connection URL via ``BaseHttpClient._derive_service_url``. Auth: same
``X-StorageApi-Token`` as the Storage API. The single exception is
``GET /apps/{id}/password`` which additionally requires
``X-KBC-ManageApiToken`` -- the manage token is passed per-call so the
client itself stays project-scoped.

Verified shapes (writeup §2 / §6 / §9, replayed in this PR's live
validation):

    POST   /apps                          -> 201, {id, configId, ...}
    GET    /apps                          -> 200, [{id, configId, state, desiredState, url}, ...]
    GET    /apps/{id}                     -> 200, full deployment record
    PATCH  /apps/{id}                     -> 200, deployment record (only
                                              desiredState / configVersion /
                                              restartIfRunning persist;
                                              ``config:{...}`` is silently
                                              dropped)
    DELETE /apps/{id}                     -> 202, cascades to Storage config
    GET    /apps/{id}/password            -> 200, {password: "<20 hex>"}
                                              (requires both Storage and
                                              Manage tokens)
    GET    /apps/{id}/logs/tail           -> 200, text/plain container log
                                              tail. ``lines=N`` and
                                              ``since=ISO8601`` are mutually
                                              exclusive on the server.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from .constants import DEFAULT_TIMEOUT
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


class DataScienceClient(BaseHttpClient):
    """HTTP client for the Keboola Data Science API (``/apps``).

    Inherits retry / backoff / token-masking from ``BaseHttpClient``.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        ds_base_url = self._derive_service_url(self._stack_url, "data-science")
        headers = {
            "X-StorageApi-Token": token,
        }
        super().__init__(
            base_url=ds_base_url,
            token=token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    def __enter__(self) -> DataScienceClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def list_apps(self) -> list[dict[str, Any]]:
        """Return the thin index of data apps in the project (no body filter).

        The Data Science API scopes responses by the token's project; there
        is no ``branchId`` query parameter on the list endpoint.
        """
        response = self._do_request("GET", "/apps")
        body = response.json()
        # Some stacks wrap the list in {"data": [...]}; fall back gracefully.
        apps = (body.get("data") or body.get("apps") or []) if isinstance(body, dict) else body
        return apps if isinstance(apps, list) else []

    def get_app(self, app_id: str) -> dict[str, Any]:
        """Fetch a single deployment record by numeric app id."""
        response = self._do_request("GET", f"/apps/{quote(str(app_id), safe='')}")
        return response.json()

    def list_app_runs(
        self, app_id: str, *, limit: int = 5, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List deployment attempts (runs) of a data app, newest first.

        Each run carries ``state`` (starting/running/finished/failed) plus a
        ``failureReason`` and ``startupLogs`` for unsuccessful attempts --
        including setup-phase failures (e.g. a clone/build error) that never
        produce container logs and so are invisible to ``/apps/{id}/logs/tail``.
        This is the canonical way to find out *why* a deploy reverted to
        ``stopped`` without the app ever serving.
        """
        response = self._do_request(
            "GET",
            f"/apps/{quote(str(app_id), safe='')}/runs",
            params={"limit": limit, "offset": offset},
        )
        payload = response.json()
        return payload if isinstance(payload, list) else payload.get("runs", [])

    def create_app(
        self,
        *,
        type_: str,
        name: str,
        description: str,
        config: dict[str, Any],
        branch_id: int | None = None,
        use_managed_git_repo: bool = False,
    ) -> dict[str, Any]:
        """Create the deployment shell + linked Storage config in one call.

        Server-generated identifiers: ``id`` (numeric) and ``configId``
        (ULID). The ``configId`` field in the request body is silently
        ignored (writeup §5) -- callers must accept whatever ULID the
        server assigns and round-trip it on subsequent updates.

        ``config`` carries the *initial* Storage configuration body. The
        full config (git block, encrypted secrets, etc.) is added via
        ``KeboolaClient.update_config`` after creation; sending it here is
        possible but the encryption step depends on knowing
        ``config_id`` first, so the canonical flow is:
        ``create_app`` -> encrypt secrets -> ``update_config``.

        ``use_managed_git_repo`` -> POST ``useManagedGitRepo: true`` so the
        sandboxes-service provisions a *managed* git repository on the Git
        Service and links it to the app (``managedGitRepoId``). The repo
        starts empty -- the caller mints a credential
        (``create_git_credential``), pushes code, then deploys. Repo
        creation failure aborts app creation server-side; the repo is
        deleted when the app is deleted.
        """
        payload: dict[str, Any] = {
            "branchId": branch_id,
            "type": type_,
            "name": name,
            "description": description,
            "config": config,
        }
        if use_managed_git_repo:
            payload["useManagedGitRepo"] = True
        response = self._do_request(
            "POST",
            "/apps",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def patch_app(
        self,
        app_id: str,
        *,
        desired_state: str | None = None,
        config_version: str | None = None,
        restart_if_running: bool | None = None,
    ) -> dict[str, Any]:
        """Update the deployment record (state / pinned config version).

        IMPORTANT: never sends a ``config`` block — that surface is owned
        by the Storage API (writeup §2.1, §8 pitfall row 3). Updating
        size / autoSuspend / git settings goes through ``update_config``
        on the Storage API.

        The §9 redeploy contract requires
        ``desired_state="running"`` + ``config_version=<N>``
        + ``restart_if_running=True`` together when bumping the deployed
        config version; sending ``config_version`` alone yields HTTP 422.
        """
        payload: dict[str, Any] = {}
        if desired_state is not None:
            payload["desiredState"] = desired_state
        if config_version is not None:
            payload["configVersion"] = config_version
        if restart_if_running is not None:
            payload["restartIfRunning"] = restart_if_running
        response = self._do_request(
            "PATCH",
            f"/apps/{quote(str(app_id), safe='')}",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def delete_app(self, app_id: str) -> None:
        """Delete the deployment AND the linked Storage config (cascade).

        Returns HTTP 202 on success; the body is empty.
        """
        self._do_request("DELETE", f"/apps/{quote(str(app_id), safe='')}")

    def get_app_password(self, app_id: str, manage_token: str) -> dict[str, Any]:
        """Retrieve the auto-generated simpleAuth password.

        Requires both the project's Storage token (already on
        ``self._client``) AND a Manage API token, supplied per-call so the
        manage token never lives on the client instance.

        The 20-character hex password is auto-generated at app create time
        and is NOT rotatable -- to change it you must delete and recreate
        the app (writeup §11.2).
        """
        path = f"/apps/{quote(str(app_id), safe='')}/password"
        # Pass the Manage token via per-request `headers=`. httpx merges these
        # with the client's persistent headers for this call only, so the
        # manage token never lives on `self._client`. Using `_do_request`
        # gives us the same retry/backoff and uniform error mapping as every
        # other call in this client (no bespoke try/except needed).
        response = self._do_request(
            "GET",
            path,
            headers={"X-KBC-ManageApiToken": manage_token},
        )
        return response.json()

    def tail_app_logs(
        self,
        app_id: str,
        *,
        lines: int | None = None,
        since: str | None = None,
    ) -> str:
        """Fetch the container log tail from ``/apps/{id}/logs/tail``.

        Returns the response body verbatim as plain text (``text/plain``)
        -- one log line per ``\\n``, trailing newline preserved as the
        server sent it. Callers that want a list of lines should call
        ``text.splitlines()``.

        ``lines`` and ``since`` are mutually exclusive on the server
        (400 ``Only one of "since" or "lines" can be set``); the caller
        MUST enforce that constraint before invoking. Passing neither
        returns the full current container buffer. ``lines=0`` and
        negative values are rejected by the server with a 400 -- callers
        opting into the full buffer should pass ``lines=None``.

        ``since`` must be an ISO 8601 timestamp WITH timezone (``Z`` or
        ``+00:00``); naive datetimes and date-only values are rejected
        by the server with a 400.
        """
        path = f"/apps/{quote(str(app_id), safe='')}/logs/tail"
        params: dict[str, Any] = {}
        if lines is not None:
            params["lines"] = lines
        if since is not None:
            params["since"] = since
        # ``params or None`` keeps the URL clean (no trailing ``?``) when
        # the caller wants the server's default buffer-all behavior.
        response = self._do_request("GET", path, params=params or None)
        return response.text

    # ------------------------------------------------------------------
    # Git repository (sandboxes-service /apps/{id}/git-repo/*)
    #
    # These endpoints introspect and manage the git repository a data app
    # is deployed from. Ground truth: keboola/sandboxes-service server
    # source + docs/swagger.yaml. Two functional groups:
    #
    #   * Repo introspection (git-repo, /branches, /entrypoints) -- works
    #     for ANY configured repo (managed or external); auth = the same
    #     X-StorageApi-Token, permission CanManageApp.
    #   * Credential management (/credentials GET + POST) -- ONLY for a
    #     *managed* git repo (app.managedGitRepoId set); a repo configured
    #     via `data-app create --git-repo <url>` is *external*, so these
    #     return 409. Auth needs an admin storage token
    #     (CanManageAppRepoCredentials).
    #
    # IMPORTANT response-shape gotchas (verified in both sources):
    #   * /branches returns a RAW top-level JSON array (NOT wrapped in
    #     {branches: [...]}).
    #   * /entrypoints returns a RAW top-level array<string>.
    #   * /credentials (GET) IS wrapped: {"credentials": [...]}.
    #   * POST /credentials returns the created credential; the one-time
    #     ``secret`` is present ONLY for type=http_token and ONLY here.
    # ------------------------------------------------------------------

    def get_git_repo(self, app_id: str) -> dict[str, Any]:
        """Return the clone URLs of the app's configured git repository.

        Shape: ``{"sshUrl": str|None, "httpsUrl": str|None,
        "isManagedGitRepo": bool}``. For external repos only the URL
        matching the configured protocol is populated (the other is
        ``None``) and embedded credentials are stripped. ``409`` if the
        app has no git repository configured.
        """
        response = self._do_request("GET", f"/apps/{quote(str(app_id), safe='')}/git-repo")
        body = response.json()
        return body if isinstance(body, dict) else {}

    def list_git_branches(self, app_id: str) -> list[dict[str, Any]]:
        """List the remote branches of the app's configured git repository.

        Returns the server's RAW top-level array of branch objects
        ``[{"branch", "comment", "sha", "author": {"name", "email"},
        "date"}]`` (HEAD/origin/HEAD filtered, sorted by name). Works for
        managed and external repos alike.
        """
        response = self._do_request("GET", f"/apps/{quote(str(app_id), safe='')}/git-repo/branches")
        body = response.json()
        return body if isinstance(body, list) else []

    def list_git_entrypoints(self, app_id: str) -> list[str]:
        """List root-level ``.py`` entrypoint files of the app's repo.

        Returns the server's RAW top-level ``array<string>`` of root
        filenames on the configured branch (or the repo default).
        Extension is hardcoded to ``py`` server-side, so non-Python
        entrypoints are not listable here.
        """
        response = self._do_request(
            "GET", f"/apps/{quote(str(app_id), safe='')}/git-repo/entrypoints"
        )
        body = response.json()
        return [str(item) for item in body] if isinstance(body, list) else []

    def list_git_credentials(self, app_id: str) -> dict[str, Any]:
        """List the credentials of the app's MANAGED git repository.

        Shape: ``{"credentials": [{"id", "type", "name", "permissions",
        "ownerAdminId", "createdAt"}]}``. The ``secret`` is NEVER returned
        here. ``409`` if the app has no managed git repository; requires an
        admin storage token.
        """
        response = self._do_request(
            "GET", f"/apps/{quote(str(app_id), safe='')}/git-repo/credentials"
        )
        body = response.json()
        return body if isinstance(body, dict) else {}

    def create_git_credential(
        self,
        app_id: str,
        *,
        type_: str,
        permissions: str,
        public_key: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create a credential for the app's MANAGED git repository.

        ``type_`` is ``"ssh_key"`` or ``"http_token"``; ``permissions`` is
        ``"readOnly"`` or ``"readWrite"``. ``public_key`` is required IFF
        ``type_ == "ssh_key"`` and MUST be absent otherwise (the server
        returns 400 on a wrong combination).

        Returns the created credential. The one-time ``secret`` field is
        present ONLY when ``type_ == "http_token"`` and is never retrievable
        again. ``409`` if the app has no managed git repository; requires an
        admin storage token.
        """
        payload: dict[str, Any] = {"type": type_, "permissions": permissions}
        if public_key is not None:
            payload["publicKey"] = public_key
        if name is not None:
            payload["name"] = name
        response = self._do_request(
            "POST",
            f"/apps/{quote(str(app_id), safe='')}/git-repo/credentials",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return response.json()
