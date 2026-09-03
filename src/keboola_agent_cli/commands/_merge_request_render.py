"""Rich renderers for the ``kbagent merge-request`` group.

A private sibling of ``commands/merge_request.py`` (precedent: ``_auth_picker.py``):
the four renderers here -- list table, detail panel, conflicts table, three-way
diff -- are a coherent unit no other group's output shares. Presentation only;
every fact rendered comes from the service payload, whose wire shape is the
authority in ``docs/merge-requests-notes.md``.

Two rules hold in every function:

- **Every wire-sourced string goes through :func:`rich.markup.escape`** before it
  enters a ``Table`` or ``Panel``. Rich interprets markup by default, so an MR
  titled ``Fix [bold] parsing`` mangles the table and an unbalanced ``[/]``
  raises ``MarkupError``. Titles, descriptions, names, conflict messages,
  reasons -- all user-authored.
- **``derived_state``, never raw ``state``, is what the user sees.** The whole
  point of the derivation is that the CLI agrees with the web UI badge.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

_DASH = "—"

# derived_state -> how the list badge / detail header shows it. The vocabulary
# is the service's (STATE_FILTER_VOCABULARY); this is only its casing/colour.
_STATE_STYLE: dict[str, str] = {
    "in_development": "cyan",
    "in_review": "yellow",
    "approved": "green",
    "in_merge": "magenta",
    "merged": "bold green",
    "closed": "dim",
    "rejected": "red",
}

# allowed_actions vocabulary (service's _ALLOWED_ACTIONS_BY_STATE) -> the
# command that produces it. `resolve_conflicts` maps to the inspect step: the
# user has to see the conflicts before choosing a resolution.
_ACTION_COMMAND: dict[str, str] = {
    "request_review": "merge-request request-review",
    "approve": "merge-request approve",
    "request_changes": "merge-request request-changes",
    "merge": "merge-request merge",
    "update": "merge-request update",
    "resolve_conflicts": "merge-request conflicts",
}

# Values longer than this are elided in the default `diff` render; --format full
# prints them whole.
_DIFF_VALUE_MAX = 60


def _state_badge(derived_state: str) -> str:
    style = _STATE_STYLE.get(derived_state, "")
    text = escape(derived_state.replace("_", " "))
    return f"[{style}]{text}[/{style}]" if style else text


def _s(value: Any) -> str:
    """Escape any wire value for a cell; ``None`` renders as a dash."""
    return _DASH if value is None else escape(str(value))


def _reviewers_cell(reviewers: list[dict[str, Any]] | None) -> str:
    parts = []
    for reviewer in reviewers or []:
        name = _s(reviewer.get("name") or reviewer.get("id"))
        status = reviewer.get("status")
        parts.append(f"{name} ({escape(str(status))})" if status else name)
    return ", ".join(parts)


# -- list ----------------------------------------------------------------------------


def format_merge_requests_table(console: Console, data: dict[str, Any]) -> None:
    """The list: server order preserved (``createdAt DESC`` -- the renderer never
    re-sorts), optional columns shown only when some row carries a value so the
    common table stays narrow, and the empty case told apart from the
    feature-less one via the service's ``feature_enabled``."""
    rows: list[dict[str, Any]] = data.get("merge_requests") or []
    if not rows:
        if data.get("feature_enabled") is False:
            console.print(
                "Merge requests are not enabled on this project "
                "(the 'branches-merge-requests' feature is missing)."
            )
        elif data.get("state_filter"):
            console.print(f"No merge requests with state '{escape(str(data['state_filter']))}'.")
        else:
            console.print("No merge requests.")
        return

    show_external = any(r.get("externalId") for r in rows)
    show_created = any(r.get("createdAt") for r in rows)
    show_merger = any((r.get("merge") or {}).get("mergerName") for r in rows)

    table = Table(title=f"Merge requests in '{escape(str(data.get('alias', '')))}'")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Title")
    table.add_column("Author")
    table.add_column("Branch", justify="right", no_wrap=True)
    table.add_column("Reviewers")
    if show_external:
        table.add_column("External ID")
    if show_created:
        table.add_column("Created", no_wrap=True)
    if show_merger:
        table.add_column("Merged by")

    for mr in rows:
        cells = [
            _s(mr.get("id")),
            _state_badge(str(mr.get("derived_state") or mr.get("state") or "")),
            _s(mr.get("title")),
            _s((mr.get("creator") or {}).get("name")),
            _s((mr.get("branches") or {}).get("branchFromId")),
            _reviewers_cell(mr.get("reviewers")),
        ]
        if show_external:
            cells.append(_s(mr.get("externalId")))
        if show_created:
            cells.append(_s(mr.get("createdAt")))
        if show_merger:
            cells.append(_s((mr.get("merge") or {}).get("mergerName")))
        table.add_row(*cells)
    console.print(table)


