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

from .constants import ENV_CONFIG_DIR, LOCAL_CONFIG_DIR_NAME
from .errors import ConfigError
from .models import AppConfig, ProjectConfig

logger = logging.getLogger(__name__)

CURRENT_CONFIG_VERSION = 1

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
            return AppConfig()

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
            return AppConfig.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"Config file has invalid structure: {exc}") from exc

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
            json_str = config.model_dump_json(indent=2)
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
            os.replace(str(tmp_path), str(self._config_path))
        except OSError as exc:
            raise ConfigError(f"Cannot write config file {self._config_path}: {exc}") from exc
        finally:
            if lock_fd is not None:
                _try_flock(lock_fd, _LOCK_UN)
                os.close(lock_fd)

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
        config.projects[alias].active_branch_id = branch_id
        self.save(config)

    def edit_project(self, alias: str, **kwargs: str | int | None) -> None:
        """Update fields on an existing project.

        Only non-None keyword arguments are applied.

        Args:
            alias: The project alias to edit.
            **kwargs: Fields to update (stack_url, token, project_name, project_id).

        Raises:
            ConfigError: If the alias does not exist.
        """
        config = self.load()
        if alias not in config.projects:
            raise ConfigError(f"Project '{alias}' not found.")
        project = config.projects[alias]
        for key, value in kwargs.items():
            if hasattr(project, key) and value is not None:
                setattr(project, key, value)
        config.projects[alias] = project
        self.save(config)
