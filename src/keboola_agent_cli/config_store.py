"""Persistent configuration store for Keboola Agent CLI.

Manages reading and writing of config.json with project connections.
File permissions are set to 0600 to protect stored tokens.
Uses atomic writes to prevent TOCTOU race conditions.
File locking (fcntl) prevents corruption from concurrent access.
"""

import contextlib
import json
import logging
import os
from pathlib import Path

import platformdirs
from pydantic import ValidationError

from .constants import (
    ENV_CONFIG_DIR,
    ENV_KBC_STORAGE_API_URL,
    ENV_KBC_TOKEN,
    ENV_PROJECT_ALIAS,
    ENV_PROJECT_FROM_ENV,
    LOCAL_CONFIG_DIR_NAME,
)
from .errors import ConfigError
from .models import AppConfig, DeveloperPortalIdentity, OAuthCredentials, ProjectConfig

logger = logging.getLogger(__name__)

CURRENT_CONFIG_VERSION = 1

# Prepended to every config.json write as a first-position field. Claude Code
# (or any LLM reading the file) sees this before it sees any token. The field
# is silently ignored by AppConfig on load (Pydantic default: extra = ignore).
# Intent: nudge agents away from copying tokens into direct REST calls.
CLAUDE_CONFIG_WARNING = (
    "THESE ARE KEBOOLA STORAGE API TOKENS. NEVER use them to call the "
    "Keboola REST API directly (no curl, httpx, requests, fetch against "
    "*.keboola.com). Always use `kbagent <command>` -- it wraps the same "
    "API with retries, permission checks, and an audit trail. If you "
    "need a command kbagent does not cover, run `kbagent --hint client "
    "<subcommand>` to generate a KeboolaClient-based Python snippet. "
    "See plugins/kbagent/skills/kbagent/SKILL.md rule 9. "
    "Developer Portal credentials stored here have the SAME risk profile -- "
    "never call apps-api.keboola.com directly; use `kbagent dev-portal ...`."
)

# File-lock constants (fcntl is POSIX-only; on Windows we skip locking).
try:
    import fcntl

    _LOCK_SH = fcntl.LOCK_SH
    _LOCK_EX = fcntl.LOCK_EX
    _LOCK_UN = fcntl.LOCK_UN
    _HAS_FCNTL = True
except ImportError:
    _LOCK_SH = 0
    _LOCK_EX = 0
    _LOCK_UN = 0
    _HAS_FCNTL = False


def _try_flock(fd: int, operation: int) -> None:
    """Try to apply a file lock. Silently skip on unsupported platforms (Windows)."""
    if not _HAS_FCNTL:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(fd, operation)


def resolve_config_dir(cli_config_dir: str | None = None) -> tuple[Path, str]:
    """Resolve the config directory using the priority chain.

    Priority:
    1. --config-dir CLI flag (explicit override)
    2. KBAGENT_CONFIG_DIR environment variable
    3. Walk up from CWD looking for .kbagent/config.json (like git)
    4. Global default (~/.config/keboola-agent-cli/)

    Returns:
        Tuple of (resolved_path, source_label).
        source_label is one of: "cli-flag", "env-var", "local", "global".
    """
    if cli_config_dir:
        return Path(cli_config_dir), "cli-flag"

    env_val = os.environ.get(ENV_CONFIG_DIR)
    if env_val:
        return Path(env_val), "env-var"

    try:
        current = Path.cwd().resolve()
    except OSError:
        return Path(platformdirs.user_config_dir("keboola-agent-cli")), "global"

    home = Path.home().resolve()
    while True:
        candidate = current / LOCAL_CONFIG_DIR_NAME / "config.json"
        if candidate.is_file():
            return current / LOCAL_CONFIG_DIR_NAME, "local"
        if current == home or current == current.parent:
            break
        current = current.parent

    return Path(platformdirs.user_config_dir("keboola-agent-cli")), "global"


