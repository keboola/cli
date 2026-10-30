"""Keboola Data Science API client -- the ``/v2`` App Deployments API.

The Data Science API owns the *deployment* side of a data app -- id, state,
url, the deployments and which of them serves production. The Storage API
(``keboola.data-apps`` configs) owns the *configuration* side -- git block,
encrypted secrets, slug, runtime size. Both must stay in sync; see
``services/data_app_service.py`` for the orchestration.

URL derivation: ``https://data-science.<stack-suffix>`` from the project's
connection URL via ``BaseHttpClient._derive_service_url``. Auth: same
``X-StorageApi-Token`` as the Storage API (or, for a browser-login session,
``Authorization: Bearer`` + ``X-KBC-ProjectId`` through ``http_auth``). Every
endpoint below, ``GET /v2/apps/{id}/password`` included, needs only a token of
the app's own project.

Endpoints (contract: sandboxes-service ``/v2`` OpenAPI):

    POST   /v2/apps                         -> 201, App {id, configId, state, production, ...}
    GET    /v2/apps?limit=N&offset=M        -> 200, [App, ...] (no filters; paginated)
    GET    /v2/apps/{id}                    -> 200, App
    DELETE /v2/apps/{id}                    -> 202, stops every deployment and moves
                                                the Storage config to the trash
    POST   /v2/apps/{id}/deployments        -> 202, Deployment {id, configVersion,
                                                buildState, runState, ...}; promoted
                                                to production by default
    GET    /v2/apps/{id}/deployments/{dep}  -> 200, Deployment
    POST   /v2/apps/{id}/suspend            -> 200, App (state=suspended; wakes on
                                                the next request)
    POST   /v2/apps/{id}/resume             -> 200, App (state=starting -> running)
    GET    /v2/apps/{id}/password           -> 200, {password: str | null}
    GET    /v2/apps/{id}/runs               -> 200, [Run, ...], newest first
    GET    /v2/apps/{id}/logs/tail          -> 200, text/plain container log tail.
                                                ``lines=N`` and ``since=ISO8601`` are
                                                mutually exclusive on the server.

App ``state`` is one of ``notDeployed``, ``starting``, ``running``,
``suspended`` (wakes on the next request) and ``disabled`` (stopped by a
failure; only an explicit start clears it). ``production`` carries
``desiredDeploymentId`` / ``actualDeploymentId`` and their config versions:
they differ while a rollout is in flight.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Self
from urllib.parse import quote

import httpx

from .constants import (
    DATA_SCIENCE_APPS_MAX_PAGES,
    DATA_SCIENCE_APPS_PAGE_SIZE,
    DEFAULT_TIMEOUT,
)
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


def _app_path(app_id: str) -> str:
    return f"/v2/apps/{quote(str(app_id), safe='')}"


class DataScienceClient(BaseHttpClient):
    """HTTP client for the Keboola Data Science ``/v2`` App Deployments API.

    Inherits retry / backoff / token-masking from ``BaseHttpClient``.
    """

    def __init__(self, stack_url: str, token: str, *, http_auth: httpx.Auth | None = None) -> None:
        self._stack_url = stack_url.rstrip("/")
        ds_base_url = self._derive_service_url(self._stack_url, "data-science")
        headers: dict[str, str] = {}
        if http_auth is None:
            headers["X-StorageApi-Token"] = token
        super().__init__(
            base_url=ds_base_url,
            token=token,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            http_auth=http_auth,
        )

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._do_request(
            "POST",
            path,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return response.json()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_apps(self) -> list[dict[str, Any]]:
        """Return every data app of the token's project.

        ``GET /v2/apps`` takes no filters and is paginated, so this pages with
        ``limit``/``offset`` until a short (or empty) page.
        """
        apps: list[dict[str, Any]] = []
        page_size = DATA_SCIENCE_APPS_PAGE_SIZE
        for page in range(DATA_SCIENCE_APPS_MAX_PAGES):
            response = self._do_request(
                "GET", "/v2/apps", params={"limit": page_size, "offset": page * page_size}
            )
            items = response.json()
            if not isinstance(items, list):
                break
            apps.extend(items)
            if len(items) < page_size:
                break
        else:
            # Guard against a server that ignores ``offset`` and keeps
            # returning full pages -- never loop forever, but say so.
            logger.warning(
                "GET /v2/apps: stopped after %d pages of %d; the listing may be incomplete",
                DATA_SCIENCE_APPS_MAX_PAGES,
                page_size,
            )
        return apps

    def get_app(self, app_id: str) -> dict[str, Any]:
        """Fetch a single app record by numeric app id."""
        response = self._do_request("GET", _app_path(app_id))
        return response.json()

    def list_app_runs(
        self, app_id: str, *, limit: int = 5, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List deployment attempts (runs) of a data app, newest first.

        Each run carries ``state`` (starting/running/finished/failed) plus a
        ``failureReason`` and ``startupLogs`` for unsuccessful attempts --
        including setup-phase failures (e.g. a clone/build error) that never
        produce container logs and so are invisible to ``/v2/apps/{id}/logs/tail``.
        This is the canonical way to find out *why* a deployment failed
        without the app ever serving.
        """
        response = self._do_request(
            "GET",
            f"{_app_path(app_id)}/runs",
            params={"limit": limit, "offset": offset},
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []

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
        """Create the app + its linked Storage config in one call.

        - Server-generated identifiers: ``id`` (numeric) and ``configId`` (ULID).
        - Production starts empty (``state=notDeployed``): the URL is reserved
          and 404s until a deployment is promoted.
        - ``type_`` is ``streamlit`` or ``python-js`` -- the only types ``/v2``
          creates.
        - ``config`` carries the *initial* Storage configuration body; the
          canonical flow is ``create_app`` -> encrypt secrets -> ``update_config``
          (encryption needs ``config_id`` first).
        - ``use_managed_git_repo`` provisions an empty *managed* git repository
          on the Git Service (``hasManagedGitRepo``); repo creation failure aborts
          app creation, and the repo is deleted with the app.
        """
        payload: dict[str, Any] = {
            # null is production; 0 (`--branch 0`) is production too. A dev
            # branch id must be a JSON string: the API answers HTTP 422 on a
            # number.
            "branchId": str(branch_id) if branch_id else None,
            "type": type_,
            "name": name,
            "description": description,
            "config": config,
            "useManagedGitRepo": use_managed_git_repo,
        }
        return self._post_json("/v2/apps", payload)

    def create_deployment(self, app_id: str, *, config_version: str) -> dict[str, Any]:
        """Deploy the app at a pinned Storage config version and promote it to production.

        - The deployment starts right away and replaces production once healthy;
          ``production.actualDeploymentId`` names it from then on.
        - Returns the Deployment (``id``, ``configVersion``, ``buildState``,
          ``runState``); ``runState=failed`` means its runs carry the cause.
        """
        return self._post_json(
            f"{_app_path(app_id)}/deployments", {"configVersion": config_version}
        )

    def get_deployment(self, app_id: str, deployment_id: str) -> dict[str, Any]:
        """Fetch one deployment of the app."""
        response = self._do_request(
            "GET", f"{_app_path(app_id)}/deployments/{quote(str(deployment_id), safe='')}"
        )
        return response.json()

    def suspend_app(self, app_id: str) -> dict[str, Any]:
        """Sleep the app's serving member now; the next request to its URL wakes it.

        Returns the App, already ``state=suspended``. ``409`` when the app has
        no serving member (never deployed).
        """
        response = self._do_request("POST", f"{_app_path(app_id)}/suspend")
        return response.json()

    def resume_app(self, app_id: str) -> dict[str, Any]:
        """Wake the app's serving member now, at the deployment production already serves.

        Returns the App, ``state=starting`` until the member is confirmed
        serving. ``409`` when the app has no serving member (never deployed).
        """
        response = self._do_request("POST", f"{_app_path(app_id)}/resume")
        return response.json()

    def delete_app(self, app_id: str) -> None:
        """Delete the app: stop every deployment and move its Storage config to the trash.

        Returns HTTP 202 on success; the body is empty.
        """
        self._do_request("DELETE", _app_path(app_id))

    def get_app_password(self, app_id: str) -> dict[str, Any]:
        """Return ``{"password": str | None}`` for a password-protected app.

        The sandboxes-service authorizes this call with the project token
        alone (``StorageApiTokenAuth`` + ``CanManageApp``, which only checks
        that the token's project is the app's project), the same call the
        Keboola UI makes. ``password`` is ``None`` while the app has no
        password yet.
        """
        response = self._do_request("GET", f"{_app_path(app_id)}/password")
        return response.json()

    def tail_app_logs(
        self,
        app_id: str,
        *,
        lines: int | None = None,
        since: str | None = None,
    ) -> str:
        """Fetch the container log tail from ``/v2/apps/{id}/logs/tail``.

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
        path = f"{_app_path(app_id)}/logs/tail"
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
    # Git repository (sandboxes-service /v2/apps/{id}/git-repo/*)
    #
    # These endpoints introspect and manage the git repository a data app
    # is deployed from. Ground truth: keboola/sandboxes-service server
    # source + docs/swagger.yaml. Two functional groups:
    #
    #   * Repo introspection (git-repo) -- works for ANY configured repo
    #     (managed or external); auth = the same X-StorageApi-Token,
    #     permission CanManageApp.
    #   * Credential management (/credentials GET + POST) -- ONLY for a
    #     *managed* git repo (app.managedGitRepoId set); a repo configured
    #     via `data-app create --git-repo <url>` is *external*, so these
    #     return 409. Auth needs an admin storage token
    #     (CanManageAppRepoCredentials).
    #
    # IMPORTANT response-shape gotchas (verified in both sources):
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
        response = self._do_request("GET", f"{_app_path(app_id)}/git-repo")
        body = response.json()
        return body if isinstance(body, dict) else {}

    def list_git_credentials(self, app_id: str) -> dict[str, Any]:
        """List the credentials of the app's MANAGED git repository.

        Shape: ``{"credentials": [{"id", "type", "name", "permissions",
        "ownerAdminId", "createdAt"}]}``. The ``secret`` is NEVER returned
        here. ``409`` if the app has no managed git repository; requires an
        admin storage token.
        """
        response = self._do_request("GET", f"{_app_path(app_id)}/git-repo/credentials")
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
        return self._post_json(f"{_app_path(app_id)}/git-repo/credentials", payload)
