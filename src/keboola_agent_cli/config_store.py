"""Persistent configuration store for Keboola Agent CLI.

Manages reading and writing of config.json with project connections.
"""

from pathlib import Path

import platformdirs

from .models import AppConfig, ProjectConfig


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
        """
        if not self._config_path.exists():
            return AppConfig()
        raw = self._config_path.read_text(encoding="utf-8")
        return AppConfig.model_validate_json(raw)

    def save(self, config: AppConfig) -> None:
        """Save configuration to disk with secure file permissions (0600)."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        json_str = config.model_dump_json(indent=2)
        self._config_path.write_text(json_str + "\n", encoding="utf-8")
        self._config_path.chmod(0o600)

    def add_project(self, alias: str, project: ProjectConfig) -> None:
        """Add a project to the configuration."""
        config = self.load()
        config.projects[alias] = project
        if not config.default_project:
            config.default_project = alias
        self.save(config)

    def remove_project(self, alias: str) -> None:
        """Remove a project from the configuration."""
        config = self.load()
        config.projects.pop(alias, None)
        if config.default_project == alias:
            config.default_project = next(iter(config.projects), "")
        self.save(config)

    def get_project(self, alias: str) -> ProjectConfig | None:
        """Get a project by alias, or None if not found."""
        config = self.load()
        return config.projects.get(alias)

    def edit_project(self, alias: str, **kwargs: str | int) -> None:
        """Update fields on an existing project."""
        config = self.load()
        if alias not in config.projects:
            return
        project = config.projects[alias]
        for key, value in kwargs.items():
            if hasattr(project, key) and value is not None:
                setattr(project, key, value)
        config.projects[alias] = project
        self.save(config)