# -- detail ----------------------------------------------------------------------------


def _blockers_line(data: dict[str, Any]) -> str:
    blockers = data.get("merge_blockers") or []
    if data.get("mergeable"):
        return "[green]Mergeable[/green] (the merge itself is still the authority)"
    if not blockers:
        # mergeable is False but no blocker listed: conflicts were not fetched
        # (closed MR) -- say nothing definite either way.
        return "[dim]Merge readiness not evaluated[/dim]"
    parts = []
    for blocker in blockers:
        if blocker == "conflicts":
            parts.append(f"conflicts ({data.get('conflicts_count', '?')})")
        else:
            parts.append(escape(str(blocker)))
    return f"[red]Blocked by[/red]: {', '.join(parts)}"


def _viewer_line(viewer: dict[str, Any] | None) -> str | None:
    """Only truthy flags render; ``None`` is "unknown", never "no"."""
    if not viewer:
        return None
    facts = []
    if viewer.get("is_creator"):
        facts.append("you created this merge request")
    if viewer.get("has_approved"):
        facts.append("you have approved it")
    return ("You: " + ", ".join(facts)) if facts else None


def _kv_table(pairs: list[tuple[str, str]]) -> Table:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    for key, value in pairs:
        table.add_row(key, value)
    return table


def format_merge_request_detail(console: Console, data: dict[str, Any]) -> None:
    title = _s(data.get("title"))
    derived = str(data.get("derived_state") or data.get("state") or "")
    header = f"Merge request #{_s(data.get('id'))}: {title}  {_state_badge(derived)}"
    if data.get("state") and data.get("state") != derived:
        header += f"  [dim](raw state: {escape(str(data['state']))})[/dim]"
    console.print(Panel(header, expand=False))

    branches = data.get("branches") or {}
    merge_info = data.get("merge") or {}
    pairs: list[tuple[str, str]] = [
        ("Readiness", _blockers_line(data)),
    ]
    viewer = _viewer_line(data.get("viewer"))
    if viewer:
        pairs.append(("", viewer))
    strategy = data.get("autoMergeStrategy")
    if strategy and strategy != "none":
        when = f" at {escape(str(data['autoMergeAt']))}" if data.get("autoMergeAt") else ""
        pairs.append(
            (
                "Auto-merge",
                f"[red]armed[/red] ({escape(str(strategy))}{when}) -- the backend merges this "
                "MR on its next tick once it is approved",
            )
        )
    pairs.append(
        (
            "Branches",
            f"{_s(branches.get('branchFromId'))} → {_s(branches.get('branchIntoId'))}",
        )
    )
    pairs.append(("Author", _s((data.get("creator") or {}).get("name"))))
    if data.get("createdAt"):
        pairs.append(("Created", _s(data["createdAt"])))
    if merge_info.get("mergedAt"):
        pairs.append(
            ("Merged", f"{_s(merge_info.get('mergedAt'))} by {_s(merge_info.get('mergerName'))}")
        )
    if data.get("externalId"):
        pairs.append(("External ID", _s(data["externalId"])))
    if data.get("description"):
        pairs.append(("Description", _s(data["description"])))
    console.print(_kv_table(pairs))

    reviewers = data.get("reviewers") or []
    if reviewers:
        table = Table(title="Reviewers", title_justify="left")
        table.add_column("Name")
        table.add_column("Status")
        for reviewer in reviewers:
            table.add_row(
                _s(reviewer.get("name") or reviewer.get("id")), _s(reviewer.get("status"))
            )
        console.print(table)

    approvals = data.get("approvals") or []
    if approvals:
        table = Table(title="Approvals", title_justify="left")
        table.add_column("Approver")
        table.add_column("At")
        for approval in approvals:
            table.add_row(_s(approval.get("approverName")), _s(approval.get("createdAt")))
        console.print(table)

    _print_change_log(console, data)

    if "conflicts" in data:
        console.print()
        format_conflicts_table(console, data, heading=True)

    activity = data.get("activityLog")
    if isinstance(activity, list):
        console.print()
        console.print(f"[bold]Activity log[/bold] ({len(activity)} entries)")
        for entry in activity:
            console.print(f"  {escape(json.dumps(entry, ensure_ascii=False, default=str))}")


