"""Shared helpers for the ``semantic-layer`` command group.

Split out of :mod:`commands.semantic_layer` so that the ``add`` / ``edit`` /
``remove`` sub-apps -- which live in :mod:`commands._semantic_layer_crud` --
can reuse the same error-handling and stdin-TTY probe without forcing a
circular import between the two command modules.
"""

from __future__ import annotations

import sys

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, map_error_to_exit_code


def _handle_service_call(ctx: typer.Context, func, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Run a service call, mapping ``ConfigError`` / ``KeboolaApiError`` to exit codes.

    Returns the service result on success; on failure, prints the structured
    error envelope (JSON mode) or a red error line (human mode) and raises
    ``typer.Exit`` with the appropriate code.
    """
    formatter = get_formatter(ctx)
    try:
        return func(*args, **kwargs)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details=exc.details,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None


def _is_stdin_tty() -> bool:
    """Return ``True`` when stdin is attached to a TTY (interactive shell)."""
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
