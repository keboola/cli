"""Persistent configuration store for Keboola Agent CLI.

Manages reading and writing of config.json with project connections.
File permissions are set to 0600 to protect stored tokens.
"""

import json
from pathlib import Path

import platformdirs

from .errors import ConfigError
from .models import AppConfig, ProjectConfig

CURRENT_CONFIG_VERSION = 1


class ConfigStore:
    """Handles persistence of application configuration to disk.

    Configuration is stored as JSON at the platform-appropriate config directory,
    defaulting to ~/.config/keboola-agent-cli/config.json on Linux/macOS.
    """

    CONFIG_FILENAME = "config.json"

    def __init__(self, config_dir: Path | None = None) -> None:
        if config_dir is None:
            self._config_dir = Path(platformdirs.user_config_dir("keboola-agent-cli"))
        else:
            self._config_dir = config_dir
        self._config_path = self._config_dir / self.CONFIG_FILENAME

    @property
    def config_path(self) -> Path:
        """Return the path to the config file."""
        return self._config_path

    def load(self) -> AppConfig:
        """Load configuration from disk.

        Returns an empty AppConfig if the file does not exist.
        Validates the config version and raises ConfigError on mismatch or corruption.

        Raises:
            ConfigError: If the config file is corrupted or has an unsupported version.
        """
        if not self._config_path.exists():
            return AppConfig()

        try:
            raw = self._config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read config file {self._config_path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Config file is not valid UTF-8 text: {exc}") from exc

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

        Raises:
            ConfigError: If the file cannot be written.
        """
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            json_str = config.model_dump_json(indent=2)
            self._config_path.write_text(json_str + "\n", encoding="utf-8")
            self._config_path.chmod(0o600)
        except OSError as exc:
            raise ConfigError(f"Cannot write config file {self._config_path}: {exc}") from exc

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