def _print_change_log(console: Console, data: dict[str, Any]) -> None:
    """The configurations the merge will apply -- written by the backend at
    review time, so LEGITIMATELY empty while the MR sits in ``development``.
    Say that instead of showing a bare empty table."""
    change_log = data.get("changeLog")
    configurations = (
        (change_log or {}).get("configurations") if isinstance(change_log, dict) else None
    )
    console.print()
    if not configurations:
        if (data.get("state") or "") == "development":
            console.print(
                "[bold]Change log[/bold]: empty until the merge request is sent for review "
                "(the backend records the changed configurations at that point)."
            )
        else:
            console.print("[bold]Change log[/bold]: no configuration changes recorded.")
        return
    table = Table(title=f"Change log ({len(configurations)} configurations)", title_justify="left")
    table.add_column("Component")
    table.add_column("Configuration")
    table.add_column("Deleted")
    for entry in configurations:
        table.add_row(
            _s(entry.get("componentId")),
            _s(entry.get("configurationId")),
            "yes" if entry.get("isDeleted") else "",
        )
    console.print(table)


def next_step_hints(allowed_actions: list[str] | None) -> list[str]:
    """``allowed_actions`` -> the commands that produce them, in the service's order.
    State-derived and feature-blind by Layer 2's decision: on a project where
    the feature was later switched off these recommend writes that end in
    FEATURE_NOT_ENABLED (RFC, Known gaps)."""
    return [_ACTION_COMMAND[a] for a in (allowed_actions or []) if a in _ACTION_COMMAND]


# -- conflicts -------------------------------------------------------------------------------


def format_conflicts_table(
    console: Console, data: dict[str, Any], *, heading: bool = False
) -> None:
    conflicts: list[dict[str, Any]] = data.get("conflicts") or []
    count = data.get("conflicts_count", len(conflicts))
    if not conflicts:
        console.print(
            "[green]No conflicts[/green] -- every changed configuration is unchanged in production."
        )
        return
    table = Table(title=f"Conflicts ({count})" if heading or count else None, title_justify="left")
    table.add_column("Component")
    table.add_column("Configuration")
    # The entry's isDeleted is the DEV-branch side's flag, not production's.
    table.add_column("Deleted in branch")
    table.add_column("Message")
    for entry in conflicts:
        table.add_row(
            _s(entry.get("componentId")),
            _s(entry.get("configurationId")),
            "yes" if entry.get("isDeleted") else "",
            _s(entry.get("message")),
        )
    console.print(table)


# -- diff ---------------------------------------------------------------------------------------


def _value_cell(value: Any, *, full: bool) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if not full and len(text) > _DIFF_VALUE_MAX:
        text = text[: _DIFF_VALUE_MAX - 1] + "…"
    return escape(text)


