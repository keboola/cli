"""Write commands of the ``kbagent merge-request`` group.

``create`` / ``update`` / ``request-review`` / ``approve`` / ``request-changes`` /
``merge`` / ``resolve`` / ``auto-merge`` -- split out of ``merge_request.py`` when
the group crossed the 800-code-line soft ceiling. Mounted flat onto the group's
Typer app via :func:`register`, so permission keys stay in the ``merge-request.*``
namespace and ``--help`` lists them with the reads. Registration goes through
Typer's public ``app.command(name)(fn)``; ``_storage_describe.register`` reaches
the same flat mount by declaring its commands inside ``register`` -- either way,
no Typer internals.

Every command here is in one of two static classes and behaves accordingly:

- **write** (``create``, ``update``, ``request-changes``): resolve the target,
  call the service, report. No prompt, no target rule.
- **destructive** (``request-review``, ``approve``, ``resolve``, ``merge``,
  ``auto-merge``): the ``--json`` explicit-target rule runs FIRST, before any
  network call, because the class is known from the command name alone; the
  two that a human must consciously choose (``merge``, arming ``auto-merge``)
  additionally prompt in human mode.

Shared machinery -- target resolution, the one error handler, the target rule --
lives in ``_merge_request_common.py``; this module only decides what each write
asks, confirms, and says afterwards. Design record: ``docs/merge-requests-layer1.md``.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape

from ..constants import MERGE_REQUEST_EXTERNAL_ID_MAX_LENGTH, MERGE_REQUEST_REASON_MAX_LENGTH
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.merge_request_service import (
    AUTO_MERGE_DISARMED,
    AUTO_MERGE_STRATEGIES,
    TAKE_MODES,
    arms_auto_merge,
    validate_auto_merge_flags,
)
from ._helpers import (
    get_formatter,
    get_service,
    parse_json_arg,
    resolve_branch,
    resolve_project_alias,
)
from ._merge_request_common import (
    _BRANCH_OPT,
    _MERGE_REQUEST_ID_OPT,
    _PROJECT_OPT,
    _emit_warnings,
    _handle_error,
    _hint_from_actions,
    _hint_next,
    _print_row_success,
    _require_explicit_target_under_json,
    _resolve_target,
    _stamp_target,
    _usage_error,
)

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
_EXTERNAL_ID_OPT = typer.Option(
    None,
    "--external-id",
    help=f"Free-form correlation id, e.g. a ticket (max {MERGE_REQUEST_EXTERNAL_ID_MAX_LENGTH} chars)",
)


def _confirm_or_abort(formatter: Any, yes: bool, question: str) -> None:
    """The house prompt shape: skipped by --yes and in --json (where consent is
    implied and the explicit-target rule stands in for it)."""
    if yes or formatter.json_mode:
        return
    if not typer.confirm(question):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)


def _warn_if_armed(formatter: Any, result: dict[str, Any]) -> None:
    """After a destructive transition: if the MR is armed for auto-merge, say what
    that means right now. Read off the RESULT the service returned (the enriched
    row carries ``autoMergeStrategy``) -- no extra GET, no payload injection;
    a --json consumer reads the same field. Phrased from the resulting state:
    approved -> the backend merges on its next tick; anything else -> it will,
    the moment the MR is approved."""
    strategy = result.get("autoMergeStrategy")
    if not arms_auto_merge(strategy):
        return
    state = str(result.get("state") or "")
    when = (
        "the backend will merge it into production on its next tick"
        if state == "approved"
        else "the backend will merge it into production as soon as it is approved"
    )
    formatter.warning(
        f"Auto-merge is armed ({strategy}) -- {when}. Disarm with "
        f"`merge-request auto-merge --strategy {AUTO_MERGE_DISARMED}` if that is not intended."
    )


# -- Writes: shape the MR without moving it -------------------------------------------


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
    external_id: str | None = _EXTERNAL_ID_OPT,
) -> None:
    """Open a merge request from a development branch into production.

    The target is always the default branch; the source is --branch or the
    active branch. A branch can have one merge request, ever. On a non-SOX
    project with 0 required approvals you can `merge` straight from here --
    no `request-review` needed. Auto-merge is a separate, destructive step:
    `merge-request auto-merge`.
    """
    formatter = get_formatter(ctx)
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
        result = service.create_merge_request(
            alias,
            branch_from_id=branch_id,
            title=title,
            description=description,
            reviewer_ids=reviewer_id or None,  # never [] -- that REPLACES the set with nothing
            external_id=external_id,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    result.setdefault("merge_request_id", result.get("id"))
    result.setdefault("resolved_from_branch", branch is None)
    _print_row_success(
        formatter,
        result,
        f"Created merge request #{result.get('id')} from branch {branch_id}",
    )
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


def merge_request_update(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    title: str | None = _TITLE_OPT,
    description: str | None = _DESCRIPTION_OPT,
    reviewer_id: list[int] | None = _REVIEWER_OPT,
    external_id: str | None = _EXTERNAL_ID_OPT,
) -> None:
    """Change a merge request's title, description, reviewers or external id.

    Omitted fields stay as they are; an empty string clears --description /
    --external-id. --reviewer-id replaces the whole reviewer set. Auto-merge
    is not a field here -- it is the destructive `merge-request auto-merge`.
    """
    formatter = get_formatter(ctx)
    if all(f is None for f in (title, description, reviewer_id or None, external_id)):
        # PUT {} is a server-side no-op that answers 200 -- refuse instead of
        # reporting success having changed nothing.
        _usage_error(formatter, "Nothing to update: pass at least one field flag.")
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
            service.update_merge_request(
                target.alias,
                target.merge_request_id,
                title=title,
                description=description,
                reviewer_ids=reviewer_id or None,
                external_id=external_id,
            ),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    _print_row_success(formatter, result, f"Updated merge request #{target.merge_request_id}")
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


# -- Transitions ----------------------------------------------------------------------------


def _transition(
    ctx: typer.Context,
    *,
    destructive_reason: str | None,
    project: str | None,
    merge_request_id: int | None,
    branch: int | None,
    call: Any,
    headline: str,
) -> None:
    """Shared body of request-review / approve / request-changes.

    ``destructive_reason`` is set for the two that move an MR toward
    ``approved`` -- they are in the destructive class, so under ``--json`` the
    target must be explicit, checked here BEFORE any network call.
    request-changes moves the MR away from approved and is a plain write.
    """
    formatter = get_formatter(ctx)
    if destructive_reason:
        _require_explicit_target_under_json(
            formatter,
            merge_request_id=merge_request_id,
            branch=branch,
            reason=destructive_reason,
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
        result = _stamp_target(call(service, target), target)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    _print_row_success(formatter, result, headline.format(id=target.merge_request_id))
    if destructive_reason:
        _warn_if_armed(formatter, result)
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


def merge_request_request_review(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
) -> None:
    """Send the merge request for review (destructive: it moves the MR toward production).

    On a non-SOX project with 0 required approvals (the default) the backend
    finishes the review itself and the merge request lands directly in
    `approved` -- where an armed auto-merge fires, and `merge` needs nothing
    more. `merge` works straight from `development` there, so this step is
    optional. Note: with no reviewers selected, the review-requested email
    goes to every project member. Under --json the target must be explicit.
    """
    _transition(
        ctx,
        destructive_reason=(
            "`merge-request request-review` moves the merge request toward production "
            "(on a 0-approval project it lands directly in `approved`)."
        ),
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        call=lambda s, t: s.request_review(t.alias, t.merge_request_id),
        headline="Review requested for merge request #{id}",
    )


def merge_request_approve(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
) -> None:
    """Add your approval (destructive: the last approval is what a merge waits for).

    Only possible while the merge request is `in_review`. On a non-SOX project
    with 0 required approvals (the default) that state is never reached --
    `request-review` jumps straight to `approved` -- so this command answers
    422 there. It exists for projects that require approvals. Under --json the
    target must be explicit.
    """
    _transition(
        ctx,
        destructive_reason=(
            "`merge-request approve` moves the merge request toward production "
            "(the last approval is what a merge -- or an armed auto-merge -- waits for)."
        ),
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        call=lambda s, t: s.approve(t.alias, t.merge_request_id),
        headline="Approved merge request #{id}",
    )


def merge_request_request_changes(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    reason: str | None = typer.Option(
        None, "--reason", help=f"Why (max {MERGE_REQUEST_REASON_MAX_LENGTH} characters)"
    ),
) -> None:
    """Send the merge request back to development; existing approvals are removed.

    This is also the closest thing to closing a merge request: the API has no
    cancel, and the web UI's "cancel" is exactly this call made by the
    creator. The merge request stays open in `development` and can be
    resubmitted; deleting the branch is the terminal outcome.
    """
    formatter = get_formatter(ctx)
    # The service validates the cap (one constant, one rule); this pre-check
    # exists only so the flag error carries exit 2 like every other bad flag.
    if reason is not None and len(reason) > MERGE_REQUEST_REASON_MAX_LENGTH:
        _usage_error(
            formatter,
            f"--reason is capped at {MERGE_REQUEST_REASON_MAX_LENGTH} characters (got {len(reason)}).",
        )
    _transition(
        ctx,
        destructive_reason=None,
        project=project,
        merge_request_id=merge_request_id,
        branch=branch,
        call=lambda s, t: s.request_changes(t.alias, t.merge_request_id, reason=reason),
        headline="Changes requested on merge request #{id}",
    )


# -- Auto-merge ------------------------------------------------------------------------------


def merge_request_auto_merge(
    ctx: typer.Context,
    project: str | None = _PROJECT_OPT,
    merge_request_id: int | None = _MERGE_REQUEST_ID_OPT,
    branch: int | None = _BRANCH_OPT,
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help=(
            f"{' | '.join(AUTO_MERGE_STRATEGIES)}. `immediately` and `scheduled` ARM: once "
            "the merge request is approved, the backend merges it into production on its own "
            f"-- no `merge` call involved. `{AUTO_MERGE_DISARMED}` disarms"
        ),
    ),
    at: str | None = typer.Option(
        None, "--at", help="When to auto-merge (ISO 8601); required with --strategy scheduled"
    ),
    yes: bool = _YES_OPT,
) -> None:
    """Arm or disarm automatic merging of this merge request (destructive).

    A backend scheduler runs every `approved` merge request whose strategy is
    `immediately` (or `scheduled` and due) through the same merge processor
    `merge` uses -- on its own, retrying until it lands, with `merge` never
    called. Arming is therefore a delayed production merge and its own,
    consciously taken step: it is not a flag on `create` or `update`. Under
    --json the target must be explicit.
    """
    formatter = get_formatter(ctx)
    problem = validate_auto_merge_flags(strategy, at)
    if problem:
        _usage_error(formatter, problem)
    _require_explicit_target_under_json(
        formatter,
        merge_request_id=merge_request_id,
        branch=branch,
        reason="`merge-request auto-merge` arms (or disarms) an automatic production merge.",
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
        if arms_auto_merge(strategy):
            when = f" at {at}" if at else ""
            _confirm_or_abort(
                formatter,
                yes,
                f"Arm auto-merge ({strategy}{when}) on merge request #{target.merge_request_id}? "
                "Once it is approved, the backend will merge it into production automatically "
                "-- without a `merge` call. Continue?",
            )
        result = _stamp_target(
            service.update_merge_request(
                target.alias,
                target.merge_request_id,
                auto_merge_strategy=strategy,
                auto_merge_at=at,
            ),
            target,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_error(formatter, exc)

    verb = "Armed" if arms_auto_merge(strategy) else "Disarmed"
    _print_row_success(
        formatter,
        result,
        f"{verb} auto-merge ({strategy}) on merge request #{target.merge_request_id}",
    )
    if arms_auto_merge(strategy):
        _warn_if_armed(formatter, result)
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


# -- Merge ------------------------------------------------------------------------------------


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
    _require_explicit_target_under_json(
        formatter,
        merge_request_id=merge_request_id,
        branch=branch,
        reason="`merge-request merge` rewrites production and deletes the source branch.",
    )
    service = get_service(ctx, "merge_request_service")
    # The row is only for the prompt (title + branch); merge()'s own result
    # already carries branch_from_id. Skip the GET when no prompt will show.
    will_prompt = not yes and not formatter.json_mode
    try:
        target = _resolve_target(
            ctx,
            formatter,
            project=project,
            merge_request_id=merge_request_id,
            branch=branch,
            need_row=will_prompt,
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

    formatter.output(result, _render_merge_result)
    _emit_warnings(formatter, result)
    if result.get("cleanup_skipped"):
        _hint_next(
            formatter,
            "`kbagent branch reset` and `kbagent sync branch-unlink` if this project's active "
            "branch pointed at the merged branch",
        )
    else:
        _hint_next(formatter, "`merge-request list` -- the merged request now shows as merged")


def _render_merge_result(console: Any, data: dict[str, Any]) -> None:
    console.print(f"[bold green]Success:[/bold green] {escape(str(data['message']))}")
    if data.get("cleanup_skipped"):
        # Keyed on the structured flag, never on warning text: the local
        # cleanup did NOT run, so active_branch_id and the sync mapping may
        # still point at the branch the merge just doomed.
        console.print(
            "[yellow]Local cleanup skipped[/yellow]: the source branch id could not be read "
            f"({escape(str(data.get('branch_from_id_raw')))}); active branch and sync mapping "
            "were left untouched."
        )


# -- Resolve --------------------------------------------------------------------------------


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
    """Resolve one conflicting configuration (destructive: it removes a merge blocker).

    Every mode replaces the configuration in your branch; the previous content
    stays in its version history. Rebasing each listed conflict makes the merge
    request mergeable -- there is no re-validate step, and on an armed MR the
    last resolution is what lets the backend merge. There is deliberately no
    --all: conflicts are meant to be walked, not waved away. Under --json the
    target must be explicit.
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
    _require_explicit_target_under_json(
        formatter,
        merge_request_id=merge_request_id,
        branch=branch,
        reason="`merge-request resolve` removes a blocker the merge is waiting on.",
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

    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Success:[/bold green] Resolved {escape(component_id)}/{escape(config_id)} "
            f"({escape(str(d.get('resolution')))}) -- rebased onto production version "
            f"{escape(str(d.get('onto_version')))}"
        ),
    )
    _emit_warnings(formatter, result)
    _hint_next(
        formatter,
        "`merge-request conflicts` for what is left, then `merge-request merge`",
    )


def register(app: typer.Typer) -> None:
    """Mount the write commands flat onto the group's app -- same permission
    namespace, same --help. Through Typer's PUBLIC API: ``app.command(name)`` is
    a decorator factory, applied here to already-defined functions, so no
    module-level Typer instance and no private attribute is involved.
    """
    app.command("create")(merge_request_create)
    app.command("update")(merge_request_update)
    app.command("request-review")(merge_request_request_review)
    app.command("approve")(merge_request_approve)
    app.command("request-changes")(merge_request_request_changes)
    app.command("auto-merge")(merge_request_auto_merge)
    app.command("merge")(merge_request_merge)
    app.command("resolve")(merge_request_resolve)
