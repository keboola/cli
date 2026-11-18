"""Output and config-dir helpers of `kbagent project refresh` (split from project.py)."""

import logging
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..config_store import ConfigStore, global_config_dir
from ..output import format_item_error

logger = logging.getLogger(__name__)

LOCAL_COPIES_HINT = (
    "Local kbagent directories created with `kbagent init --from-global` keep their own "
    "copies of the tokens; run `kbagent project refresh` inside each to take the new ones."
)


def is_global_config(config_store: ConfigStore) -> bool:
    """True when ``config_store`` is the global config, however its path is spelled."""
    try:
        return config_store.config_dir.resolve() == global_config_dir().resolve()
    except OSError as exc:
        logger.debug(
            "Cannot resolve config dirs (%s); treating the config as global", type(exc).__name__
        )
        return True


def global_store_for(config_store: ConfigStore) -> ConfigStore | None:
    """The read-only global config to take tokens from, or None (#955).

    None when the refreshed config is the global config itself, or when the
    global ``config.json`` does not exist or cannot be checked.
    """
    if is_global_config(config_store):
        return None
    global_dir = global_config_dir()
    try:
        if not (global_dir / ConfigStore.CONFIG_FILENAME).is_file():
            return None
    except OSError as exc:
        # An unreadable global directory means there is no global store to use.
        logger.debug("Global config dir unreadable (%s); not consulted", type(exc).__name__)
        return None
    return ConfigStore(config_dir=global_dir, source="global")


def refresh_total(data: dict[str, Any]) -> int:
    """Every project that `project refresh` looked at."""
    keys = (
        "projects_adopted",
        "projects_refreshed",
        "projects_valid",
        "projects_skipped",
        "projects_failed",
    )
    return sum(len(data.get(key, [])) for key in keys)


def tokens_to_update(data: dict[str, Any]) -> int:
    """Tokens a run would write: taken from the global config plus minted."""
    return len(data.get("projects_adopted", [])) + len(data.get("projects_refreshed", []))


def refresh_summary(data: dict[str, Any]) -> str:
    """The headline summary. Without adoptions it keeps the pre-#955 wording."""
    would = "would be " if data.get("dry_run") else ""
    adopted = len(data.get("projects_adopted", []))
    refreshed = len(data.get("projects_refreshed", []))
    if not adopted:
        return f"{refreshed} token(s) {would}refreshed."
    return f"{adopted} token(s) {would}taken from the global config, {refreshed} {would}refreshed."


def not_used_lines(entries: list[dict[str, Any]]) -> list[str]:
    """One line per project whose global token was refused for having fewer rights.

    Names the aliases only, never a token.
    """
    lines = []
    for entry in entries:
        not_used = entry.get("global_token_not_used")
        if not not_used:
            continue
        lines.append(
            f"Global token of '{entry['alias']}' (global alias '{not_used['global_alias']}') "
            "has fewer rights than a new token; not used."
        )
    return lines


def format_refresh_result(console: Console, data: dict, *, local_copies_hint: bool = False) -> None:
    """Render token refresh results as Rich tables with summary.

    ``local_copies_hint``: the run used the global config; when it minted a
    token, remind that local directories keep their own copies.
    """
    dry_run = data.get("dry_run", False)
    mode_label = "[bold yellow]DRY RUN[/bold yellow] " if dry_run else ""
    console.print(f"\n{mode_label}Token Refresh\n")

    adopted = data.get("projects_adopted", [])
    if adopted:
        title = (
            "Tokens to Take from Global Config" if dry_run else "Tokens Taken from Global Config"
        )
        table = Table(title=title)
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        table.add_column("Global Alias")
        for p in adopted:
            table.add_row(
                escape(p["alias"]),
                str(p["project_id"]),
                escape(p["project_name"]),
                escape(p["global_alias"]),
            )
        console.print(table)
        console.print()

    refreshed = data.get("projects_refreshed", [])
    if refreshed:
        action_label = "Projects to Refresh" if dry_run else "Projects Refreshed"
        table = Table(title=action_label)
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        if not dry_run:
            table.add_column("Token", style="dim")

        for p in refreshed:
            row = [escape(p["alias"]), str(p["project_id"]), escape(p["project_name"])]
            if not dry_run:
                row.append(p.get("token", ""))
            table.add_row(*row)

        console.print(table)
        console.print()

    valid = data.get("projects_valid", [])
    if valid:
        table = Table(title="Projects Valid")
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")

        for p in valid:
            table.add_row(escape(p["alias"]), str(p["project_id"]), escape(p["project_name"]))

        console.print(table)
        console.print()

    skipped = data.get("projects_skipped", [])
    if skipped:
        table = Table(title="Projects Skipped")
        table.add_column("Alias", style="bold cyan")
        table.add_column("Reason", style="dim")

        for p in skipped:
            table.add_row(p["alias"], p["reason"])

        console.print(table)
        console.print()

    failed = data.get("projects_failed", [])
    for line in not_used_lines(refreshed):
        console.print(line, soft_wrap=True, markup=False)
    for p in failed:
        # A refused global token is part of that project's one error line (HO2).
        message = " ".join([p["message"], *not_used_lines([p])])
        console.print(format_item_error(f"project '{p['alias']}'", message), soft_wrap=True)

    summary_parts = []
    if adopted:
        verb = "to take from global config" if dry_run else "taken from global config"
        summary_parts.append(f"[bold green]{len(adopted)}[/bold green] {verb}")
    if refreshed:
        verb = "to refresh" if dry_run else "refreshed"
        summary_parts.append(f"[bold green]{len(refreshed)}[/bold green] {verb}")
    if valid:
        summary_parts.append(f"[bold green]{len(valid)}[/bold green] valid")
    if skipped:
        summary_parts.append(f"[dim]{len(skipped)} skipped[/dim]")
    if failed:
        summary_parts.append(f"[bold red]{len(failed)} failed[/bold red]")

    console.print("Summary: " + ", ".join(summary_parts) if summary_parts else "No changes.")

    if local_copies_hint and not dry_run and refreshed:
        console.print(LOCAL_COPIES_HINT, soft_wrap=True)
