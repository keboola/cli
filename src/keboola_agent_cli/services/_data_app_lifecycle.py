"""Data-app ``/v2`` lifecycle guards: suspend/resume conflicts and replaced deployments."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..errors import ErrorCode, KeboolaApiError


def _replacing_deployment_id(app: dict[str, Any], deployment: dict[str, Any]) -> str:
    """The newer deployment production was asked to serve instead; ``""`` when none was.

    - compares creation times, so a promotion still in flight never reads as replaced
    """
    production = app.get("production") or {}
    desired = str(production.get("desiredDeploymentId") or "")
    if not desired or desired == str(deployment.get("id") or ""):
        return ""
    try:
        desired_at = datetime.fromisoformat(str(production.get("desiredCreatedAt")))
        created_at = datetime.fromisoformat(str(deployment.get("createdAt")))
        return desired if desired_at > created_at else ""
    except (TypeError, ValueError):  # missing, malformed, or naive vs aware
        return ""


def suspend_or_resume(
    toggle: Callable[[str], dict[str, Any]], app_id: str, action: str
) -> dict[str, Any]:
    """Call ``/suspend`` or ``/resume``; a ``409`` becomes a hint to deploy first.

    - ``/v2`` answers ``409`` when no deployment serves production: the app was
      never deployed, or it still runs on a ``/v1`` start
    """
    try:
        return toggle(app_id)
    except KeboolaApiError as exc:
        if exc.status_code != 409:
            raise
        raise KeboolaApiError(
            message=(
                f"Cannot {action} data app {app_id}: no deployment serves its production "
                f"({exc.message}). Run `kbagent data-app deploy` first."
            ),
            status_code=exc.status_code,
            error_code=exc.error_code,
            retryable=False,
        ) from exc


def raise_if_deployment_cannot_serve(
    app_id: str, deployment_id: str, app: dict[str, Any], deployment: dict[str, Any]
) -> None:
    """Fail the ``--wait`` poll once the deployment can no longer reach production."""
    replaced_by = _replacing_deployment_id(app, deployment)
    if replaced_by:
        reason = f"a newer deployment {replaced_by} replaced it"
    elif deployment.get("archived"):
        reason = "it was deleted (archived)"
    else:
        return
    raise KeboolaApiError(
        message=(
            f"Data app {app_id} deployment {deployment_id} will not serve production: "
            f"{reason}. Run `kbagent data-app detail` to see the serving deployment."
        ),
        status_code=0,
        error_code=ErrorCode.DATA_APP_BUILD_FAILED,
        retryable=False,
    )
