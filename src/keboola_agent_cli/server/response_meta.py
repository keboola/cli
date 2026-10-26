"""What a REST response says beside its body: targets, warnings and the error fields.

A REST success body is the bare dict of a service, so the ``targets`` record
of a request and its warnings go into two JSON response headers (spec V3):
``Kbagent-Targets`` and ``Kbagent-Warnings``. :class:`ResponseMetaMiddleware`
opens a fresh record for each request (``effective_branch.open_request_record``)
and a fresh warnings list, and adds the two headers when they are not empty.
An error body carries the same two lists as ``targets`` and ``warnings`` keys,
and its ``error`` object has the fields of the CLI error object (spec V4):
``code``, ``error_type``, ``message``, ``project``, ``retryable``.

The HTTP status of an error follows the error-code table in ``errors.py``
(:func:`http_status_for`), so one code has one status on every route.

A destructive or admin request (its route key or a flag escalation, see
:func:`note_operation`) whose branch came from the active branch gets a
``TARGET_FROM_ACTIVE_CHOICE`` warning, as such a CLI command under ``--json``
does (spec T12).
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from ..commands._helpers import DEPRECATED_CODE, deprecation_entry
from ..effective_branch import open_request_record, recorded_targets
from ..errors import ErrorCode, classify_error_code
from ..output import TARGET_FROM_ACTIVE_CHOICE_CODE, WARNING_CODE
from ..permissions import FLAG_ESCALATIONS, OPERATION_REGISTRY

TARGETS_HEADER = "Kbagent-Targets"
WARNINGS_HEADER = "Kbagent-Warnings"
# V8: the request changed the active project or branch saved on disk, which
# every CLI call and REST client of the same configuration directory uses.
ACTIVE_CHOICE_SHARED_CODE = "ACTIVE_CHOICE_SHARED"


@dataclass
class _RequestMeta:
    """The warnings of one request, and whether it is destructive or admin (spec T12)."""

    warnings: list[dict[str, Any]] = field(default_factory=list)
    warn_active_choice: bool = False


_REQUEST_META: ContextVar[_RequestMeta | None] = ContextVar("kbagent_request_meta", default=None)

# The HTTP status of each `error_type` of the error-code table. An `api` or
# `network` code is a failed call to an upstream API (502), except a code
# that exits 2: that one rejects the request itself (400).
_STATUS_BY_ERROR_TYPE = {
    "validation": 400,
    "configuration": 400,
    "authentication": 401,
    "authorization": 403,
    "not_found": 404,
    "conflict": 409,
}


# Two codes that the server itself answers with a fixed status: the bearer
# check of the server (401) and an unhandled error (500).
_SERVER_STATUS = {ErrorCode.UNAUTHORIZED: 401, ErrorCode.INTERNAL_ERROR: 500}


def http_status_for(error_code: str) -> int:
    """The HTTP status of ``error_code``, from its ``error_type`` and exit code."""
    if error_code in _SERVER_STATUS:
        return _SERVER_STATUS[ErrorCode(error_code)]
    entry = classify_error_code(error_code)
    if entry.error_type in _STATUS_BY_ERROR_TYPE:
        return _STATUS_BY_ERROR_TYPE[entry.error_type]
    return 400 if entry.exit_code == 2 else 502


def add_warning(message: str, *, code: str = WARNING_CODE, **extra: Any) -> None:
    """Add an entry to the ``Kbagent-Warnings`` header of the running request.

    The entry is ``{"code", "message"}`` plus every ``extra`` field, the shape
    of a ``warnings[]`` entry in the CLI envelope. Outside a request it does
    nothing.
    """
    meta = _REQUEST_META.get()
    if meta is not None:
        meta.warnings.append({"code": code, "message": message, **extra})


def warn_deprecated(form: str) -> None:
    """Add the ``DEPRECATED`` warning of a ``DEPRECATIONS`` entry (spec C3) to the request."""
    message, extra = deprecation_entry(form)
    add_warning(message, code=DEPRECATED_CODE, **extra)


def warn_active_choice_shared(config_dir: object, what: str) -> None:
    """V8: say that the saved active ``what`` is shared with every caller of ``config_dir``."""
    directory = str(config_dir)
    add_warning(
        f"The {what} is saved in the configuration directory {directory}. Every CLI "
        "call and every REST client that uses this directory now uses it.",
        code=ACTIVE_CHOICE_SHARED_CODE,
        config_dir=directory,
    )


def _targets() -> list[dict[str, Any]]:
    return [target.to_dict() for target in recorded_targets()]


def note_operation(operation: str) -> None:
    """Note an operation key that the request was checked for (route table, inline, flag).

    A ``destructive`` or ``admin`` key (``FLAG_ESCALATIONS`` first) makes the
    active-branch targets of the request get a warning (spec T12).
    """
    meta = _REQUEST_META.get()
    category = FLAG_ESCALATIONS.get(operation) or OPERATION_REGISTRY.get(operation)
    if meta is not None and category in ("destructive", "admin"):
        meta.warn_active_choice = True


def _warnings() -> list[dict[str, Any]]:
    """The request's warnings, plus T12: a destructive or admin request on the active branch.

    The project of a REST route is in its path or query, so only the branch
    half of the CLI warning applies. The hint names the explicit parameter.
    """
    meta = _REQUEST_META.get()
    if meta is None:
        return []
    found = list(meta.warnings)
    if meta.warn_active_choice:
        for target in recorded_targets():
            if target.role == "target" and target.branch_source == "active_branch":
                found.append(
                    {
                        "code": TARGET_FROM_ACTIVE_CHOICE_CODE,
                        "message": f"Branch {target.branch_id} of project "
                        f"'{target.project_alias}' comes from 'kbagent branch use', "
                        "not from the request.",
                        "hint": f"branch_id={target.branch_id}",
                    }
                )
    return found


def error_response(
    message: str,
    error_code: ErrorCode | str,
    *,
    http_status: int,
    request: Request | None = None,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """The REST error envelope: ``targets``, ``warnings`` and the CLI error fields.

    ``project`` is the ``{project}`` path parameter of the request, or its one
    ``project`` query parameter. It is empty when the request names no project.
    """
    project = ""
    if request is not None:
        project = request.path_params.get("project") or ""
        if not project:
            values = request.query_params.getlist("project")
            project = values[0] if len(values) == 1 else ""
    error: dict[str, Any] = {
        "code": str(error_code),
        "error_type": classify_error_code(str(error_code)).error_type,
        "message": message,
        "project": project,
        "retryable": retryable,
    }
    if details:
        # Only when non-empty, matching the CLI envelope's presence contract.
        error["details"] = details
    content: dict[str, Any] = {"status": "error"}
    targets = _targets()
    if targets:
        content["targets"] = targets
    warnings = _warnings()
    if warnings:
        content["warnings"] = warnings
    content["error"] = error
    return JSONResponse(status_code=http_status, content=content)


class ResponseMetaMiddleware:
    """Open the per-request record and add ``Kbagent-Targets`` / ``Kbagent-Warnings``.

    Pure ASGI, so a streamed body (SSE, file download) is not buffered. A
    FastAPI dependency cannot do this: it cannot add a header to the dict a
    route returns, and a ContextVar that a sync dependency sets lives in a
    copied context that the route does not see.

    The ContextVars are not reset after the response: the 500 handler of an
    unhandled error runs after this middleware returns, in the same task, and
    still reads the record. Each request runs in its own task and sets fresh
    values first, so nothing carries over to the next request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        open_request_record()
        _REQUEST_META.set(_RequestMeta())

        async def _send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                targets = _targets()
                if targets:
                    headers.append(TARGETS_HEADER, json.dumps(targets, separators=(",", ":")))
                warnings = _warnings()
                if warnings:
                    headers.append(WARNINGS_HEADER, json.dumps(warnings, separators=(",", ":")))
            await send(message)

        await self.app(scope, receive, _send)
