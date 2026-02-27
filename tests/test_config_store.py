"""Tests for ConfigStore - load, save, add/remove/edit project, permissions, version check."""

import json
import os
import stat
from pathlib import Path

import pytest

from keboola_agent_cli.config_store import CURRENT_CONFIG_VERSION, ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import AppConfig, ProjectConfig


class TestLoadEmptyConfig:
    """Tests for loading when no config file exists."""

    def test_load_empty_returns_default_appconfig(self, tmp_config_dir: Path) -> None:
        """Loading with no config file returns an empty AppConfig."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config = store.load()

        assert isinstance(config, AppConfig)
        assert config.version == 1
        assert config.default_project == ""
        assert config.projects == {}

    def test_load_creates_no_file(self, tmp_config_dir: Path) -> None:
        """Loading an empty config does not create the config file."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.load()

        assert not (tmp_config_dir / "config.json").exists()


class TestSaveAndLoad:
    """Tests for save/load round-trip."""

    def test_save_and_load_round_trip(self, tmp_config_dir: Path) -> None:
        """Saving and then loading config preserves all data."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config = AppConfig(
            version=1,
            default_project="test",
            projects={
                "test": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-abcdef-12345678",
                    project_name="Test Project",
                    project_id=1234,
                )
            },
        )

        store.save(config)
        loaded = store.load()

        assert loaded.version == 1
        assert loaded.default_project == "test"
        assert "test" in loaded.projects
        assert loaded.projects["test"].stack_url == "https://connection.keboola.com"
        assert loaded.projects["test"].token == "901-abcdef-12345678"
        assert loaded.projects["test"].project_name == "Test Project"
        assert loaded.projects["test"].project_id == 1234

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """Save creates the config directory if it does not exist."""
        nested_dir = tmp_path / "nested" / "config"
        store = ConfigStore(config_dir=nested_dir)
        store.save(AppConfig())

        assert nested_dir.exists()
        assert (nested_dir / "config.json").exists()


class TestFilePermissions:
    """Tests for file permission security."""

    def test_file_permissions_0600(self, tmp_config_dir: Path) -> None:
        """Config file is created with 0600 permissions (owner read/write only)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.save(AppConfig())

        config_file = tmp_config_dir / "config.json"
        file_stat = os.stat(config_file)
        mode = stat.S_IMODE(file_stat.st_mode)

        assert mode == 0o600

    def test_permissions_preserved_on_resave(self, tmp_config_dir: Path) -> None:
        """Permissions remain 0600 after re-saving."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.save(AppConfig())
        store.save(AppConfig(default_project="changed"))

        config_file = tmp_config_dir / "config.json"
        file_stat = os.stat(config_file)
        mode = stat.S_IMODE(file_stat.st_mode)

        assert mode == 0o600


class TestAddProject:
    """Tests for add_project()."""

    def test_add_project_success(self, tmp_config_dir: Path) -> None:
        """Adding a project stores it in config with correct data."""
        store = ConfigStore(config_dir=tmp_config_dir)
        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-abcdef-12345678",
            project_name="Test Project",
            project_id=1234,
        )

        store.add_project("test", project)

        config = store.load()
        assert "test" in config.projects
        assert config.projects["test"].project_name == "Test Project"

    def test_add_first_project_becomes_default(self, tmp_config_dir: Path) -> None:
        """The first added project becomes the default."""
        store = ConfigStore(config_dir=tmp_config_dir)
        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-abcdef-12345678",
        )

        store.add_project("first", project)

        config = store.load()
        assert config.default_project == "first"

    def test_add_second_project_does_not_change_default(self, tmp_config_dir: Path) -> None:
        """Adding a second project does not change the default."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "first",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )
        store.add_project(
            "second",
            ProjectConfig(
                stack_url="https://b.com",
                token="902-abcdef-12345678",
            ),
        )

        config = store.load()
        assert config.default_project == "first"

    def test_add_duplicate_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Adding a project with an existing alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-abcdef-12345678",
        )
        store.add_project("test", project)

        with pytest.raises(ConfigError, match="already exists"):
            store.add_project("test", project)


