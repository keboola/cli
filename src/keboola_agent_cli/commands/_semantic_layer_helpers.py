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
from ._checkbox_select import CheckboxItem, CheckboxUnavailable, checkbox_select
from ._helpers import get_formatter, get_service, map_error_to_exit_code


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


def resolve_scope_targets(
    ctx: typer.Context,
    *,
    scope: str,
    target_project: list[str] | None,
    owner_alias: str,
) -> list[str] | None:
    """Resolve ``--target-project`` aliases for ``--scope targeted``.

    Returns ``None`` when ``scope`` isn't ``"targeted"`` (nothing to
    resolve) or the raw alias list when the caller already gave explicit
    ``--target-project`` values. When ``--scope targeted`` is chosen with
    none given, this is the "ask when uncertain" mechanism: on a real
    terminal it launches the checkbox picker over every OTHER registered
    project; in a non-TTY or ``--json`` context it hard-fails instead of
    silently defaulting -- widening an object's visibility across projects
    is not a guess this CLI makes on the caller's behalf.
    """
    if scope != "targeted":
        return None
    if target_project:
        return list(target_project)

    formatter = get_formatter(ctx)
    project_service = get_service(ctx, "project_service")
    candidates = [p for p in project_service.list_projects() if p["alias"] != owner_alias]
    if not candidates:
        formatter.error(
            message=(
                "--scope targeted needs at least one other registered project to "
                "target. Register one with `kbagent project add`, pass "
                "--target-project, or use --scope project."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    if formatter.json_mode:
        formatter.error(
            message=(
                "--scope targeted requires --target-project (repeatable) in "
                "--json mode -- there is no terminal to pick from."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    items = [
        CheckboxItem(
            label=p["alias"],
            hint=f"{p.get('project_name', '')} ({p.get('project_id', '?')})",
        )
        for p in candidates
    ]
    try:
        selected = checkbox_select(
            items, title="Select project(s) to grant visibility to (targeted scope):"
        )
    except CheckboxUnavailable:
        formatter.error(
            message=(
                "--scope targeted requires --target-project (repeatable) -- no "
                "interactive terminal available to pick from."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    if not selected:
        formatter.error(
            message="No target project selected. Pass --target-project or use --scope project.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)
    return [candidates[i]["alias"] for i in selected]
