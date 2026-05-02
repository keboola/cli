"""Init command - initialize a local .kbagent/ workspace in the current directory."""

import json
import stat
import sys
from pathlib import Path

import typer

from ..config_store import ConfigStore
from ..constants import LOCAL_CONFIG_DIR_NAME
from ..errors import ErrorCode
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

    # Check if global config has projects to offer
    global_store: ConfigStore = get_service(ctx, "config_store")
    copy_from_global = from_global

    if not from_global and global_store.source == "global":
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
                "CONFIG_ERROR",
                "Cannot use --from-global: active config is not the global config. "
                "Run from a directory without an existing .kbagent/ workspace.",
            )
            raise typer.Exit(code=5)
        config = global_store.load()

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
        # Read-only workspaces are typically AI-agent sandboxes. Env vars
        # sit OUTSIDE the kbagent permission firewall, so a sandboxed
        # agent with KBC_MANAGE_API_TOKEN in its env can `curl` the
        # Manage API directly while --deny-writes silently lets it
        # through. Default to refusing env-var manage tokens to close
        # that exfiltration window; the operator can re-enable later
        # via `kbagent permissions allow-manage-env` if they need to
        # (likewise, `kbagent permissions deny-manage-env` is the same
        # lever for an existing non-read-only workspace).
        config.allow_env_manage_token = False

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
        # Defense-in-depth parity for the manage-env policy commands
        # (since v0.27.1). The random-code TTY confirmation is the
        # primary gate; these deny rules add a second layer at the
        # Claude Code permission boundary so the agent never even
        # attempts to invoke them programmatically.
        "Bash(kbagent permissions deny-manage-env*)",
        "Bash(kbagent permissions allow-manage-env*)",
        "Bash(*permissions deny-manage-env*)",
        "Bash(*permissions allow-manage-env*)",
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