class TestRemoveProject:
    """Tests for remove_project()."""

    def test_remove_project_success(self, tmp_config_dir: Path) -> None:
        """Removing a project deletes it from config."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )

        store.remove_project("test")

        config = store.load()
        assert "test" not in config.projects

    def test_remove_default_project_updates_default(self, tmp_config_dir: Path) -> None:
        """Removing the default project updates the default to the next available."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "first",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )
        store.add_project(
            "second",
            ProjectConfig(
                stack_url="https://b.com",
                token="902-abcdef-12345678",
            ),
        )

        store.remove_project("first")
        config = store.load()

        assert config.default_project == "second"

    def test_remove_last_project_clears_default(self, tmp_config_dir: Path) -> None:
        """Removing the last project clears the default."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "only",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )

        store.remove_project("only")
        config = store.load()

        assert config.default_project == ""
        assert config.projects == {}

    def test_remove_nonexistent_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Removing a nonexistent alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)

        with pytest.raises(ConfigError, match="not found"):
            store.remove_project("nonexistent")


class TestEditProject:
    """Tests for edit_project()."""

    def test_edit_stack_url(self, tmp_config_dir: Path) -> None:
        """Editing stack_url updates it in the config."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://old.com",
                token="901-abcdef-12345678",
            ),
        )

        store.edit_project("test", stack_url="https://new.com")

        project = store.get_project("test")
        assert project is not None
        assert project.stack_url == "https://new.com"

    def test_edit_token(self, tmp_config_dir: Path) -> None:
        """Editing token updates it in the config."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )

        store.edit_project("test", token="902-newtoken-87654321")

        project = store.get_project("test")
        assert project is not None
        assert project.token == "902-newtoken-87654321"

    def test_edit_multiple_fields(self, tmp_config_dir: Path) -> None:
        """Editing multiple fields at once works."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://old.com",
                token="901-abcdef-12345678",
                project_name="Old Name",
            ),
        )

        store.edit_project("test", stack_url="https://new.com", project_name="New Name")

        project = store.get_project("test")
        assert project is not None
        assert project.stack_url == "https://new.com"
        assert project.project_name == "New Name"

    def test_edit_none_values_ignored(self, tmp_config_dir: Path) -> None:
        """None values in kwargs are ignored and don't overwrite existing data."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )

        store.edit_project("test", stack_url=None, token="new-token-1234abcd")

        project = store.get_project("test")
        assert project is not None
        assert project.stack_url == "https://a.com"  # unchanged
        assert project.token == "new-token-1234abcd"

    def test_edit_nonexistent_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Editing a nonexistent alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)

        with pytest.raises(ConfigError, match="not found"):
            store.edit_project("nonexistent", stack_url="https://new.com")


class TestGetProject:
    """Tests for get_project()."""

    def test_get_existing_project(self, tmp_config_dir: Path) -> None:
        """Getting an existing project returns it."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
                project_name="Test",
            ),
        )

        project = store.get_project("test")
        assert project is not None
        assert project.project_name == "Test"

    def test_get_nonexistent_returns_none(self, tmp_config_dir: Path) -> None:
        """Getting a nonexistent project returns None."""
        store = ConfigStore(config_dir=tmp_config_dir)
        assert store.get_project("nonexistent") is None


