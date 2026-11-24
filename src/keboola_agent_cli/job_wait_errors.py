"""Errors for a Queue job that was already created when waiting for it failed.

Shared by the CLI/service path (``services/job_service.py``) and the SDK
facade (``lib.py``). It depends only on ``errors`` and ``httpx``, so the SDK
does not load the service layer (CONTRIBUTING.md, SDK checklist).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import ErrorCode, KeboolaApiError

logger = logging.getLogger(__name__)


def raw_poll_error(exc: Exception) -> KeboolaApiError:
    """A ``KeboolaApiError`` for an exception that escaped ``wait_for_queue_job`` raw.

    The HTTP layer translates connect errors and timeouts, but another
    transport error (a reset connection, a protocol error) or an unreadable
    poll response reaches the caller untranslated.
    """
    # ValueError / TypeError / AttributeError can also be a bug in kbagent
    # itself; keep the traceback in the debug log (`--verbose`), the error
    # keeps it as ``__cause__``.
    logger.debug("Untranslated error while polling a Queue job", exc_info=exc)
    if isinstance(exc, httpx.TransportError):
        return KeboolaApiError(
            message=f"Lost the connection to the Queue API while polling the job: {exc}",
            status_code=0,
            error_code=ErrorCode.CONNECTION_ERROR,
        )
    return KeboolaApiError(
        message=f"Could not read the job state from the Queue API: {exc}",
        status_code=0,
        error_code=ErrorCode.API_ERROR,
    )


# Raw exceptions ``wait_for_queue_job`` can let through: transport errors the
# HTTP layer does not translate, and a poll response that is not a job object.
RAW_POLL_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    ValueError,
    TypeError,
    AttributeError,
)


def created_job_error(
    exc: KeboolaApiError,
    *,
    job_id: str,
    follow_up: str,
    details: dict[str, Any] | None = None,
) -> KeboolaApiError:
    """``exc``, raised while waiting for job ``job_id`` that was already created.

    The code and HTTP status stay (so the exit code does too); ``retryable``
    is always false, because starting the job again creates a second one
    (spec LR3). ``details`` gains ``job_id`` and the message names
    ``follow_up``, the way to read the job (a CLI command, or an SDK call).
    """
    base = exc.details if details is None else details
    return KeboolaApiError(
        message=(
            f"{exc.message} Queue job {job_id} was created and may still be running: "
            f"check it with {follow_up}. Do not start it again."
        ),
        status_code=exc.status_code,
        error_code=exc.error_code,
        retryable=False,
        details={**(base or {}), "job_id": job_id},
    )
