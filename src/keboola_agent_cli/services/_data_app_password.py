"""Pure helpers for ``DataAppService.get_data_app_password``.

Split out of ``data_app_service.py``, which is on the file-size ratchet
(CONTRIBUTING.md "File-size budgets"). The service method does the three API
calls; this module holds the decisions around them:

- which auth a data-app config uses (the Keboola UI fetches the password only
  when ``auth_providers[0].type == "password"``, and so does kbagent);
- the Keboola UI page that shows the password (``ui_url``);
- the result shape. :class:`DataAppPassword` keeps the password apart from the
  metadata and out of ``repr``, so no formatter can print it by accident. The
  password reaches output only where a caller adds it on purpose (``--reveal``
  on the CLI, ``reveal=true`` over ``serve``).

The password must never be part of an exception message: the command layer
sends error text to telemetry (``commands/_helpers.map_error_to_exit_code``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig

PASSWORD_AUTH = "password"
PUBLIC_AUTH = "public"
MISSING_AUTH = "missing"
UNKNOWN_AUTH = "unknown"


@dataclass(frozen=True)
class DataAppPassword:
    """Metadata of a password-protected data app, plus its password.

    ``password`` is excluded from ``repr`` and from :meth:`metadata`, so
    printing or serializing the metadata never includes it.
    """

    project_alias: str
    app_id: str
    auth: str
    app_url: str
    ui_url: str | None
    password: str = field(repr=False)

    def metadata(self) -> dict[str, Any]:
        """Every field except the password."""
        return {
            "project_alias": self.project_alias,
            "app_id": self.app_id,
            "auth": self.auth,
            "app_url": self.app_url,
            "ui_url": self.ui_url,
        }

    def ui_hint(self) -> str:
        """Tell the user where the Keboola UI shows the password."""
        if self.ui_url:
            return f"Open {self.ui_url} and log in; the password is shown under Open App."
        return "Open the data app in the Keboola UI; the password is shown under Open App."


def app_branch_id(app: dict[str, Any]) -> int | None:
    """The numeric ``branchId`` of a Data Science app record, or None (default branch)."""
    raw = str(app.get("branchId") or "")
    return int(raw) if raw.isdigit() else None


def data_app_auth_kind(configuration: dict[str, Any]) -> str:
    """Name the auth a ``keboola.data-apps`` configuration sets.

    Returns the first auth provider's ``type`` (``password``, ``oidc``, ...),
    ``public`` for an empty provider list (the UI "None" option), or
    ``missing`` when the config has no ``authorization.app_proxy`` block.
    """
    authorization = configuration.get("authorization")
    app_proxy = authorization.get("app_proxy") if isinstance(authorization, dict) else None
    if not isinstance(app_proxy, dict) or "auth_providers" not in app_proxy:
        return MISSING_AUTH
    providers = app_proxy.get("auth_providers")
    if not isinstance(providers, list) or not providers:
        return PUBLIC_AUTH
    first = providers[0]
    kind = first.get("type") if isinstance(first, dict) else None
    return str(kind) if kind else UNKNOWN_AUTH


def require_password_auth(app_id: str, auth: str) -> None:
    """Refuse an app whose auth is not ``password`` -- it has no password to read."""
    if auth != PASSWORD_AUTH:
        raise KeboolaApiError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message=(
                f"Data app {app_id} does not use password authentication (auth: {auth}), "
                "so it has no password to read."
            ),
        )


def data_app_ui_url(project: ProjectConfig, branch_id: int | None, config_id: str) -> str | None:
    """The Keboola UI detail page of a data app, or None when an id is unknown."""
    if project.project_id is None or not config_id:
        return None
    branch = branch_id if branch_id is not None else "default"
    stack_url = project.stack_url.rstrip("/")
    return f"{stack_url}/admin/projects/{project.project_id}/branch/{branch}/data-apps/{config_id}"


def build_data_app_password(
    *,
    alias: str,
    app_id: str,
    project: ProjectConfig,
    app: dict[str, Any],
    payload: Any,
) -> DataAppPassword:
    """Assemble the result, or refuse when the app has no password yet (``null``)."""
    password = payload.get("password") if isinstance(payload, dict) else None
    if not password:
        raise KeboolaApiError(
            error_code=ErrorCode.NOT_FOUND,
            message=(
                f"Data app {app_id} has no password yet. "
                "Deploy the app (`kbagent data-app deploy`), then try again."
            ),
        )
    return DataAppPassword(
        project_alias=alias,
        app_id=str(app_id),
        auth=PASSWORD_AUTH,
        app_url=str(app.get("url") or ""),
        ui_url=data_app_ui_url(project, app_branch_id(app), str(app.get("configId") or "")),
        password=str(password),
    )
