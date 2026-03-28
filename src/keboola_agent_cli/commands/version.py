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
    """Format an auto-updating dependency (runs via uvx @latest)."""
    name = dep["name"]
    desc = dep["description"]
    latest = dep.get("latest_version")
    uvx_available = dep.get("uvx_available", False)

    label = f"{name} ({desc})"
    text.append(f"  {label:<28}")

    if not uvx_available:
        text.append("uvx not found", style="red")
        text.append(" (install: brew install uv)", style="dim")
    elif latest:
        text.append(f"v{latest}", style="green")
        text.append("    auto-updates", style="dim")
    else:
        text.append("available", style="green")
        text.append("    (version check failed)", style="dim")

    text.append("\n")


def _format_version_panel(console: Console, data: dict) -> None:
    """Render version information as a Rich panel."""
    text = Text()
    kbagent = data["kbagent"]
    text.append(f"kbagent v{kbagent['version']}", style="bold")

    if kbagent.get("up_to_date") is False and kbagent.get("latest_version"):
        text.append(f"    -> v{kbagent['latest_version']} available", style="yellow")
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


def version_command(ctx: typer.Context) -> None:
    """Show kbagent version and check for dependency updates."""
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    result = version_service.get_versions()
    formatter.output(result, _format_version_panel)


def update_command(ctx: typer.Context) -> None:
    """Update kbagent to the latest version.

    Uses 'uv tool install --upgrade' (preferred) or 'pip install --upgrade'
    to install the latest version from the GitHub repository.
    """
    formatter = get_formatter(ctx)
    version_service = get_service(ctx, "version_service")
    result = version_service.self_update()

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result["updated"]:
            formatter.success(result["message"])
        else:
            formatter.console.print(result["message"])