class ConfigStore:
    """Handles persistence of application configuration to disk.

    Configuration is stored as JSON at the platform-appropriate config directory,
    defaulting to ~/.config/keboola-agent-cli/config.json on Linux/macOS.
    """

    CONFIG_FILENAME = "config.json"

    def __init__(self, config_dir: Path | None = None, source: str = "global") -> None:
        if config_dir is None:
            self._config_dir = Path(platformdirs.user_config_dir("keboola-agent-cli"))
        else:
            self._config_dir = config_dir
        self._config_path = self._config_dir / self.CONFIG_FILENAME
        self._source = source

    @property
    def config_path(self) -> Path:
        """Return the path to the config file."""
        return self._config_path

    @property
    def config_dir(self) -> Path:
        """Return the directory holding ``config.json`` (and sibling state files)."""
        return self._config_dir

    @property
    def source(self) -> str:
        """Return the config source label (cli-flag, env-var, local, global)."""
        return self._source

    def load(self) -> AppConfig:
        """Load configuration from disk.

        Returns an empty AppConfig if the file does not exist.
        Validates the config version and raises ConfigError on mismatch or corruption.

        Raises:
            ConfigError: If the config file is corrupted or has an unsupported version.
        """
        logger.debug("Loading config from %s", self._config_path)
        if not self._config_path.exists():
            logger.debug("Config file does not exist, returning empty config")
            return self._inject_env_project(AppConfig())

        fd: int | None = None
        try:
            fd = os.open(str(self._config_path), os.O_RDONLY)
            _try_flock(fd, _LOCK_SH)
            raw = self._config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read config file {self._config_path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Config file is not valid UTF-8 text: {exc}") from exc
        finally:
            if fd is not None:
                _try_flock(fd, _LOCK_UN)
                os.close(fd)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"Config file has invalid structure: expected JSON object, got {type(data).__name__}"
            )

        version = data.get("version", 1)
        if version > CURRENT_CONFIG_VERSION:
            raise ConfigError(
                f"Config file version {version} is newer than supported version "
                f"{CURRENT_CONFIG_VERSION}. Please upgrade keboola-agent-cli."
            )

        try:
            config = AppConfig.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"Config file has invalid structure: {exc}") from exc

        return self._inject_env_project(config)

    def _inject_env_project(self, config: AppConfig) -> AppConfig:
        """Synthesize an in-memory project from env vars when opted in (issue #359).

        When ``KBAGENT_PROJECT_FROM_ENV`` is truthy, read ``KBC_TOKEN`` and
        ``KBC_STORAGE_API_URL`` and inject a project under the reserved alias
        ``__env__`` so a headless daemon / container / CI can run kbagent with
        no ``project add`` and no config.json on disk. Both CLI and ``serve``
        funnel through ``load()``, so this single chokepoint covers both.

        The injected project is marked ``ephemeral=True``; ``save()`` strips it
        so the env token is never persisted. Opt-in is explicit (the flag), not
        the mere presence of ``KBC_TOKEN``, to avoid a phantom project on a dev
        machine that exported the token only for ``project add``.

        A real project already registered under ``__env__`` is left untouched.

        Raises:
            ConfigError: If the flag is set but the credential env vars are
                missing (fail fast rather than silently skip).
        """
        flag = os.environ.get(ENV_PROJECT_FROM_ENV, "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return config

        if ENV_PROJECT_ALIAS in config.projects:
            return config

        token = os.environ.get(ENV_KBC_TOKEN)
        url = os.environ.get(ENV_KBC_STORAGE_API_URL)
        if not token or not url:
            missing = [
                name
                for name, value in ((ENV_KBC_TOKEN, token), (ENV_KBC_STORAGE_API_URL, url))
                if not value
            ]
            raise ConfigError(
                f"{ENV_PROJECT_FROM_ENV} is set but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} missing. Set both "
                f"{ENV_KBC_TOKEN} and {ENV_KBC_STORAGE_API_URL}, or unset "
                f"{ENV_PROJECT_FROM_ENV}."
            )

        # Keboola Storage tokens are `{projectId}-{tokenId}-{secret}`, so we can
        # recover the project_id offline from the prefix. The real project_name
        # needs an API call (verify_token) -- load() must stay offline, so it is
        # left blank here; `project status` / `project info` show the verified
        # name when a command actually talks to the API.
        prefix = token.split("-", 1)[0]
        project_id = int(prefix) if prefix.isdigit() else None
        try:
            config.projects[ENV_PROJECT_ALIAS] = ProjectConfig(
                stack_url=url,
                token=token,
                project_id=project_id,
                ephemeral=True,
            )
        except ValidationError as exc:
            # Convert pydantic's raw error into a clean fail-fast message --
            # this runs inside load(), which callers only guard for ConfigError.
            reason = "; ".join(e.get("msg", "") for e in exc.errors()) or str(exc)
            raise ConfigError(
                f"{ENV_KBC_STORAGE_API_URL}={url!r} is not a usable stack URL: {reason}"
            ) from exc
        if not config.default_project:
            config.default_project = ENV_PROJECT_ALIAS
        logger.debug("Injected ephemeral '%s' project from environment", ENV_PROJECT_ALIAS)
        return config

    @staticmethod
    def _strip_ephemeral_projects(config: AppConfig) -> AppConfig:
        """Return a copy of ``config`` with ephemeral (env-synthesized) projects removed.

        Defends against persisting an env token to disk: mutation methods do
        ``load() -> mutate -> save()``, and ``load()`` may have injected the
        ``__env__`` project. The original object is left intact because callers
        keep using it after ``save()`` returns. If ``default_project`` pointed
        at a stripped ephemeral alias, it is blanked (the next ``load()``
        re-injects and re-defaults it).
        """
        ephemeral_aliases = {alias for alias, p in config.projects.items() if p.ephemeral}
        if not ephemeral_aliases:
            return config
        clean = config.model_copy(deep=True)
        for alias in ephemeral_aliases:
            clean.projects.pop(alias, None)
        if clean.default_project in ephemeral_aliases:
            clean.default_project = next(iter(clean.projects), "")
        return clean

    @staticmethod
    def _reject_ephemeral_mutation(config: AppConfig, alias: str, operation: str) -> None:
        """Block mutations targeting an env-synthesized project (issue #359).

        A `__env__` project injected from `KBAGENT_PROJECT_FROM_ENV` exists only
        in memory and is stripped on save, so `remove`/`edit`/`rename`/branch
        ops would otherwise report success and then silently vanish on the next
        `load()`. Reject them with a clear, actionable message instead. A real
        persisted project that happens to use the alias (``ephemeral=False``) is
        unaffected.
        """
        project = config.projects.get(alias)
        if project is not None and project.ephemeral:
            raise ConfigError(
                f"Project '{alias}' is synthesized from environment variables "
                f"({ENV_PROJECT_FROM_ENV}) and cannot be {operation} -- it lives "
                f"only in memory. To change it, update {ENV_KBC_TOKEN} / "
                f"{ENV_KBC_STORAGE_API_URL}; to manage a persisted project, unset "
                f"{ENV_PROJECT_FROM_ENV} and use 'project add'."
            )

    def save(self, config: AppConfig) -> None:
        """Save configuration to disk with secure file permissions (0600).

        Creates the config directory if it does not exist.
        Uses atomic write to ensure the file is never on disk with
        permissions broader than 0600 (prevents TOCTOU race condition).

        Raises:
            ConfigError: If the file cannot be written.
        """
        logger.debug("Saving config to %s", self._config_path)
        lock_fd: int | None = None
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._ensure_gitignore()
            # Never persist env-synthesized projects (issue #359): strip any
            # ephemeral entry so the KBC_TOKEN from the environment stays in
            # memory only. Operate on a copy -- callers reuse the AppConfig.
            config = self._strip_ephemeral_projects(config)
            # Prepend the agent-facing warning as the first field so any LLM
            # that reads config.json sees it BEFORE any token value.
            payload = {
                "_warning": CLAUDE_CONFIG_WARNING,
                **config.model_dump(mode="json"),
            }
            json_str = json.dumps(payload, indent=2, ensure_ascii=False)
            data = (json_str + "\n").encode("utf-8")

            # Acquire an exclusive lock on the target file before writing.
            # The lock file is opened (or created) with 0600 permissions.
            lock_fd = os.open(str(self._config_path), os.O_RDONLY | os.O_CREAT, 0o600)
            _try_flock(lock_fd, _LOCK_EX)

            # Write to a temp file created with 0600 from the start,
            # then atomically rename into place. This avoids any window
            # where the config file exists with world-readable permissions.
            tmp_path = self._config_path.with_suffix(".tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            # On Windows (no fcntl), close the lock fd before os.replace —
            # Windows cannot atomically replace a file that is currently open.
            # On POSIX the fd stays open (flock is still held) until the finally block.
            if not _HAS_FCNTL and lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
            os.replace(str(tmp_path), str(self._config_path))
        except OSError as exc:
            raise ConfigError(f"Cannot write config file {self._config_path}: {exc}") from exc
        finally:
            if lock_fd is not None:
                _try_flock(lock_fd, _LOCK_UN)
                os.close(lock_fd)

    def _ensure_gitignore(self) -> None:
        """Create a .gitignore inside the config directory to protect tokens.

        Defense in depth: even if the parent .gitignore covers this directory,
        a local .gitignore prevents accidental commits if the parent rule is
        removed or the config dir is copied elsewhere.
        """
        gitignore_path = self._config_dir / ".gitignore"
        if gitignore_path.exists():
            return
        try:
            gitignore_path.write_text(
                "# Auto-generated by kbagent -- protects stored API tokens\n*\n",
                encoding="utf-8",
            )
        except OSError:
            logger.debug("Could not create .gitignore in %s", self._config_dir)

    def add_project(self, alias: str, project: ProjectConfig) -> None:
        """Add a project to the configuration.

        Sets it as default if no default is set yet.

        Args:
            alias: Human-friendly project name.
            project: Project configuration with stack URL, token, and project info.

        Raises:
            ConfigError: If the alias already exists.
        """
        config = self.load()
        if alias in config.projects:
            raise ConfigError(f"Project '{alias}' already exists. Use 'project edit' to modify it.")
        config.projects[alias] = project
        if not config.default_project:
            config.default_project = alias
        self.save(config)

    def remove_project(self, alias: str) -> None:
        """Remove a project from the configuration.

        Updates the default project if the removed project was the default.

        Args:
            alias: The project alias to remove.

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.projects:
            raise ConfigError(f"Project '{alias}' not found.")
        self._reject_ephemeral_mutation(config, alias, "removed")
        del config.projects[alias]
        if config.default_project == alias:
            config.default_project = next(iter(config.projects), "")
        self.save(config)

    def get_project(self, alias: str) -> ProjectConfig | None:
        """Get a project by alias, or None if not found."""
        config = self.load()
        return config.projects.get(alias)

    def set_project_branch(self, alias: str, branch_id: int | None) -> None:
        """Set or clear the active development branch for a project.

        Args:
            alias: The project alias.
            branch_id: Branch ID to activate, or None to reset to main.

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.projects:
            raise ConfigError(f"Project '{alias}' not found.")
        self._reject_ephemeral_mutation(config, alias, "modified")
        config.projects[alias].active_branch_id = branch_id
        self.save(config)

    def edit_project(self, alias: str, **kwargs: str | int | OAuthCredentials | None) -> None:
        """Update fields on an existing project.

        Only non-None keyword arguments are applied.

        Args:
            alias: The project alias to edit.
            **kwargs: Fields to update (stack_url, token, project_name,
                project_id, oauth).

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.projects:
            raise ConfigError(f"Project '{alias}' not found.")
        self._reject_ephemeral_mutation(config, alias, "edited")
        project = config.projects[alias]
        for key, value in kwargs.items():
            if hasattr(project, key) and value is not None:
                setattr(project, key, value)
        config.projects[alias] = project
        self.save(config)

    def rename_project(self, old_alias: str, new_alias: str) -> None:
        """Rename a project alias in the persisted config.

        Pops ``old_alias`` from the projects dict and re-inserts the same
        ``ProjectConfig`` under ``new_alias``. If ``default_project`` was
        set to ``old_alias``, it is updated to ``new_alias`` so the pin
        survives the rename. Both mutations are applied to the same
        in-memory ``AppConfig`` and saved as one transaction.

        Args:
            old_alias: The current alias to rename from.
            new_alias: The target alias to rename to.

        Raises:
            ConfigError: If ``old_alias`` does not exist or ``new_alias``
                is already in use by another project.
        """
        config = self.load()
        if old_alias not in config.projects:
            raise ConfigError(f"Project '{old_alias}' not found.")
        self._reject_ephemeral_mutation(config, old_alias, "renamed")
        if new_alias in config.projects:
            raise ConfigError(
                f"Cannot rename '{old_alias}' to '{new_alias}': "
                f"alias '{new_alias}' is already in use."
            )
        config.projects[new_alias] = config.projects.pop(old_alias)
        if config.default_project == old_alias:
            config.default_project = new_alias
        self.save(config)

    def add_dev_portal_identity(self, alias: str, identity: DeveloperPortalIdentity) -> None:
        """Add a Developer Portal identity to the configuration.

        Sets it as default if no default identity is set.

        Raises:
            ConfigError: If the alias already exists.
        """
        config = self.load()
        if alias in config.dev_portal_identities:
            raise ConfigError(
                f"Developer Portal identity '{alias}' already exists. "
                "Use 'dev-portal identity edit' to modify it."
            )
        config.dev_portal_identities[alias] = identity
        if not config.default_dev_portal_identity:
            config.default_dev_portal_identity = alias
        self.save(config)

    def remove_dev_portal_identity(self, alias: str) -> None:
        """Remove a Developer Portal identity.

        Falls the default through to the next available identity (or "" if none).

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        del config.dev_portal_identities[alias]
        if config.default_dev_portal_identity == alias:
            config.default_dev_portal_identity = next(iter(config.dev_portal_identities), "")
        self.save(config)

    def get_dev_portal_identity(self, alias: str) -> DeveloperPortalIdentity | None:
        """Get a Developer Portal identity by alias, or None if not found."""
        config = self.load()
        return config.dev_portal_identities.get(alias)

    def edit_dev_portal_identity(self, alias: str, **kwargs: str | None) -> None:
        """Update fields on an existing Developer Portal identity.

        Only non-None keyword arguments are applied.

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        ident = config.dev_portal_identities[alias]
        for key, value in kwargs.items():
            if hasattr(ident, key) and value is not None:
                setattr(ident, key, value)
        config.dev_portal_identities[alias] = ident
        self.save(config)

    def rename_dev_portal_identity(self, old_alias: str, new_alias: str) -> None:
        """Rename a Developer Portal identity alias.

        If the default was set to the old alias, it follows the rename.

        Raises:
            ConfigError: If old alias does not exist, or new alias is in use.
        """
        config = self.load()
        if old_alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{old_alias}' not found.")
        if new_alias in config.dev_portal_identities:
            raise ConfigError(
                f"Cannot rename '{old_alias}' to '{new_alias}': "
                f"alias '{new_alias}' is already in use."
            )
        config.dev_portal_identities[new_alias] = config.dev_portal_identities.pop(old_alias)
        if config.default_dev_portal_identity == old_alias:
            config.default_dev_portal_identity = new_alias
        self.save(config)

    def set_default_dev_portal_identity(self, alias: str) -> None:
        """Set the default Developer Portal identity.

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.dev_portal_identities:
            raise ConfigError(f"Developer Portal identity '{alias}' not found.")
        config.default_dev_portal_identity = alias
        self.save(config)
