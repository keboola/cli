"""Write commands of the ``kbagent merge-request`` group.

``create`` / ``update`` / ``request-review`` / ``approve`` / ``request-changes`` /
``merge`` / ``resolve`` -- split out of ``merge_request.py`` when the group
crossed the 800-code-line soft ceiling. Mounted flat onto the group's Typer app
via :func:`register`, so permission keys stay in the ``merge-request.*``
namespace and ``--help`` lists them with the reads (precedent:
``_storage_describe.register``). Shared machinery -- target resolution, the one
error handler, the destructive-under-json rule, the auto-merge escalation --
lives in ``_merge_request_common.py``; this module only decides what each write
asks, confirms, and says afterwards. Design record: ``docs/merge-requests-layer1.md``.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.merge_request_service import (
    TAKE_MODES,
    arms_auto_merge,
    validate_auto_merge_flags,
)
from ._helpers import (
    check_cli_operation,
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
    _escalate_if_armed,
    _handle_error,
    _hint_from_actions,
    _hint_next,
    _print_row_success,
    _require_explicit_target_under_json,
    _resolve_target,
    _stamp_target,
    _usage_error,
    _warn_armed,
)

writes_app = typer.Typer()


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
    """Exit 2 on a bad strategy or pairing (the service owns the vocabulary and
    the rule, so the router and the CLI cannot drift); return whether the flags ARM."""
    problem = validate_auto_merge_flags(strategy, at)
    if problem:
        _usage_error(formatter, problem)
    return arms_auto_merge(strategy)


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


@writes_app.command("create")
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
        _warn_armed(formatter, str(auto_merge_strategy), result)
    _print_row_success(
        formatter,
        result,
        f"Created merge request #{result.get('id')} from branch {branch_id}",
    )
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


@writes_app.command("update")
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
        _warn_armed(formatter, str(auto_merge_strategy), result)
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
        _warn_armed(formatter, strategy, result)
    _print_row_success(formatter, result, headline.format(id=target.merge_request_id))
    _emit_warnings(formatter, result)
    _hint_from_actions(formatter, result)


@writes_app.command("request-review")
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


@writes_app.command("approve")
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


@writes_app.command("request-changes")
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


@writes_app.command("merge")
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
        # Keyed on the structured flag (followups F3), never on warning text:
        # the local cleanup did NOT run, so active_branch_id and the sync
        # mapping may still point at the branch the merge just doomed.
        console.print(
            "[yellow]Local cleanup skipped[/yellow]: the source branch id could not be read "
            f"({escape(str(data.get('branch_from_id_raw')))}); active branch and sync mapping "
            "were left untouched."
        )


@writes_app.command("resolve")
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
        _warn_armed(formatter, strategy, result)
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
    """Mount the write commands flat onto the group's app (same namespace, same --help)."""
    app.registered_commands.extend(writes_app.registered_commands)
