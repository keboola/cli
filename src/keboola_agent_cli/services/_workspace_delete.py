"""Workspace delete: the ``workspaceDrop`` job plus the workspace's sandbox config.

A workspace created for the UI is backed by a ``keboola.sandboxes``
configuration. Deleting the workspace must trash that configuration too, or
it is left behind with nothing ``workspace gc`` can find: gc looks for
workspaces whose configuration is gone, not for configurations whose
workspace is gone.

The drop is an async Storage job that can outlast the local wait. The
configuration is trashed on that timeout as well: if the drop still fails
later, the workspace is the one left without a configuration, which is the
orphan ``workspace gc`` removes. The configuration is located before the
DELETE, because a DELETE on an already-trashed configuration purges it
(``_config_trash``) -- the case of a re-run after such a timeout.

Kept out of ``workspace_service.py`` because that module is over its size
budget (``make loc-check``).
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ._config_trash import STATE_LIVE, locate_config
from ._storage_jobs import with_job_timeout_hint

logger = logging.getLogger(__name__)

SANDBOX_COMPONENT = "keboola.sandboxes"


def delete_workspace_and_config(
    client: Any, alias: str, workspace_id: int, branch_id: int | None
) -> None:
    """Drop the workspace, then trash its ``keboola.sandboxes`` config if it has one.

    Raises:
        KeboolaApiError: The drop failed, or ``STORAGE_JOB_TIMEOUT``
            (``retryable=False``) when it outlasted the wait. The timeout
            carries ``job_id``, ``workspace_id``, ``branch_id`` and, for a
            sandbox workspace, ``sandbox_config_id`` with
            ``sandbox_config_trashed`` (plus ``cleanup_command`` when the
            config could not be trashed).
    """
    config_id = None
    try:
        ws_data = client.get_workspace(workspace_id, branch_id=branch_id)
    except KeboolaApiError:
        # A missing workspace is reported by the DELETE itself (NOT_FOUND).
        ws_data = {}
    if ws_data.get("component") == SANDBOX_COMPONENT:
        config_id = ws_data.get("configurationId")

    try:
        client.delete_workspace(workspace_id, branch_id=branch_id)
    except KeboolaApiError as exc:
        if exc.error_code != ErrorCode.STORAGE_JOB_TIMEOUT:
            raise
        raise _drop_timeout_error(client, alias, exc, workspace_id, branch_id, config_id) from exc

    if config_id:
        try:
            _trash_sandbox_config(client, config_id, branch_id)
        except KeboolaApiError:
            logger.debug("Could not delete sandbox config %s", config_id)


def _trash_sandbox_config(client: Any, config_id: str, branch_id: int | None) -> None:
    """Move the sandbox config to the trash; do nothing when it is not live."""
    if locate_config(client, SANDBOX_COMPONENT, config_id, branch_id) == STATE_LIVE:
        client.delete_config(SANDBOX_COMPONENT, config_id, branch_id=branch_id)


def _cleanup_command(alias: str, config_id: str, branch_id: int | None) -> str:
    args = ["kbagent", "config", "delete", "--project", alias]
    args += ["--component-id", SANDBOX_COMPONENT, "--config-id", str(config_id)]
    if branch_id is not None:
        args += ["--branch", str(branch_id)]
    return shlex.join(args)


def _drop_timeout_error(
    client: Any,
    alias: str,
    exc: KeboolaApiError,
    workspace_id: int,
    branch_id: int | None,
    config_id: str | None,
) -> KeboolaApiError:
    """The drop-job timeout, after the sandbox config was trashed (or not)."""
    timeout = with_job_timeout_hint(exc, alias)
    details: dict[str, Any] = {
        **timeout.details,
        "workspace_id": workspace_id,
        "branch_id": branch_id,
    }
    outcome = ""
    if config_id:
        details["sandbox_config_id"] = config_id
        try:
            _trash_sandbox_config(client, config_id, branch_id)
        except KeboolaApiError as cleanup_exc:
            command = _cleanup_command(alias, config_id, branch_id)
            details["sandbox_config_trashed"] = False
            details["cleanup_command"] = command
            outcome = (
                f" Sandbox config {config_id} could not be moved to the trash "
                f"({cleanup_exc.message}); remove it with: {command}"
            )
        else:
            details["sandbox_config_trashed"] = True
            outcome = (
                f" Sandbox config {config_id} was moved to the trash; if the drop fails, "
                f"'kbagent workspace gc --project {shlex.quote(alias)}' removes the workspace."
            )
    return KeboolaApiError(
        message=timeout.message + outcome,
        status_code=timeout.status_code,
        error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
        retryable=False,
        details=details,
    )
