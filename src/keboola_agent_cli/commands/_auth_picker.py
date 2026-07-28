"""Interactive project picker for `kbagent auth register-projects` / login hook.

Pure terminal I/O over the `ProjectCandidate` list the service layer builds --
alias computation, collision resolution against the persisted config, and the
actual write all live in `AuthService` (services/auth_service.py). This module
must never reimplement any of that; it only parses what the user typed and
drives the prompts.

`parse_selection` and `parse_alias_overrides` are pure functions (no I/O) so
they are directly unit-testable without a terminal. `run_project_picker` is
the interactive half and is exercised through `CliRunner(input=...)` in
tests/test_cli_auth.py instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import typer
from rich.console import Console
from rich.table import Table

from ..config_store import validate_alias_format
from ..errors import ConfigError
from ..services.auth_service import ProjectCandidate, ProjectSelection

# Bounds every re-prompt loop below so a piped/garbage stdin (or a script that
# forgot --yes) cannot spin forever -- after this many bad attempts we give up
# loudly via typer.Abort() rather than looping until the process is killed.
_MAX_PROMPT_ATTEMPTS = 5


def parse_selection(raw: str, count: int) -> list[int]:
    """Parse a picker selection string into 0-based indices, ascending, deduped.

    Accepts: "" and "none" -> []; "all" and "*" -> every index; comma- and/or
    space-separated 1-based numbers; inclusive ranges "1-3"; mixtures
    ("1-3, 5 7"). Raises ValueError with a user-facing message on an
    out-of-range index, a reversed range, or an unparseable token.
    """
    stripped = raw.strip()
    if not stripped or stripped.lower() == "none":
        return []
    if stripped.lower() in ("all", "*"):
        return list(range(count))

    # Accept commas and/or whitespace as separators uniformly.
    tokens = [token for token in stripped.replace(",", " ").split() if token]
    if not tokens:
        return []

    indices: set[int] = set()
    for token in tokens:
        indices.update(_parse_token(token, count))
    return sorted(indices)


def _parse_token(token: str, count: int) -> list[int]:
    """Parse one selection token ("5" or "1-3") into 0-based indices."""
    if "-" in token:
        start_str, sep, end_str = token.partition("-")
        if not sep or not start_str.isdigit() or not end_str.isdigit():
            raise ValueError(
                f"Invalid selection '{token}': expected a number, a range like "
                "'1-3', 'all', or 'none'."
            )
        start, end = int(start_str), int(end_str)
        if start > end:
            raise ValueError(f"Invalid range '{token}': start must not be greater than end.")
        _check_bounds(start, count, token)
        _check_bounds(end, count, token)
        return list(range(start - 1, end))

    if not token.isdigit():
        raise ValueError(
            f"Invalid selection '{token}': expected a number, a range like '1-3', 'all', or 'none'."
        )
    value = int(token)
    _check_bounds(value, count, token)
    return [value - 1]


def _check_bounds(value: int, count: int, token: str) -> None:
    """Raise ValueError if the 1-based `value` is outside [1, count]."""
    if value < 1 or value > count:
        raise ValueError(f"Selection '{token}' is out of range (valid: 1-{count}).")


def parse_alias_overrides(raw: Sequence[str]) -> dict[int, str]:
    """Parse repeated `--alias ID=ALIAS` values.

    Raises ConfigError on a missing `=`, a non-integer id, a duplicate id, or
    an alias that fails `validate_alias_format`.
    """
    overrides: dict[int, str] = {}
    for item in raw:
        id_part, sep, alias_part = item.partition("=")
        if not sep:
            raise ConfigError(f"Invalid --alias '{item}': expected ID=ALIAS.")
        id_part = id_part.strip()
        alias_part = alias_part.strip()
        if not id_part.isdigit():
            raise ConfigError(f"Invalid --alias '{item}': '{id_part}' is not a valid project id.")
        project_id = int(id_part)
        if project_id in overrides:
            raise ConfigError(f"Duplicate --alias for project id {project_id}.")
        validate_alias_format(alias_part, field="--alias")
        overrides[project_id] = alias_part
    return overrides


def _render_candidates_table(
    console: Console, candidates: Sequence[ProjectCandidate], alias_overrides: Mapping[int, str]
) -> None:
    """Print the numbered candidate table the picker's selection prompt refers to."""
    table = Table()
    table.add_column("#", justify="right")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Alias")
    table.add_column("Status")
    for position, candidate in enumerate(candidates, start=1):
        alias_preview = alias_overrides.get(candidate.project_id, candidate.default_alias)
        table.add_row(
            str(position),
            str(candidate.project_id),
            candidate.project_name,
            candidate.role,
            alias_preview,
            "registered" if candidate.registered else "new",
        )
    console.print(table)


