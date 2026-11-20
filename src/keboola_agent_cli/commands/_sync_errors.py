"""One error mapping for the sync commands that read a working tree (L6).

``SYNC_DIRECTORY_BUSY`` (another kbagent run holds the directory) is a
``KeboolaApiError``: it reaches the caller with its code, ``retryable`` and
``details`` through :func:`emit_api_error`, never as a crash or another code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import emit_api_error, map_error_to_exit_code


@contextmanager
def sync_errors(formatter: Any) -> Iterator[None]:
    """Report a sync service error and exit: no tree 1, config 5, API / busy by its code."""
    try:
        yield
    except FileNotFoundError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.NOT_INITIALIZED)
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        # A corrupted .keboola/branch-mapping.json lands here too: a clean
        # exit-5 envelope, never a traceback (issue #269 sec-20 follow-up).
        formatter.error(
            message=exc.message, error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)
        )
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        emit_api_error(formatter, exc)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
