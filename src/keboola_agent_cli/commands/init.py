"""Init command - initialize a local .kbagent/ workspace in the current directory."""

import json
import stat
from pathlib import Path

import typer

from ..config_store import ConfigStore
from ..constants import LOCAL_CONFIG_DIR_NAME
from ..models import AppConfig, PermissionPolicy
from ._helpers import get_formatter, get_service


def init_command(
    ctx: typer.Context,
    from_global: bool = typer.Option(
        False,
        "--from-global",
        help="Copy projects from the global config into the new local workspace.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Set read-only permission policy (blocks all write CLI commands and MCP tools).",
    ),
) -> None:
    """Initialize a local .kbagent/ workspace in the current directory."""
    formatter = get_formatter(ctx)
    cwd = Path.cwd()
    local_dir = cwd / LOCAL_CONFIG_DIR_NAME
    config_path = local_dir / "config.json"

    if config_path.is_file():
        formatter.output(
            {
                "message": f"Already initialized at {local_dir}",
                "path": str(local_dir),
                "created": False,
            }
        )
        return

    config = AppConfig()
    if from_global:
        global_store: ConfigStore = get_service(ctx, "config_store")
        if global_store.source != "global":
            formatter.error(
                "CONFIG_ERROR",
                "Cannot use --from-global: active config is not the global config. "
                "Run from a directory without an existing .kbagent/ workspace.",
            )
            raise typer.Exit(code=5)
        config = global_store.load()

    if read_only:
        config.permissions = PermissionPolicy(
            mode="allow",
            deny=["cli:write", "tool:write"],
        )

    local_store = ConfigStore(config_dir=local_dir, source="local")
    local_store.save(config)

    if read_only:
        # Make config.json owner-read-only so other users (agent) can't read or write it.
        # kbagent itself runs as the owner and can still read it.
        config_path.chmod(stat.S_IRUSR)  # 0400
        # Create .claude/settings.json to prevent Claude Code from touching the config
        _create_claude_settings(cwd, local_dir)

    _update_gitignore(cwd)

    project_count = len(config.projects)
    message = f"Initialized local workspace at {local_dir}"
    if from_global and project_count > 0:
        message += f" (copied {project_count} project(s) from global config)"
    if read_only:
        message += " [read-only mode]"

    formatter.output(
        {
            "message": message,
            "path": str(local_dir),
            "created": True,
            "projects_copied": project_count if from_global else 0,
            "read_only": read_only,
        }
    )


def _create_claude_settings(project_dir: Path, kbagent_dir: Path) -> None:
    """Create .claude/settings.json to prevent Claude Code from modifying the config.

    This is a defense-in-depth measure: even if Claude Code somehow bypasses
    the permission policy, it cannot edit the config file or run commands
    that would change the policy.
    """
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"

    # Relative path from project root to kbagent config
    config_rel = f"{kbagent_dir.name}/config.json"

    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = {}

    permissions = settings.setdefault("permissions", {})
    deny_list: list[str] = permissions.get("deny", [])

    # Rules to add:
    # 1. Block direct file operations on config.json
    # 2. Block any Bash command that mentions the config file (chmod, cat >, python, sed, etc.)
    # 3. Block permission-changing CLI commands
    # 4. Block --config-dir bypass (pointing to global config without policy)
    # 5. Block reading the config (agent doesn't need to -- kbagent reads it internally)
    new_rules = [
        f"Read({config_rel})",
        f"Edit({config_rel})",
        f"Write({config_rel})",
        f"Bash(*{config_rel}*)",
        f"Bash(*chmod*{kbagent_dir.name}*)",
        "Bash(kbagent permissions set*)",
        "Bash(kbagent permissions reset*)",
        "Bash(*permissions set*)",
        "Bash(*permissions reset*)",
        "Bash(*--config-dir*)",
        "Bash(*KBAGENT_CONFIG_DIR*)",
    ]
    for rule in new_rules:
        if rule not in deny_list:
            deny_list.append(rule)

    permissions["deny"] = deny_list
    settings["permissions"] = permissions

    settings_path.write_text(
        json.dumps(settings, indent=2) + "\n",
        encoding="utf-8",
    )


def _update_gitignore(directory: Path) -> None:
    """Append .kbagent/ to .gitignore if not already listed."""
    gitignore_path = directory / ".gitignore"
    entry = f"{LOCAL_CONFIG_DIR_NAME}/"

    if gitignore_path.is_file():
        content = gitignore_path.read_text(encoding="utf-8")
        if entry in content.splitlines():
            return
        if not content.endswith("\n"):
            content += "\n"
        content += entry + "\n"
        gitignore_path.write_text(content, encoding="utf-8")
    else:
        gitignore_path.write_text(entry + "\n", encoding="utf-8")