def _prompt_selection(console: Console, count: int) -> list[int]:
    """Prompt for the selection string, re-prompting on a parse error.

    The default is deliberately ``all``, not ``none``. By the time this
    prompt appears the user has already opted in -- either by running
    `auth register-projects` outright, or by answering yes to the post-login
    "register these now?" question. Defaulting to ``none`` would mean two
    bare Enters register nothing and drop the user straight back into the
    "logged in but `--project` resolves nothing" trap this picker exists to
    fix. Registering is additive, never overwrites, and is undone with
    `kbagent project remove`, so the wrong-default cost is asymmetric.
    """
    for _ in range(_MAX_PROMPT_ATTEMPTS):
        raw = typer.prompt(
            "Select projects to register [numbers, ranges (1-3), 'all', 'none']",
            default="all",
        )
        try:
            return parse_selection(raw, count)
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Abort()


def _prompt_alias(
    console: Console,
    candidate: ProjectCandidate,
    default_alias: str,
    candidates: Sequence[ProjectCandidate],
    chosen_aliases: dict[str, int],
) -> str:
    """Prompt for one candidate's alias, re-prompting on format or collision errors.

    Collision checks here are advisory UX only (format validity, a duplicate
    within this run, or reuse of another candidate's already-registered
    alias) -- they use only what is already in `candidates`, never a fresh
    config lookup. The authoritative collision check against the full
    persisted config happens in `AuthService.register_projects`, which this
    picker must not reimplement.
    """
    for _ in range(_MAX_PROMPT_ATTEMPTS):
        raw = typer.prompt(
            f"Alias for project {candidate.project_id} ({candidate.project_name})",
            default=default_alias,
        )
        candidate_alias = raw.strip()
        try:
            validate_alias_format(candidate_alias, field="alias")
        except ConfigError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc.message}")
            continue

        chosen_by = chosen_aliases.get(candidate_alias)
        if chosen_by is not None and chosen_by != candidate.project_id:
            console.print(
                f"[bold red]Error:[/bold red] Alias '{candidate_alias}' was already "
                f"chosen for project {chosen_by} earlier in this run."
            )
            continue

        other = next(
            (
                other_candidate
                for other_candidate in candidates
                if other_candidate.project_id != candidate.project_id
                and other_candidate.existing_alias == candidate_alias
            ),
            None,
        )
        if other is not None:
            console.print(
                f"[bold red]Error:[/bold red] Alias '{candidate_alias}' is already "
                f"registered to project {other.project_id} ({other.project_name})."
            )
            continue

        return candidate_alias
    raise typer.Abort()


def _print_selection_summary(
    console: Console, candidates: Sequence[ProjectCandidate], selections: Sequence[ProjectSelection]
) -> None:
    """Print the confirmation summary table just before the final yes/no prompt."""
    by_id = {candidate.project_id: candidate for candidate in candidates}
    table = Table(title="Projects to register")
    table.add_column("Alias")
    table.add_column("Project")
    for selection in selections:
        candidate = by_id[selection.project_id]
        table.add_row(selection.alias, f"{candidate.project_name} ({candidate.project_id})")
    console.print(table)


def run_project_picker(
    console: Console,
    candidates: Sequence[ProjectCandidate],
    *,
    alias_overrides: Mapping[int, str] | None = None,
    assume_yes: bool = False,
) -> list[ProjectSelection]:
    """Interactive TTY picker. Returns [] when the user selects nothing or
    declines the final confirmation.

    Callers own printing any header (e.g. "Accessible projects on <stack>")
    before invoking this -- the picker itself only knows about `candidates`,
    not the stack they came from.
    """
    if not candidates:
        console.print("No accessible projects to register.")
        return []

    overrides = alias_overrides or {}
    _render_candidates_table(console, candidates, overrides)

    indices = _prompt_selection(console, len(candidates))
    if not indices:
        return []

    selections: list[ProjectSelection] = []
    chosen_aliases: dict[str, int] = {}
    for index in indices:
        candidate = candidates[index]
        default_alias = overrides.get(candidate.project_id, candidate.default_alias)
        chosen_alias = _prompt_alias(console, candidate, default_alias, candidates, chosen_aliases)
        chosen_aliases[chosen_alias] = candidate.project_id
        selections.append(ProjectSelection(project_id=candidate.project_id, alias=chosen_alias))

    _print_selection_summary(console, candidates, selections)
    if not assume_yes and not typer.confirm(
        f"Register these {len(selections)} project(s)?", default=True
    ):
        return []
    return selections