def _deleted_side_message(data: dict[str, Any]) -> str | None:
    """The side-level facts come FIRST: a null side yields zero per-path rows,
    so 'production deleted it, your branch changed it' arrives as
    ``changes: []`` + ``theirs_deleted: true`` -- the sharpest conflict there is,
    and a table-first render would print three empty sections and "No changes".
    This is the one place the user faces a binary choice the command already
    knows, so the sentence recommends the resolution."""
    ours, theirs = data.get("ours_deleted"), data.get("theirs_deleted")
    if theirs is True and not ours:
        return (
            "[red]Production deleted this configuration; your branch changed it.[/red]\n"
            "Resolve with `merge-request resolve --take delete` (drop it) or "
            "`--take ours` (keep your version)."
        )
    if ours is True and not theirs:
        return (
            "[red]Your branch deleted this configuration; production changed it.[/red]\n"
            "Resolve with `merge-request resolve --take delete` (drop it) or "
            "`--take theirs` (keep production's version)."
        )
    if ours is True and theirs is True:
        return "Both sides deleted this configuration -- there is nothing to reconcile."
    # A None flag means the side does not exist at all, which a conflict should
    # never produce (it requires the config on both sides): render defensively,
    # no recommendation.
    if theirs is None:
        return "This configuration is not present on the production side."
    if ours is None:
        return "This configuration is not present in the development branch."
    return None


def format_config_diff(console: Console, data: dict[str, Any], *, full: bool = False) -> None:
    console.print(
        f"[bold]{_s(data.get('component_id'))}/{_s(data.get('config_id'))}[/bold]  "
        f"branch {_s(data.get('branch_id'))} → production  "
        f"[dim](rebase onto version {_s(data.get('onto_version'))})[/dim]"
    )

    deleted = _deleted_side_message(data)
    if deleted:
        console.print(deleted)
        return

    changes: list[dict[str, Any]] = data.get("changes") or []
    if not changes:
        # Both sides exist, neither deleted, nothing differs: the conflict
        # cleared between `conflicts` and `diff` -- say that, not "nothing changed".
        console.print(
            "No differing paths: this conflict has cleared since it was listed "
            "(run `merge-request conflicts` again)."
        )
        return

    both = [c for c in changes if c.get("changed_by") == "both" and not c.get("agreed")]
    agreed = [c for c in changes if c.get("changed_by") == "both" and c.get("agreed")]
    ours_only = [c for c in changes if c.get("changed_by") == "ours"]
    theirs_only = [c for c in changes if c.get("changed_by") == "theirs"]

    def section(title: str, rows: list[dict[str, Any]], *, columns: tuple[str, ...]) -> None:
        if not rows:
            return
        table = Table(title=f"{title} ({len(rows)})", title_justify="left")
        table.add_column("Path", style="cyan", no_wrap=True)
        for column in columns:
            # fold, never crop: in --format full the whole value must be
            # readable, and Rich's default overflow would cut it with "…" --
            # indistinguishable from this renderer's own elision marker.
            table.add_column(column, overflow="fold")
        for change in rows:
            cells = [escape(str(change.get("path")))]
            for column in columns:
                key = {"Base": "base", "Yours": "ours", "Production": "theirs"}[column]
                cells.append(_value_cell(change.get(key), full=full))
            table.add_row(*cells)
        console.print(table)

    section("Both changed -- decide", both, columns=("Base", "Yours", "Production"))
    section("Both changed identically -- agreed", agreed, columns=("Base", "Yours"))
    section("Only you changed", ours_only, columns=("Base", "Yours"))
    section("Only production changed", theirs_only, columns=("Base", "Production"))
    if not full and any(
        len(json.dumps(c.get(k), default=str)) > _DIFF_VALUE_MAX
        for c in changes
        for k in ("base", "ours", "theirs")
    ):
        console.print("[dim]Long values elided -- pass --format full to print them whole.[/dim]")
