"""Version command - show the kbagent version and check for updates.

Thin CLI layer: calls VersionService and formats output.
No business logic belongs here.
"""

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..auto_update import show_deprecation_notice
from ..services.version_service import _major_of
from ._helpers import get_formatter, get_service


def _format_version_panel(console: Console, data: dict) -> None:
    """Render version information as a Rich panel."""
    text = Text()
    kbagent = data["kbagent"]
    text.append(f"kbagent v{kbagent['version']}", style="bold")

    if kbagent.get("up_to_date") is False and kbagent.get("latest_version"):
        text.append(f"    -> v{kbagent['latest_version']} available", style="yellow")
        # `install_channel` is present only for a frozen native binary, where
        # `kbagent update` deliberately refuses to act -- pointing the user at
        # it would just send them one step further down a dead end. The service
        # already put that channel's real command in `upgrade_command`.
        if kbagent.get("install_channel"):
            # `upgrade_command` is empty for channels with no single runnable
            # command (hand-unpacked archive); the hint sentence covers those.
            action = kbagent.get("upgrade_command") or kbagent.get("upgrade_hint", "")
            text.append(f"  ({action})", style="dim")
        else:
            text.append("  (run: kbagent update)", style="dim")
    elif kbagent.get("up_to_date") is True:
        text.append("    up to date", style="green")

    console.print(Panel(text, title="Version Info", border_style="blue"))


def version_command(
    ctx: typer.Context,
    beta: bool = typer.Option(
        False,
        "--beta",
        help="Report the latest pre-release (beta / rc) instead of the latest stable. "
        "KBAGENT_INCLUDE_PRERELEASE=1 has the same effect.",
    ),
) -> None:
    """Show the kbagent version and check for updates."""
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    include_prerelease = beta or _env_opted_into_prerelease()
    result = version_service.get_versions(include_prerelease=include_prerelease)
    show_deprecation_notice(formatter)
    formatter.output(result, _format_version_panel)


def update_command(
    ctx: typer.Context,
    beta: bool = typer.Option(
        False,
        "--beta",
        help=(
            "Opt into pre-release versions (beta / rc). Without this flag (the "
            "default) and without KBAGENT_INCLUDE_PRERELEASE=1 in env, every "
            "release marked as a pre-release on GitHub is skipped, so a beta "
            "release will never silently install. With --beta, the newest pre-release "
            "is installed from its own release; its dependencies stay on stable "
            "versions. --beta never installs a new major version without --major."
        ),
    ),
    major: bool = typer.Option(
        False,
        "--major",
        help=(
            "Install the next major version now (for example 0.x -> 1.x). Without "
            "this flag kbagent stays on the running major version until 28 days "
            "after the next major is released, then updates itself. The new major "
            "is installed after this command exits, once no kbagent process runs."
        ),
    ),
) -> None:
    """Update kbagent to the latest version.

    Runs ``uv tool install --force --reinstall`` from an exact release wheel
    or Git tag. This recreates the whole tool environment and preserves
    ``[server]`` extras. On a failure the result includes a copy/paste
    recovery command.

    On Windows the install is **scheduled, not run here** (issue #528).
    ``uv tool install`` removes the tool environment before recreating it, so
    running it from inside that environment deletes files the live process has
    locked and aborts half-way. A detached helper waits until every kbagent has
    exited and installs then; ``deferred: true`` marks that in JSON and the next
    launch reports the outcome.

    Human mode prints a one-line summary such as
    ``kbagent v0.84.2 -> v0.85.0``.

    Pre-release channel (since v0.42.0): use ``--beta`` (or set
    ``KBAGENT_INCLUDE_PRERELEASE=1`` in env) to opt into beta / rc
    releases. The startup auto-update hook never auto-installs a beta;
    only this explicit command does.

    Major versions (CLI-40): without ``--major`` the update stays on the
    running major until the migration date of the next one and prints the
    deprecation notice. ``--major`` installs the next major through the
    deferred helper: this command finishes on the old version.
    """
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    include_prerelease = beta or _env_opted_into_prerelease()
    result = version_service.self_update(include_prerelease=include_prerelease, allow_major=major)
    # No notice in the run that has just scheduled the crossing to the next major.
    if not _scheduled_major_crossing(result.get("kbagent", {})):
        show_deprecation_notice(formatter)

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result["updated"]:
            formatter.success(result["message"])
        else:
            formatter.console.print(result["message"])
        # The summary line only says "(scheduled)". What the user has to *do*
        # -- close other kbagent processes -- lives on the stage result, so
        # print that too rather than leave the instruction in JSON only.
        # Only for the scheduled case: every other branch already has its
        # message carried into the summary verbatim, so printing it here again
        # would just repeat the same sentence back at the user.
        kbagent_stage = result.get("kbagent", {})
        if kbagent_stage.get("deferred") is True:
            formatter.console.print(kbagent_stage["message"])


def _scheduled_major_crossing(kbagent_result: dict) -> bool:
    """Whether the update result is a deferred install of a higher major version."""
    if not kbagent_result.get("deferred"):
        return False
    current = kbagent_result.get("current_version")
    latest = kbagent_result.get("latest_version")
    return _major_of(latest) > _major_of(current)


def _env_opted_into_prerelease() -> bool:
    """Honour ``KBAGENT_INCLUDE_PRERELEASE=1`` as a per-shell opt-in.

    Some users want every kbagent invocation in a session to use the beta
    channel without re-typing ``--beta`` (e.g. CI smoke-tests for an
    upcoming release). Mirrors the truthy parse pattern used by
    ``KBAGENT_SKIP_UPDATE`` so the two env vars behave consistently.
    """
    import os

    raw = os.environ.get("KBAGENT_INCLUDE_PRERELEASE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
