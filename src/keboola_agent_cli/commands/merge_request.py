"""Merge-request commands -- the non-SOX Branches 2.0 lifecycle (DMD-1900).

Thin CLI layer over :class:`services.merge_request_service.MergeRequestService`:
parse flags, resolve the target, call the service, render. No business logic
here. Design record: ``docs/merge-requests-layer1.md``; the wire facts it rests
on: ``docs/merge-requests-notes.md``.

What this module decides that the service leaves to the caller:

- **Target resolution.** ``--merge-request-id`` (alias ``--id``) is optional on
  every command that takes one: omitted, the branch is resolved the house way
  (``--branch`` -> ``active_branch_id``) and the service maps it to its MR. A
  branch has at most one MR ever, so this cannot be ambiguous. See
  :func:`_resolve_target`.
- **Nothing irreversible happens without a human saying so.** ``merge`` is
  destructive; arming auto-merge IS a merge (a backend scheduler runs every
  approved MR armed with it through the same MergeProcessor), so arming
  escalates ``create``/``update`` to destructive, and ``request-review`` /
  ``approve`` / ``resolve`` on an already-armed MR escalate too -- they are
  what moves it into ``approved``. Confirmation sits where a human CHOOSES the
  outcome (``merge``, arming); escalation wherever one is CAUSED. Under
  ``--json`` a destructive invocation must name its target explicitly -- the
  prompt is gone there, and no other destructive command in kbagent lets the
  command line identify nothing (see :func:`_require_explicit_target_under_json`).
- **One error handler** (:func:`_handle_error`). ``FeatureNotEnabledError`` is a
  ``ConfigError`` with its own code and surfaces from the resolver behind every
  omitted id -- reads included -- exactly where a copied ``except ConfigError ->
  CONFIG_ERROR`` idiom would flatten it.
- **One soft-failure channel**: every result may carry ``warnings[]``; it is
  rendered the same way in every command, after the main output, before the
  hint-next line (:func:`_emit_warnings`).

Renderers live in ``_merge_request_render.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import typer

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.merge_request_service import STATE_FILTER_VOCABULARY
from ._helpers import (
    check_cli_operation,
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    resolve_project_alias,
)
from ._merge_request_render import (
    format_config_diff,
    format_conflicts_table,
    format_merge_request_detail,
    format_merge_requests_table,
    next_step_hints,
)

merge_request_app = typer.Typer(
    help=(
        "Merge requests: merge a development branch into production with review "
        "(Branches 2.0, non-SOX)"
    )
)


@merge_request_app.callback(invoke_without_command=True)
def _merge_request_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "merge-request")


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
    hint = (
        f"--merge-request-id {suggested_id}" if suggested_id else "--merge-request-id or --branch"
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


def _armed_warning(strategy: str, *, what_happened: str) -> str:
    return (
        f"Auto-merge is armed ({strategy}) -- {what_happened}, and the backend will merge "
        "this merge request into production on its next tick. Disarm with "
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


# -- Reads ------------------------------------------------------------------------------


@merge_request_app.command("list")
def merge_request_list(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    state: str | None = typer.Option(
        None,
        "--state",
        help=(
            "Show only merge requests in this state (client-side filter). Accepted: "
            + ", ".join(sorted(STATE_FILTER_VOCABULARY))
        ),
    ),
) -> None:
    """List the project's merge requests, newest first.

    Status is the derived state the web UI shows (in_development, in_review,
    approved, in_merge, merged, closed, rejected), not the raw lifecycle
    state. Single-project: pass --project or rely on the `project use` pin.
    """
    formatter = get_formatter(ctx)
    if state is not None and state.lower() not in STATE_FILTER_VOCABULARY:
        # Pre-validated here so a typo exits 2 like every other bad-enum flag
        # in kbagent, instead of reaching the service and exiting 5.
        _usage_error(
            formatter,
            f"Unknown --state value {state!r}. Accepted values: "
            f"{', '.join(sorted(STATE_FILTER_VOCABULARY))}.",
        )
    service = get_service(ctx, "merge_request_service")
    try:
        alias = resolve_project_alias(ctx, formatter, project)
        result = service.list_merge_requests(alias, state=state)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    formatter.output(result, format_merge_requests_table)
    _emit_warnings(formatter, result)
    if result.get("count"):
        _hint_next(
            formatter,
            "`merge-request detail --merge-request-id <ID>` for readiness, reviewers and conflicts",
        )
    elif result.get("feature_enabled") is not False:
        _hint_next(formatter, "`merge-request create --title ...` from your active branch")


@merge_request_app.command("detail")
def merge_request_detail(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    activity_log: bool = typer.Option(
        False, "--activity-log", help="Include the merge request's activity log"
    ),
) -> None:
    """Show one merge request: readiness, blockers, reviewers, change log, conflicts.

    Readiness (`mergeable` / `merge_blockers`) is informational -- the merge
    itself stays the authority. The change log is empty until the merge
    request is sent for review; that is the backend's behaviour, not a gap.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "merge_request_service")
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=False,
        )
        result = _stamp_target(
            service.get_merge_request(
                target.alias, target.merge_request_id, include_activity_log=activity_log
            ),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    formatter.output(result, format_merge_request_detail)
    _emit_warnings(formatter, result)
    hints = next_step_hints(result.get("allowed_actions"))
    if hints:
        _hint_next(formatter, " | ".join(f"`{h}`" for h in hints))


@merge_request_app.command("conflicts")
def merge_request_conflicts(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
) -> None:
    """List the configurations changed on both sides (computed live by the backend).

    Conflicts are re-validated on every call and on every merge attempt, so
    rebasing each listed configuration (`merge-request resolve`) is sufficient
    -- there is no separate re-validate step.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "merge_request_service")
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=False,
        )
        result = _stamp_target(
            service.list_conflicts(target.alias, target.merge_request_id), target
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    formatter.output(result, format_conflicts_table)
    _emit_warnings(formatter, result)
    conflicts = result.get("conflicts") or []
    if conflicts:
        first = conflicts[0]
        _hint_next(
            formatter,
            f"`merge-request diff --component-id {first.get('componentId')} "
            f"--config-id {first.get('configurationId')}` to see what differs, then "
            "`merge-request resolve --take ours|theirs|delete`",
        )
    else:
        _hint_next(formatter, "`merge-request merge` -- nothing blocks it on the conflict side")


@merge_request_app.command("diff")
def merge_request_diff(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    output_format: str = typer.Option(
        "short",
        "--format",
        help="short (long values elided) | full (print every value whole)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help=(
            "Write the resolution candidate (your branch's content, ready to edit) to "
            "this file; hand it back with `merge-request resolve --resolved @FILE`"
        ),
    ),
) -> None:
    """Three-way diff of one conflicting configuration, classified per path.

    Each differing path is tagged by who changed it: both (the actual
    conflict), only you, or only production. Deletions are not paths -- a
    side deleted wholesale is reported as such, with the resolution to pick.
    The branch is the merge request's own; there is no --branch of the diff.
    """
    formatter = get_formatter(ctx)
    if output_format not in ("short", "full"):
        _usage_error(formatter, f"Unknown --format value {output_format!r}: use short or full.")
    service = get_service(ctx, "merge_request_service")
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=False,
        )
        result = _stamp_target(
            service.get_config_diff(target.alias, target.merge_request_id, component_id, config_id),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    if output is not None:
        candidate = result.get("resolution_candidate")
        if candidate is None:
            # Nothing to prefill: the configuration is deleted (or absent) in
            # the branch. A skeleton here would be a misleading file kbagent
            # then refuses; the resolution for this shape is --take delete.
            _usage_error(
                formatter,
                "--output has nothing to write: the configuration is deleted in your "
                "branch, so there is no content to edit. Resolve with "
                "`merge-request resolve --take delete` (or `--take theirs` to keep "
                "production's version).",
            )
        output.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n")
        result["output_path"] = str(output)

    formatter.output(result, lambda c, d: format_config_diff(c, d, full=output_format == "full"))
    _emit_warnings(formatter, result)
    resolve_cmd = f"merge-request resolve --component-id {component_id} --config-id {config_id}"
    if output is not None:
        _hint_next(
            formatter,
            f"edit {output}, then `{resolve_cmd} --resolved @{output}`",
        )
    elif result.get("ours_deleted") or result.get("theirs_deleted"):
        _hint_next(formatter, f"`{resolve_cmd} --take delete|ours|theirs` as recommended above")
    else:
        _hint_next(
            formatter,
            f"`{resolve_cmd} --take ours|theirs`, or `merge-request diff ... --output FILE` "
            "to edit a resolution by hand",
        )
