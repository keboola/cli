"""Shared machinery of the ``kbagent merge-request`` group.

Everything ``merge_request.py`` (reads) and ``_merge_request_writes.py``
(writes) have in common, in one place so the two command modules cannot drift:
the option declarations, the ONE error handler, target resolution, the
destructive-under-json rule and the auto-merge escalation, and the output
helpers. Design record: ``docs/merge-requests-layer1.md``.

Split out of ``merge_request.py`` when the group crossed the 800-code-line soft
ceiling (eleven commands at ~75 lines each, as the RFC predicted). A third
module rather than reads importing from writes or vice versa: both command
modules import from here and only ``merge_request.py`` imports the writes, so
there is no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_operation,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    resolve_project_alias,
)
from ._merge_request_render import next_step_hints

# -- Shared option declarations ------------------------------------------------
#
# Reused across commands so the help text and the flag names cannot drift
# between them. `--merge-request-id`/`--id`: a merge request is the OBJECT the
# command acts on, hence `--<noun>-id` like --config-id/--table-id (the bare
# nouns --project/--branch are the CONTEXT you work in); `--id` is the short
# alias the `agent` group already established beside `--task-id`.

_PROJECT_OPT = typer.Option(
    None,
    "--project",
    help="Project alias (default: KBAGENT_PROJECT, then the `project use` pin, then the sole project)",
)
_MERGE_REQUEST_ID_OPT = typer.Option(
    None,
    "--merge-request-id",
    "--id",
    help=(
        "Merge request ID. Omit to use the merge request of --branch, or of the "
        "active branch (`branch use`)"
    ),
)
_BRANCH_OPT = typer.Option(
    None,
    "--branch",
    help=(
        "Dev branch ID whose merge request to use (default: the active branch set "
        "via `branch use`). Mutually exclusive with --merge-request-id"
    ),
)

_AUTO_MERGE_DISARMED = "none"


# -- Error handling ---------------------------------------------------------------


def _handle_error(formatter: Any, exc: ConfigError | KeboolaApiError) -> NoReturn:
    """The group's ONE ``ConfigError``/``KeboolaApiError`` -> exit-code mapping.

    No command in this module has its own ``except``: eleven inline copies of
    the idiom are eleven places to flatten ``FeatureNotEnabledError`` (a
    ``ConfigError`` subclass carrying ``FEATURE_NOT_ENABLED``) into a bare
    ``CONFIG_ERROR``, which is what ``commands/config.py``'s
    ``_handle_config_service_error`` does and what a ``--json`` consumer cannot
    recover from. Same shape as that helper, corrected code lookup -- the
    pattern ``server/app.py``'s ConfigError handler already uses.
    """
    if isinstance(exc, ConfigError):
        formatter.error(
            message=exc.message,
            error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR),
        )
        raise typer.Exit(code=5) from None
    formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
    raise typer.Exit(code=map_error_to_exit_code(exc)) from None


def _usage_error(formatter: Any, message: str) -> NoReturn:
    formatter.error(message=message, error_code=ErrorCode.INVALID_ARGUMENT)
    raise typer.Exit(code=2)


# -- Target resolution -------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """What a command operates on, and how that was decided.

    ``row`` is the service's enriched MR row (raw + ``derived_state`` +
    ``allowed_actions``). It is always present when the target was resolved
    from a branch (``find_merge_request_for_branch`` returns it for free) and
    fetched on demand (``need_row``) when the id was explicit -- one GET via
    ``get_merge_request_row``, never the three-call detail.
    """

    alias: str
    merge_request_id: int
    row: dict[str, Any] | None
    branch_id: int | None
    resolved_from_branch: bool

    @property
    def auto_merge_strategy(self) -> str:
        """``immediately`` | ``scheduled`` | ``none``; ``none`` when unknown."""
        if not self.row:
            return _AUTO_MERGE_DISARMED
        return str(self.row.get("autoMergeStrategy") or _AUTO_MERGE_DISARMED)

    @property
    def armed(self) -> bool:
        return self.auto_merge_strategy != _AUTO_MERGE_DISARMED


def _branch_from_row(row: dict[str, Any] | None) -> int | None:
    raw = ((row or {}).get("branches") or {}).get("branchFromId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_target(
    ctx: typer.Context,
    formatter: Any,
    *,
    project: str | None,
    merge_request_id: int | None,
    branch: int | None,
    need_row: bool,
) -> _Target:
    """Resolve which merge request a command operates on.

    1. ``--merge-request-id`` given -> that (``row`` fetched only if ``need_row``).
    2. Else ``resolve_branch()``: explicit ``--branch``, else ``active_branch_id``.
    3. On that branch, ``find_merge_request_for_branch()`` -> the MR (and its row).

    Both flags at once is exit 2, not silent precedence: they are two ways of
    naming one target, and a contradiction (MR 7 not being FROM branch 123) is
    exactly what a ``--json`` script would never notice. With neither and no
    active branch, the house wording: pass ``--branch`` or run ``branch use``.
    Service errors propagate -- the caller's ``except`` routes them through
    :func:`_handle_error` (a feature-less project surfaces here as
    ``FEATURE_NOT_ENABLED``, since the resolver runs the feature pre-flight on
    its no-match path).
    """
    if merge_request_id is not None and branch is not None:
        _usage_error(
            formatter,
            "Pass either --merge-request-id or --branch, not both -- they are two ways "
            "of naming the same merge request.",
        )
    alias = resolve_project_alias(ctx, formatter, project)
    service = get_service(ctx, "merge_request_service")

    if merge_request_id is not None:
        row = service.get_merge_request_row(alias, merge_request_id) if need_row else None
        return _Target(
            alias=alias,
            merge_request_id=merge_request_id,
            row=row,
            branch_id=_branch_from_row(row),
            resolved_from_branch=False,
        )

    config_store = get_service(ctx, "config_store")
    _, branch_id = resolve_branch(config_store, formatter, alias, branch)
    if branch_id is None:
        formatter.error(
            message=(
                f"No merge request selected for project '{alias}': pass "
                "--merge-request-id, or --branch, or run `kbagent branch use` first."
            ),
            error_code=ErrorCode.CONFIG_ERROR,
        )
        raise typer.Exit(code=5)
    row = service.find_merge_request_for_branch(alias, branch_id)
    resolved_id = int(row["id"])
    if not formatter.json_mode:
        formatter.err_console.print(
            f"[bold blue]Info:[/bold blue] Resolved merge request #{resolved_id} "
            f"from branch {branch_id}"
        )
    return _Target(
        alias=alias,
        merge_request_id=resolved_id,
        row=row,
        branch_id=branch_id,
        resolved_from_branch=True,
    )


def _stamp_target(result: dict[str, Any], target: _Target) -> dict[str, Any]:
    """Add the target facts every ``--json`` result carries regardless of how
    the target was reached -- so a machine caller can always assert on what was
    actually operated upon. Never overwrites a key the service already set."""
    result.setdefault("merge_request_id", target.merge_request_id)
    result.setdefault("branch_from_id", target.branch_id)
    result.setdefault("resolved_from_branch", target.resolved_from_branch)
    return result


# -- Destructive-under-json rule and auto-merge escalation ----------------------


def _require_explicit_target_under_json(
    formatter: Any,
    *,
    merge_request_id: int | None,
    branch: int | None,
    reason: str,
    suggested_id: int | None = None,
    hint: str | None = None,
) -> None:
    """When an invocation resolves to the destructive class, ``--json`` requires
    an explicit target.

    Every destructive command in kbagent either prompts or is told its target;
    none relies on the prompt for machine safety (``--json`` implies consent
    in all 48 commands carrying ``--yes``). A bare ``--json merge-request
    merge`` would do neither -- the first command where nothing on the command
    line identifies what gets destroyed. Humans keep the active-branch
    fallback and get the prompt; a script, which received the id in its
    previous call's payload, names it.
    """
    if not formatter.json_mode or merge_request_id is not None or branch is not None:
        return
    if hint is None:
        hint = (
            f"--merge-request-id {suggested_id}"
            if suggested_id
            else "--merge-request-id or --branch"
        )
    _usage_error(
        formatter,
        f"{reason} Under --json a destructive operation needs an explicit target: pass {hint}.",
    )


def _escalate_if_armed(
    ctx: typer.Context,
    formatter: Any,
    target: _Target,
    *,
    operation: str,
    merge_request_id: int | None,
    branch: int | None,
) -> str | None:
    """Apply the state-derived destructive escalation for an armed MR.

    Returns the strategy (``immediately`` / ``scheduled``) when the MR is armed
    so the caller can say so in its output, or ``None``. Order matters: the
    policy check comes first (a denial is the stronger statement -- telling a
    denied caller to "pass --merge-request-id" would not help), then the
    ``--json`` explicit-target rule. That rule can only fire AFTER resolution
    here -- whether the invocation is destructive is only known from the
    fetched row; the check cannot move earlier because the information does
    not exist earlier. One wasted round trip on the rare path is the price of
    a rule with no exceptions.
    """
    if not target.armed:
        return None
    check_cli_operation(ctx, f"merge-request.{operation} --auto-merge-armed")
    _require_explicit_target_under_json(
        formatter,
        merge_request_id=merge_request_id,
        branch=branch,
        reason=(
            f"Merge request #{target.merge_request_id} has auto-merge armed "
            f"({target.auto_merge_strategy}), so `{operation}` will cause a production merge."
        ),
        suggested_id=target.merge_request_id,
    )
    return target.auto_merge_strategy


def _armed_warning(strategy: str, result: dict[str, Any]) -> str:
    """What an armed MR means right now, phrased from the resulting state:
    approved -> the backend merges on its next tick; anything else -> it will,
    the moment the MR is approved. Always names the disarm."""
    state = str(result.get("state") or "")
    when = (
        "the backend will merge it into production on its next tick"
        if state == "approved"
        else "the backend will merge it into production as soon as it is approved"
    )
    return (
        f"Auto-merge is armed ({strategy}) -- {when}. Disarm with "
        "`merge-request update --auto-merge-strategy none` if that is not intended."
    )


# -- Output helpers ----------------------------------------------------------------


def _emit_warnings(formatter: Any, result: dict[str, Any]) -> None:
    """Render ``warnings[]`` -- the group's one soft-failure key -- in human mode.
    In ``--json`` the key is in the payload; ``formatter.warning`` is human-only."""
    for warning in result.get("warnings") or []:
        formatter.warning(str(warning))


def _hint_next(formatter: Any, text: str) -> None:
    """The one-line next step every command ends with in human mode. Rich-only:
    ``--json`` consumers have ``allowed_actions`` as data on every result."""
    if not formatter.json_mode:
        formatter.console.print(f"[dim]Next:[/dim] {text}")


def _hint_from_actions(formatter: Any, result: dict[str, Any]) -> None:
    hints = next_step_hints(result.get("allowed_actions"))
    if hints:
        _hint_next(formatter, " | ".join(f"`{h}`" for h in hints))


def _print_row_success(formatter: Any, result: dict[str, Any], headline: str) -> None:
    def render(c: Any, d: dict[str, Any]) -> None:
        state = str(d.get("derived_state") or d.get("state") or "").replace("_", " ")
        c.print(f"[bold green]Success:[/bold green] {headline} -- state: {state}")

    formatter.output(result, render)
