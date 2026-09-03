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
from ..services.merge_request_service import STATE_FILTER_VOCABULARY, TAKE_MODES
from ._helpers import (
    check_cli_operation,
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    parse_json_arg,
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


# -- Writes -----------------------------------------------------------------------------

_AUTO_MERGE_STRATEGIES = ("immediately", "scheduled", _AUTO_MERGE_DISARMED)
_REASON_MAX_LENGTH = 1000  # MergeRequestRejectRequest::REASON_MAX_LENGTH, server-side cap

_YES_OPT = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt")
_TITLE_OPT = typer.Option(None, "--title", help="Merge request title")
_DESCRIPTION_OPT = typer.Option(
    None, "--description", help="Description (on update: an empty string clears it)"
)
_REVIEWER_OPT = typer.Option(
    None,
    "--reviewer-id",
    help=(
        "Reviewer user ID (repeatable; ids from `project member-list`). On update the "
        "given set REPLACES the current reviewers -- it never appends"
    ),
)
_AUTO_MERGE_STRATEGY_OPT = typer.Option(
    None,
    "--auto-merge-strategy",
    help=(
        "immediately | scheduled | none. ARMING (immediately/scheduled) is a destructive "
        "operation: once the merge request is approved, the backend merges it into "
        "production on its own -- no `merge` call involved. `none` disarms"
    ),
)
_AUTO_MERGE_AT_OPT = typer.Option(
    None,
    "--auto-merge-at",
    help="When to auto-merge (ISO 8601); required with --auto-merge-strategy scheduled",
)
_EXTERNAL_ID_OPT = typer.Option(
    None, "--external-id", help="Free-form correlation id, e.g. a ticket (max 255 chars)"
)


def _validate_auto_merge_flags(formatter: Any, strategy: str | None, at: str | None) -> bool:
    """Exit 2 on a bad strategy or a broken strategy/at pairing; return whether
    the flags ARM auto-merge (strategy given and not `none`)."""
    if strategy is not None and strategy not in _AUTO_MERGE_STRATEGIES:
        _usage_error(
            formatter,
            f"Unknown --auto-merge-strategy {strategy!r}: use {', '.join(_AUTO_MERGE_STRATEGIES)}.",
        )
    if strategy == "scheduled" and not at:
        _usage_error(formatter, "--auto-merge-strategy scheduled requires --auto-merge-at.")
    if at is not None and strategy != "scheduled":
        _usage_error(
            formatter,
            "--auto-merge-at is only meaningful with --auto-merge-strategy scheduled.",
        )
    return strategy is not None and strategy != _AUTO_MERGE_DISARMED


def _confirm_or_abort(formatter: Any, yes: bool, question: str) -> None:
    """The house prompt shape: skipped by --yes and in --json (where consent is
    implied and the explicit-target rule stands in for it)."""
    if yes or formatter.json_mode:
        return
    if not typer.confirm(question):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)


def _arming_question(strategy: str, at: str | None, *, subject: str) -> str:
    when = f" at {at}" if at else ""
    return (
        f"Arm auto-merge ({strategy}{when}) on {subject}? Once it is approved, the backend "
        "will merge it into production automatically -- without a `merge` call. Continue?"
    )


def _print_row_success(formatter: Any, result: dict[str, Any], headline: str) -> None:
    def render(c: Any, d: dict[str, Any]) -> None:
        state = str(d.get("derived_state") or d.get("state") or "").replace("_", " ")
        c.print(f"[bold green]Success:[/bold green] {headline} -- state: {state}")

    formatter.output(result, render)


def _hint_from_actions(formatter: Any, result: dict[str, Any]) -> None:
    hints = next_step_hints(result.get("allowed_actions"))
    if hints:
        _hint_next(formatter, " | ".join(f"`{h}`" for h in hints))