class TestVersionCheck:
    """Tests for config version validation."""

    def test_version_1_loads_successfully(self, tmp_config_dir: Path) -> None:
        """Config with version 1 loads successfully."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text(json.dumps({"version": 1, "projects": {}}))

        config = store.load()
        assert config.version == 1

    def test_future_version_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Config with a future version raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": CURRENT_CONFIG_VERSION + 1,
                    "projects": {},
                }
            )
        )

        with pytest.raises(ConfigError, match="newer than supported"):
            store.load()

    def test_invalid_json_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Corrupted JSON in config file raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text("{invalid json!!")

        with pytest.raises(ConfigError, match="not valid JSON"):
            store.load()

    def test_invalid_structure_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Config file with wrong structure raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": {"bad": {"not_a_valid_field_only": True}},
                }
            )
        )

        with pytest.raises(ConfigError, match="invalid structure"):
            store.load()


class TestCorruptedFile:
    """Tests for handling corrupted config files."""

    def test_binary_garbage_in_config(self, tmp_config_dir: Path) -> None:
        """Loading a config file with binary garbage raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe\xfd")

        with pytest.raises(ConfigError, match="not valid UTF-8"):
            store.load()

    def test_truncated_json(self, tmp_config_dir: Path) -> None:
        """Loading a truncated JSON config raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text('{"version": 1, "projects": {')

        with pytest.raises(ConfigError, match="not valid JSON"):
            store.load()

    def test_json_array_instead_of_object(self, tmp_config_dir: Path) -> None:
        """Loading a JSON array instead of object raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text("[1, 2, 3]")

        with pytest.raises(ConfigError, match="invalid structure"):
            store.load()

    def test_json_null_value(self, tmp_config_dir: Path) -> None:
        """Loading JSON null raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text("null")

        with pytest.raises(ConfigError, match="invalid structure"):
            store.load()

    def test_empty_file(self, tmp_config_dir: Path) -> None:
        """Loading an empty file raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text("")

        with pytest.raises(ConfigError, match="not valid JSON"):
            store.load()

    def test_config_with_extra_fields_loads(self, tmp_config_dir: Path) -> None:
        """Config with extra fields (forward compatibility) loads successfully."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_project": "",
                    "projects": {},
                    "future_field": "some value",
                    "another_future": 42,
                }
            )
        )

        config = store.load()
        assert config.version == 1
        assert config.projects == {}


class TestMissingDirectory:
    """Tests for config operations when directory does not exist."""

    def test_load_from_nonexistent_directory(self, tmp_path: Path) -> None:
        """Loading from a nonexistent directory returns empty config."""
        nonexistent = tmp_path / "does" / "not" / "exist"
        store = ConfigStore(config_dir=nonexistent)
        config = store.load()

        assert isinstance(config, AppConfig)
        assert config.projects == {}

    def test_save_creates_nested_directory(self, tmp_path: Path) -> None:
        """Save creates deeply nested directory structure."""
        deep_dir = tmp_path / "a" / "b" / "c" / "d" / "config"
        store = ConfigStore(config_dir=deep_dir)
        store.save(AppConfig(default_project="test"))

        assert deep_dir.exists()
        loaded = store.load()
        assert loaded.default_project == "test"

    def test_add_project_creates_directory(self, tmp_path: Path) -> None:
        """add_project creates the config directory and file if needed."""
        new_dir = tmp_path / "fresh" / "config"
        store = ConfigStore(config_dir=new_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )

        assert (new_dir / "config.json").exists()
        config = store.load()
        assert "test" in config.projects


class TestPermissionDenied:
    """Tests for permission-related errors."""

    def test_save_to_readonly_directory(self, tmp_config_dir: Path) -> None:
        """Saving to a read-only directory raises ConfigError."""
        # Make the directory read-only
        tmp_config_dir.chmod(0o444)
        store = ConfigStore(config_dir=tmp_config_dir)

        try:
            with pytest.raises(ConfigError, match="Cannot write"):
                store.save(AppConfig())
        finally:
            # Restore permissions for cleanup
            tmp_config_dir.chmod(0o755)

    def test_load_unreadable_config_file(self, tmp_config_dir: Path) -> None:
        """Loading an unreadable config file raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_file = tmp_config_dir / "config.json"
        config_file.write_text(json.dumps({"version": 1, "projects": {}}))

        # Make the file unreadable
        config_file.chmod(0o000)

        try:
            with pytest.raises(ConfigError, match="Cannot read"):
                store.load()
        finally:
            # Restore permissions for cleanup
            config_file.chmod(0o644)


class TestDirectoryPermissions:
    """Tests for S3: config directory permissions."""

    def test_config_dir_permissions(self, tmp_path: Path) -> None:
        """Config directory is created with 0o700 permissions (owner only)."""
        nested_dir = tmp_path / "secure" / "config"
        store = ConfigStore(config_dir=nested_dir)
        store.save(AppConfig())

        dir_stat = os.stat(nested_dir)
        mode = stat.S_IMODE(dir_stat.st_mode)

        # Directory should be owner-only accessible (0o700)
        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"

    def test_config_dir_permissions_on_add_project(self, tmp_path: Path) -> None:
        """Config directory created via add_project also has 0o700 permissions."""
        new_dir = tmp_path / "fresh" / "secure_config"
        store = ConfigStore(config_dir=new_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abcdef-12345678",
            ),
        )

        dir_stat = os.stat(new_dir)
        mode = stat.S_IMODE(dir_stat.st_mode)

        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"


class TestConfigPath:
    """Tests for config_path property."""

    def test_config_path_returns_correct_path(self, tmp_config_dir: Path) -> None:
        """config_path property returns the full path to config.json."""
        store = ConfigStore(config_dir=tmp_config_dir)
        assert store.config_path == tmp_config_dir / "config.json"

    def test_multiple_save_load_cycles(self, tmp_config_dir: Path) -> None:
        """Multiple save/load cycles preserve data integrity."""
        store = ConfigStore(config_dir=tmp_config_dir)

        for i in range(10):
            store.add_project(
                f"project-{i}",
                ProjectConfig(
                    stack_url=f"https://stack-{i}.keboola.com",
                    token=f"901-token-{i}-abcdefgh",
                    project_name=f"Project {i}",
                    project_id=i,
                ),
            )

        config = store.load()
        assert len(config.projects) == 10
        assert config.projects["project-0"].project_name == "Project 0"
        assert config.projects["project-9"].project_id == 9
        assert config.default_project == "project-0"
