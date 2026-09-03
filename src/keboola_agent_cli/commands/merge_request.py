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

Renderers live in ``_merge_request_render.py``; the shared machinery in
``_merge_request_common.py``; the write commands in ``_merge_request_writes.py``
(mounted onto this app at the bottom) -- the group crossed the 800-code-line
soft ceiling, as the RFC predicted for eleven commands.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.markup import escape

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.merge_request_service import STATE_FILTER_VOCABULARY
from ._helpers import check_cli_permission, get_formatter, get_service, resolve_project_alias
from ._merge_request_common import (
    _BRANCH_OPT,
    _MERGE_REQUEST_ID_OPT,
    _PROJECT_OPT,
    _emit_warnings,
    _handle_error,
    _hint_next,
    _resolve_target,
    _stamp_target,
    _usage_error,
)
from ._merge_request_render import (
    format_config_diff,
    format_conflicts_table,
    format_merge_request_detail,
    format_merge_requests_table,
    next_step_hints,
)
from ._merge_request_writes import register as _register_write_commands

merge_request_app = typer.Typer(
    help=(
        "Merge requests: merge a development branch into production with review "
        "(Branches 2.0, non-SOX)"
    )
)


@merge_request_app.callback(invoke_without_command=True)
def _merge_request_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "merge-request")


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
    if result.get("feature_enabled") is False:
        # State-derived actions are feature-blind; the detail knows better
        # (followups F5) and must not recommend a write that cannot succeed.
        _hint_next(
            formatter,
            "none of the write actions can succeed here -- 'branches-merge-requests' is not "
            "enabled on this project",
        )
        return
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
            need_row=True,
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
            f"`merge-request diff --component-id {escape(str(first.get('componentId')))} "
            f"--config-id {escape(str(first.get('configurationId')))}` to see what differs, "
            "then `merge-request resolve --take ours|theirs|delete`",
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
            # Nothing to prefill. Three shapes, said apart: deleted in the
            # branch (resolve with --take delete/theirs), absent from the
            # branch entirely, or a hole in the server envelope (the service
            # explains in warnings). A skeleton here would be a misleading
            # file kbagent then refuses.
            if result.get("ours_deleted") is True:
                why = (
                    "the configuration is deleted in your branch, so there is no content "
                    "to edit. Resolve with `merge-request resolve --take delete` (or "
                    "`--take theirs` to keep production's version)."
                )
            elif result.get("ours_deleted") is None:
                why = "the configuration does not exist in your branch."
            else:
                why = "; ".join(result.get("warnings") or ["no candidate could be composed."])
            _usage_error(formatter, f"--output has nothing to write: {why}")
        try:
            output.write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            formatter.error(
                message=f"Cannot write --output {output}: {exc}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None
        result["output_path"] = str(output)

    formatter.output(result, lambda c, d: format_config_diff(c, d, full=output_format == "full"))
    _emit_warnings(formatter, result)
    resolve_cmd = f"merge-request resolve --component-id {escape(component_id)} --config-id {escape(config_id)}"
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


# Writes are declared in _merge_request_writes.py (file-size budget) and mounted
# here so `kbagent merge-request --help` lists the whole group in one place.
_register_write_commands(merge_request_app)
