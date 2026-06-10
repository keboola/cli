"""Init command - initialize a local .kbagent/ workspace in the current directory."""

import json
import stat
import sys
from pathlib import Path

import typer

from ..config_store import ConfigStore
from ..constants import ENV_PROJECT_FROM_ENV, LOCAL_CONFIG_DIR_NAME
from ..errors import ErrorCode
from ..models import AppConfig, PermissionPolicy
from ..output import OutputFormatter
from ._helpers import get_formatter, get_service


def init_command(
    ctx: typer.Context,
    from_global: bool = typer.Option(
        False,
        "--from-global",
        help="Copy projects from the global config into the new local workspace.",
    ),
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Copy only the named project(s) from the global config (repeatable). "
        "Implies --from-global. Without it, all global projects are copied.",
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

    # Check if global config has projects to offer
    global_store: ConfigStore = get_service(ctx, "config_store")
    selected_aliases = project or []
    # An explicit --project filter implies --from-global: there is nowhere else
    # to copy the named projects from, so opt into copying automatically.
    copy_from_global = from_global or bool(selected_aliases)

    if not copy_from_global and global_store.source == "global":
        try:
            global_config = global_store.load()
            project_count = len(global_config.projects)
            if project_count > 0:
                is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
                if not formatter.json_mode and is_tty:
                    copy_from_global = typer.confirm(
                        f"Global config has {project_count} project(s). Copy to local workspace?",
                        default=True,
                    )
                else:
                    formatter.warning(
                        f"Global config has {project_count} project(s) that won't be "
                        "available in local workspace. Use --from-global to copy them."
                    )
        except Exception:
            pass  # Global config unreadable, proceed with empty

    if copy_from_global:
        if global_store.source != "global":
            formatter.error(
                message=(
                    "Cannot use --from-global: active config is not the global config. "
                    "Run from a directory without an existing .kbagent/ workspace."
                ),
                error_code=ErrorCode.CONFIG_ERROR,
            )
            raise typer.Exit(code=5)
        config = global_store.load()
        if selected_aliases:
            config = _filter_global_projects(config, selected_aliases, formatter)

    if read_only and len(config.projects) == 0:
        # Read-only locks the config (chmod 0400 + cli:write deny), so an empty
        # read-only workspace cannot accept any project via `project add` afterwards.
        # Refuse to create a useless locked workspace and guide the user to the
        # correct order: init -> project add -> permissions set.
        formatter.error(
            message=(
                "Cannot init --read-only with an empty workspace: read-only blocks "
                "'project add', so you would be locked out. Correct workflow: "
                "1) 'kbagent init' (without --read-only), "
                "2) 'kbagent project add ...', "
                "3) 'kbagent permissions set --mode allow --deny cli:write --deny tool:write' to lock. "
                "Alternatively, use --from-global to seed projects from the global config."
            ),
            error_code=ErrorCode.CONFIG_ERROR,
        )
        raise typer.Exit(code=5)

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
    if copy_from_global and project_count > 0:
        message += f" (copied {project_count} project(s) from global config)"
    if read_only:
        message += " [read-only mode]"

    formatter.output(
        {
            "message": message,
            "path": str(local_dir),
            "created": True,
            "projects_copied": project_count if copy_from_global else 0,
            "read_only": read_only,
        }
    )


def _filter_global_projects(
    config: AppConfig,
    selected_aliases: list[str],
    formatter: OutputFormatter,
) -> AppConfig:
    """Narrow a global config copy down to the named projects.

    Validates that every requested alias exists, keeps only the selected
    projects (deduplicated, preserving the order given on the CLI), and
    repoints ``default_project`` if the global default fell outside the
    selection. ``dev_portal_identities`` and ``permissions`` are not
    project-scoped, so they are left untouched.

    Args:
        config: The freshly loaded global config (mutated in place).
        selected_aliases: Aliases passed via repeated ``--project`` flags.
        formatter: Output formatter for emitting the error on an unknown alias.

    Raises:
        typer.Exit: Exit code 5 if any requested alias is missing.
    """
    missing = [alias for alias in selected_aliases if alias not in config.projects]
    if missing:
        available = ", ".join(sorted(config.projects)) or "(none)"
        formatter.error(
            message=(
                f"Unknown project alias(es): {', '.join(missing)}. "
                f"Available in global config: {available}"
            ),
            error_code=ErrorCode.CONFIG_ERROR,
        )
        raise typer.Exit(code=5)

    # Reject env-synthesized projects: a `__env__` project lives only in memory
    # and is stripped on save, so copying it would report success and then
    # silently vanish on the next load. Fail clearly instead -- mirrors
    # `_reject_ephemeral_mutation` (issue #359).
    ephemeral = [alias for alias in selected_aliases if config.projects[alias].ephemeral]
    if ephemeral:
        formatter.error(
            message=(
                "Cannot copy env-synthesized project(s) into a local workspace: "
                f"{', '.join(ephemeral)}. They are derived from "
                f"{ENV_PROJECT_FROM_ENV} and live only in memory, so they would be "
                "stripped on save. Use 'project add' to persist a project instead."
            ),
            error_code=ErrorCode.CONFIG_ERROR,
        )
        raise typer.Exit(code=5)

    # Dedupe while preserving CLI order (dict keeps insertion order).
    filtered = {alias: config.projects[alias] for alias in selected_aliases}
    config.projects = filtered
    if config.default_project not in filtered:
        # Match the codebase fallback convention (config_store uses the same
        # 2-arg form); `filtered` is non-empty here, so "" is never returned.
        config.default_project = next(iter(filtered), "")
    return config


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
