"""Version command - show kbagent version and dependency update checks.

Thin CLI layer: calls VersionService and formats output.
No business logic belongs here.
"""

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ._helpers import get_formatter, get_service


def _format_dep_standard(text: Text, dep: dict) -> None:
    """Format a standard dependency (local install with upgrade check)."""
    name = dep["name"]
    desc = dep["description"]
    local = dep.get("local_version")
    latest = dep.get("latest_version")
    up_to_date = dep.get("up_to_date")

    if local is None:
        text.append(f"  {name:<28}", style="dim")
        text.append("not installed\n", style="red")
        return

    label = f"{name} ({desc})"
    text.append(f"  {label:<28}")
    text.append(f"v{local}")

    if up_to_date is False and latest is not None:
        text.append(f"    -> v{latest} available", style="yellow")
        text.append(f" ({dep['upgrade_command']})", style="dim")
    elif up_to_date is True:
        text.append("    up to date", style="green")
    else:
        text.append("    (update check failed)", style="dim")

    text.append("\n")


def _format_dep_auto_update(text: Text, dep: dict) -> None:
    """Format an auto-updating dependency.

    Since v0.30.1: dependencies are auto-updated by ``auto_update.py`` on
    kbagent startup (and explicitly by ``kbagent update``). The renderer
    surfaces the locally installed version + up-to-date status so a user
    in the terminal sees the staleness signal immediately, not just in
    JSON mode.
    """
    name = dep["name"]
    desc = dep["description"]
    local = dep.get("version")
    latest = dep.get("latest_version")
    up_to_date = dep.get("up_to_date")
    install_method = dep.get("install_method", "?")
    upgrade_cmd = dep.get("upgrade_command", "")

    label = f"{name} ({desc})"
    text.append(f"  {label:<28}")

    if local is None:
        if install_method == "none":
            text.append("not installed", style="yellow")
            if upgrade_cmd:
                text.append(f" ({upgrade_cmd})", style="dim")
        else:
            text.append("local version unknown", style="dim")
            if latest:
                text.append(f"    (latest on PyPI: v{latest})", style="dim")
        text.append("\n")
        return

    text.append(f"v{local}")

    if up_to_date is False and latest is not None:
        text.append(f"    -> v{latest} (auto-updates on next startup)", style="yellow")
    elif up_to_date is True:
        text.append("    auto-updates (up to date)", style="green")
    else:
        text.append("    (update check failed)", style="dim")

    text.append("\n")


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
            text.append(f"  ({kbagent.get('upgrade_command')})", style="dim")
        else:
            text.append("  (run: kbagent update)", style="dim")
    elif kbagent.get("up_to_date") is True:
        text.append("    up to date", style="green")

    text.append("\n\nDependencies:\n")

    for dep in data["dependencies"]:
        if dep.get("auto_updates"):
            _format_dep_auto_update(text, dep)
        else:
            _format_dep_standard(text, dep)

    console.print(Panel(text, title="Version Info", border_style="blue"))


def version_command(
    ctx: typer.Context,
    beta: bool = typer.Option(
        False,
        "--beta",
        help="Report the latest pre-release (beta / rc) instead of the latest stable.",
    ),
) -> None:
    """Show kbagent version and check for dependency updates."""
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    include_prerelease = beta or _env_opted_into_prerelease()
    result = version_service.get_versions(include_prerelease=include_prerelease)
    formatter.output(result, _format_version_panel)


def update_command(
    ctx: typer.Context,
    beta: bool = typer.Option(
        False,
        "--beta",
        help=(
            "Opt into pre-release versions (beta / rc). Without this flag (the "
            "default) and without KBAGENT_INCLUDE_PRERELEASE=1 in env, the "
            "GitHub /releases/latest endpoint -- which filters out prereleases "
            "server-side -- is used, so a beta release will never silently "
            "install. With --beta, the resolver opts into PEP 440 prereleases "
            "(``uv --prerelease=allow`` / ``pip --pre``)."
        ),
    ),
) -> None:
    """Update kbagent + keboola-mcp-server to the latest versions.

    Two-stage upgrade (since v0.30.1):

    1. **keboola-mcp-server** -- detects install method and runs the
       matching upgrade command (``uv tool upgrade`` / ``pip install -U``
       / ``uvx --refresh``).
    2. **kbagent** -- final terminal stage: ``uv tool install --force
       --reinstall`` from an exact release wheel or Git tag. This recreates
       the whole tool environment and preserves ``[server]`` extras. On a
       failure the result includes a copy/paste recovery command.

    On Windows the kbagent stage is **scheduled, not run here** (issue #528).
    ``uv tool install`` removes the tool environment before recreating it, so
    running it from inside that environment deletes files the live process has
    locked and aborts half-way. A detached helper waits until every kbagent has
    exited and installs then; ``deferred: true`` marks that in JSON and the next
    launch reports the outcome.

    JSON output reports both stages independently. Human mode prints a
    one-line summary such as
    ``kbagent v0.30.0 -> v0.30.1 | keboola-mcp-server v1.49.0 -> v1.59.1``.

    Pre-release channel (since v0.42.0): use ``--beta`` (or set
    ``KBAGENT_INCLUDE_PRERELEASE=1`` in env) to opt into beta / rc
    releases. The startup auto-update hook never auto-installs a beta;
    only this explicit command does.
    """
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    include_prerelease = beta or _env_opted_into_prerelease()
    result = version_service.self_update(include_prerelease=include_prerelease)

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