@merge_request_app.command("create")
def merge_request_create(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    title: str = typer.Option(..., "--title", help="Merge request title"),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Source dev branch ID (default: the active branch set via `branch use`)",
    ),
    description: str | None = _DESCRIPTION_OPT,
    reviewer_id: list[int] | None = _REVIEWER_OPT,
    auto_merge_strategy: str | None = _AUTO_MERGE_STRATEGY_OPT,
    auto_merge_at: str | None = _AUTO_MERGE_AT_OPT,
    external_id: str | None = _EXTERNAL_ID_OPT,
    yes: bool = _YES_OPT,
) -> None:
    """Open a merge request from a development branch into production.

    The target is always the default branch; the source is --branch or the
    active branch. A branch can have one merge request, ever. On a non-SOX
    project with 0 required approvals you can `merge` straight from here --
    no `request-review` needed.
    """
    formatter = get_formatter(ctx)
    arming = _validate_auto_merge_flags(formatter, auto_merge_strategy, auto_merge_at)
    if arming:
        # Arming IS a (delayed) production merge: destructive, and under
        # --json it must name its target -- here the source branch.
        check_cli_operation(ctx, "merge-request.create --auto-merge-strategy")
        _require_explicit_target_under_json(
            formatter,
            merge_request_id=None,
            branch=branch,
            reason="--auto-merge-strategy arms an automatic production merge.",
            hint="--branch",
        )
    service = get_service(ctx, "merge_request_service")
    try:
        alias = resolve_project_alias(ctx, formatter, project)
        config_store = get_service(ctx, "config_store")
        _, branch_id = resolve_branch(config_store, formatter, alias, branch)
        if branch_id is None:
            formatter.error(
                message=(
                    f"No source branch for project '{alias}': pass --branch or run "
                    "`kbagent branch use` first."
                ),
                error_code=ErrorCode.CONFIG_ERROR,
            )
            raise typer.Exit(code=5)
        if arming:
            _confirm_or_abort(
                formatter,
                yes,
                _arming_question(
                    str(auto_merge_strategy),
                    auto_merge_at,
                    subject=f"the new merge request from branch {branch_id}",
                ),
            )
        result = service.create_merge_request(
            alias,
            branch_from_id=branch_id,
            title=title,
            description=description,
            reviewer_ids=reviewer_id or None,  # never [] -- that REPLACES the set with nothing
            auto_merge_strategy=auto_merge_strategy,
            auto_merge_at=auto_merge_at,
            external_id=external_id,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    result.setdefault("merge_request_id", result.get("id"))
    result.setdefault("resolved_from_branch", branch is None)
    if arming:
        result.setdefault("warnings", []).append(_armed_warning(str(auto_merge_strategy), result))
    _print_row_success(
        formatter,
        result,
        f"Created merge request #{result.get('id')} from branch {branch_id}",
    )
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


@merge_request_app.command("update")
def merge_request_update(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    title: str | None = _TITLE_OPT,
    description: str | None = _DESCRIPTION_OPT,
    reviewer_id: list[int] | None = _REVIEWER_OPT,
    auto_merge_strategy: str | None = _AUTO_MERGE_STRATEGY_OPT,
    auto_merge_at: str | None = _AUTO_MERGE_AT_OPT,
    external_id: str | None = _EXTERNAL_ID_OPT,
    yes: bool = _YES_OPT,
) -> None:
    """Change a merge request's title, description, reviewers, auto-merge or external id.

    Omitted fields stay as they are; an empty string clears --description /
    --external-id. --reviewer-id replaces the whole reviewer set.
    """
    formatter = get_formatter(ctx)
    fields = (
        title,
        description,
        reviewer_id or None,
        auto_merge_strategy,
        auto_merge_at,
        external_id,
    )
    if all(f is None for f in fields):
        # PUT {} is a server-side no-op that answers 200 -- refuse instead of
        # reporting success having changed nothing.
        _usage_error(formatter, "Nothing to update: pass at least one field flag.")
    arming = _validate_auto_merge_flags(formatter, auto_merge_strategy, auto_merge_at)
    if arming:
        check_cli_operation(ctx, "merge-request.update --auto-merge-strategy")
        _require_explicit_target_under_json(
            formatter,
            merge_request_id=merge_request_id,
            branch=branch,
            reason="--auto-merge-strategy arms an automatic production merge.",
        )
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
        if arming:
            _confirm_or_abort(
                formatter,
                yes,
                _arming_question(
                    str(auto_merge_strategy),
                    auto_merge_at,
                    subject=f"merge request #{target.merge_request_id}",
                ),
            )
        result = _stamp_target(
            service.update_merge_request(
                target.alias,
                target.merge_request_id,
                title=title,
                description=description,
                reviewer_ids=reviewer_id or None,
                auto_merge_strategy=auto_merge_strategy,
                auto_merge_at=auto_merge_at,
                external_id=external_id,
            ),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    if arming:
        result.setdefault("warnings", []).append(_armed_warning(str(auto_merge_strategy), result))
    _print_row_success(formatter, result, f"Updated merge request #{target.merge_request_id}")
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


def _transition(
    ctx: typer.Context,
    *,
    operation: str,
    project: str | None,
    merge_request_id: int | None,
    branch: int | None,
    escalate_when_armed: bool,
    call: Any,
    headline: str,
) -> None:
    """Shared body of request-review / approve / request-changes.

    ``escalate_when_armed`` is True for the two that move an MR toward
    ``approved`` (what an armed auto-merge waits for); request-changes moves
    it AWAY and deletes approvals, so it never escalates.
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
            need_row=escalate_when_armed,
        )
        strategy = (
            _escalate_if_armed(
                ctx,
                formatter,
                target,
                operation=operation,
                merge_request_id=merge_request_id,
                branch=branch,
            )
            if escalate_when_armed
            else None
        )
        result = _stamp_target(call(service, target), target)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    if strategy:
        result.setdefault("warnings", []).append(_armed_warning(strategy, result))
    _print_row_success(formatter, result, headline.format(id=target.merge_request_id))
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


@merge_request_app.command("request-review")
def merge_request_request_review(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
) -> None:
    """Send the merge request for review.

    On a non-SOX project with 0 required approvals (the default) the backend
    finishes the review itself and the merge request lands directly in
    `approved` -- so `merge` works straight from `development` and this step
    is optional. Note: with no reviewers selected, the review-requested email
    goes to every project member.
    """
    _transition(
        ctx,
        operation="request-review",
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        escalate_when_armed=True,
        call=lambda s, t: s.request_review(t.alias, t.merge_request_id),
        headline="Review requested for merge request #{id}",
    )


@merge_request_app.command("approve")
def merge_request_approve(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
) -> None:
    """Add your approval to a merge request under review.

    Only possible while the merge request is `in_review`. On a non-SOX project
    with 0 required approvals (the default) that state is never reached --
    `request-review` jumps straight to `approved` -- so this command answers
    422 there. It exists for projects that require approvals.
    """
    _transition(
        ctx,
        operation="approve",
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        escalate_when_armed=True,
        call=lambda s, t: s.approve(t.alias, t.merge_request_id),
        headline="Approved merge request #{id}",
    )


@merge_request_app.command("request-changes")
def merge_request_request_changes(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    reason: str | None = typer.Option(
        None, "--reason", help=f"Why (max {_REASON_MAX_LENGTH} characters)"
    ),
) -> None:
    """Send the merge request back to development; existing approvals are removed.

    This is also the closest thing to closing a merge request: the API has no
    cancel, and the web UI's "cancel" is exactly this call made by the
    creator. The merge request stays open in `development` and can be
    resubmitted; deleting the branch is the terminal outcome.
    """
    formatter = get_formatter(ctx)
    if reason is not None and len(reason) > _REASON_MAX_LENGTH:
        _usage_error(
            formatter, f"--reason is capped at {_REASON_MAX_LENGTH} characters (got {len(reason)})."
        )
    _transition(
        ctx,
        operation="request-changes",
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        escalate_when_armed=False,
        call=lambda s, t: s.request_changes(t.alias, t.merge_request_id, reason=reason),
        headline="Changes requested on merge request #{id}",
    )


@merge_request_app.command("merge")
def merge_request_merge(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    yes: bool = _YES_OPT,
) -> None:
    """Merge the merge request into production and delete its source branch.

    Waits for the merge job (up to 10 minutes). Works straight from
    `development` when approvals are satisfied. The source branch is always
    deleted afterwards (a separate async job). Under --json the target must be
    explicit: pass --merge-request-id or --branch.
    """
    formatter = get_formatter(ctx)
    # Statically destructive: the explicit-target rule applies before any lookup.
    _require_explicit_target_under_json(
        formatter,
        merge_request_id=merge_request_id,
        branch=branch,
        reason="`merge-request merge` rewrites production and deletes the source branch.",
    )
    service = get_service(ctx, "merge_request_service")
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=True,
        )
        title = (target.row or {}).get("title") or ""
        _confirm_or_abort(
            formatter,
            yes,
            f"Merge request #{target.merge_request_id} '{title}' will be merged into "
            f"production and its source branch {target.branch_id} deleted. Continue?",
        )
        result = _stamp_target(service.merge(target.alias, target.merge_request_id), target)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    formatter.output(
        result, lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}")
    )
    _emit_warnings(formatter, result)
    _hint_next(formatter, "`merge-request list` -- the merged request now shows as merged")


@merge_request_app.command("resolve")
def merge_request_resolve(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
    take: str | None = typer.Option(
        None,
        "--take",
        help=(
            "ours (keep your branch's content) | theirs (adopt production's) | delete. "
            "Mutually exclusive with --resolved"
        ),
    ),
    resolved: str | None = typer.Option(
        None,
        "--resolved",
        help=(
            "A hand-authored resolution: JSON inline, @file, or - for stdin. Start from "
            "`merge-request diff --output FILE`; the body must carry name, description, "
            "isDisabled, configuration and rows (rebase REPLACES the whole configuration)"
        ),
    ),
    change_description: str | None = typer.Option(
        None, "--change-description", help="Version message for the rebased configuration"
    ),
) -> None:
    """Resolve one conflicting configuration by rebasing it onto production's version.

    Every mode replaces the configuration in your branch; the previous content
    stays in its version history. Rebasing each listed conflict makes the merge
    request mergeable -- there is no re-validate step. There is deliberately no
    --all: conflicts are meant to be walked, not waved away.
    """
    formatter = get_formatter(ctx)
    if (take is None) == (resolved is None):
        _usage_error(formatter, "Pass exactly one of --take ours|theirs|delete or --resolved.")
    if take is not None and take not in TAKE_MODES:
        _usage_error(formatter, f"Unknown --take value {take!r}: use {', '.join(TAKE_MODES)}.")
    body: dict[str, Any] | None = None
    if resolved is not None:
        try:
            parsed = parse_json_arg(resolved, label="--resolved")
        except ValueError as exc:
            _usage_error(formatter, str(exc))
        if not isinstance(parsed, dict):
            _usage_error(
                formatter, "--resolved must be a JSON object (the replaced configuration body)."
            )
        body = parsed
    service = get_service(ctx, "merge_request_service")
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=True,
        )
        # Resolving the last conflict on an armed, approved MR unblocks the
        # scheduler's retry loop -- it causes the merge as surely as approve does.
        strategy = _escalate_if_armed(
            ctx,
            formatter,
            target,
            operation="resolve",
            merge_request_id=merge_request_id,
            branch=branch,
        )
        result = _stamp_target(
            service.resolve_conflict(
                target.alias,
                target.merge_request_id,
                component_id,
                config_id,
                take=take,
                resolved=body,
                change_description=change_description,
            ),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    if strategy:
        result.setdefault("warnings", []).append(_armed_warning(strategy, result))
    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Success:[/bold green] Resolved {component_id}/{config_id} "
            f"({d.get('resolution')}) -- rebased onto production version {d.get('onto_version')}"
        ),
    )
    _emit_warnings(formatter, result)
    _hint_next(
        formatter,
        "`merge-request conflicts` for what is left, then `merge-request merge`",
    )
