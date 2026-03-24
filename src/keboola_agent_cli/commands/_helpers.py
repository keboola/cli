"""Shared command-layer helpers to eliminate duplication across command files.

Provides common patterns used by all CLI commands:
- Context extraction (formatter, services)
- Exit code mapping for API errors
- Warning emission for multi-project operations
"""

from typing import Any

import typer

from ..errors import KeboolaApiError
from ..output import OutputFormatter


def get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def get_service(ctx: typer.Context, key: str) -> Any:
    """Retrieve a service from the Typer context."""
    return ctx.obj[key]


_ERROR_CODE_TO_TYPE: dict[str, str] = {
    "INVALID_TOKEN": "authentication",
    "TIMEOUT": "network",
    "CONNECTION_ERROR": "network",
    "RETRY_EXHAUSTED": "network",
    "NOT_FOUND": "not_found",
    "CONFIG_ERROR": "configuration",
    "VALIDATION_ERROR": "validation",
}


def map_error_code_to_type(error_code: str) -> str:
    """Map a machine-readable error code to a broad error type category."""
    return _ERROR_CODE_TO_TYPE.get(error_code, "api")


def map_error_to_exit_code(exc: KeboolaApiError) -> int:
    """Map a KeboolaApiError to a CLI exit code.

    Unified 3-case logic:
    - INVALID_TOKEN -> 3 (authentication error)
    - TIMEOUT / CONNECTION_ERROR / RETRY_EXHAUSTED -> 4 (network error)
    - Everything else -> 1 (general error)
    """
    if exc.error_code == "INVALID_TOKEN":
        return 3
    if exc.error_code in ("TIMEOUT", "CONNECTION_ERROR", "RETRY_EXHAUSTED"):
        return 4
    return 1


def emit_project_warnings(formatter: OutputFormatter, result: dict) -> None:
    """Emit warnings from multi-project operation results.

    Iterates the 'errors' list in the result dict (if present) and prints
    each entry as a warning via the formatter.
    """
    for err in result.get("errors", []):
        alias = err.get("project_alias", "unknown")
        message = err.get("message", "Unknown error")
        formatter.warning(f"Project '{alias}': {message}")
