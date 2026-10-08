"""Shared helpers for the ``semantic-layer`` command group.

Split out of :mod:`commands.semantic_layer` so that the ``add`` / ``edit`` /
``remove`` sub-apps -- which live in :mod:`commands._semantic_layer_crud` --
can reuse the same error-handling and stdin-TTY probe without forcing a
circular import between the two command modules.
"""

from __future__ import annotations

import sys
from enum import StrEnum

import typer

from ..errors import ErrorCode
from ._checkbox_select import CheckboxItem, CheckboxUnavailable, checkbox_select
from ._helpers import check_cli_operation, get_formatter, get_service, handle_service_call


class ScopeChoice(StrEnum):
    """Fixed values of ``--scope`` on the create commands (rendered as a click Choice)."""

    PROJECT = "project"
    ORGANIZATION = "organization"
    TARGETED = "targeted"


class ElevationScope(StrEnum):
    """``scope set --scope`` accepts only ``organization`` (scope changes are one-way)."""

    ORGANIZATION = "organization"


class ItemType(StrEnum):
    """Semantic item kinds addressable by ``scope`` commands (``--type``)."""

    MODEL = "model"
    DATASET = "dataset"
    METRIC = "metric"
    RELATIONSHIP = "relationship"
    CONSTRAINT = "constraint"
    GLOSSARY = "glossary"


# The handler is shared by every metastore-backed command group (semantic-layer, rls, cls).
_handle_service_call = handle_service_call


def _is_stdin_tty() -> bool:
    """Return ``True`` when stdin is attached to a TTY (interactive shell)."""
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def resolve_scope_targets(
    ctx: typer.Context,
    *,
    operation: str,
    scope: str | None,
    target_project: list[str] | None,
    owner_alias: str,
    inherit_from_model: tuple[str | None] | None = None,
) -> list[str] | None:
    """Validate ``--scope`` / ``--target-project`` of a create command.

    Returns the ``--target-project`` values (aliases or IDs; the service
    resolves them) for ``--scope targeted``, else ``None``. ``--scope
    organization`` is gated here as destructive (``operation`` is the command's
    permission key, e.g. ``semantic-layer.add.dataset``); so is an omitted
    ``--scope`` that would inherit ``organization`` from the model
    (``inherit_from_model=(model,)`` on the ``add`` commands). ``--target-project``
    without ``--scope targeted`` is a usage error, never silently ignored.

    With ``--scope targeted`` and no target, this is the "ask when uncertain"
    mechanism: on a real terminal it launches the checkbox picker over every
    OTHER registered project on the owner's stack; in a non-TTY or ``--json``
    context it hard-fails instead of silently defaulting -- widening an
    object's visibility across projects is not a guess this CLI makes.
    """
    engine = ctx.obj.get("permission_engine")
    if scope is None and inherit_from_model is not None and engine is not None and engine.active:
        # `add <kind>` without --scope takes its model's scope: an inherited
        # organization scope widens visibility just like a typed one, so it goes
        # through the same permission gate.
        service = get_service(ctx, "semantic_layer_service")
        inherited = _handle_service_call(
            ctx, service.child_scope, alias=owner_alias, model_name_or_uuid=inherit_from_model[0]
        )
        if inherited == "organization":
            check_cli_operation(ctx, f"{operation} --scope organization")
    if scope == "organization":
        check_cli_operation(ctx, f"{operation} --scope organization")
    if scope != "targeted":
        if target_project:
            get_formatter(ctx).error(
                message="--target-project requires --scope targeted.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)
        return None
    if target_project:
        return list(target_project)

    formatter = get_formatter(ctx)
    project_service = get_service(ctx, "project_service")
    projects = project_service.list_projects()
    owner_stack = next((p["stack_url"] for p in projects if p["alias"] == owner_alias), None)
    candidates = [
        p
        for p in projects
        if p["alias"] != owner_alias and (owner_stack is None or p["stack_url"] == owner_stack)
    ]
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
