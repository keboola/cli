"""Tests for the sync diff engine module.

Tests normalize_for_comparison, config_hash, deep_diff, and compute_changeset
functions from keboola_agent_cli.sync.diff_engine.
"""

from __future__ import annotations

import copy
from typing import Any

from keboola_agent_cli.constants import DIFF_MAX_LINES, ENCRYPTED_PLACEHOLDER
from keboola_agent_cli.sync.diff_engine import (
    ConfigChange,
    compute_changeset,
    config_hash,
    deep_diff,
    normalize_for_comparison,
)

# ===================================================================
# TestNormalizeForComparison
# ===================================================================


class TestNormalizeForComparison:
    """Tests for normalize_for_comparison()."""

    def test_strips_keboola_key(self) -> None:
        """_keboola metadata block is removed from output."""
        data = {
            "name": "My Config",
            "_keboola": {"component_id": "keboola.ex-http", "config_id": "123"},
            "parameters": {"url": "https://example.com"},
        }
        result = normalize_for_comparison(data)
        assert "_keboola" not in result
        assert "name" in result
        assert "parameters" in result

    def test_strips_version_key(self) -> None:
        """version key is removed from output."""
        data = {
            "version": 2,
            "name": "My Config",
            "parameters": {"key": "value"},
        }
        result = normalize_for_comparison(data)
        assert "version" not in result
        assert "name" in result

    def test_strips_configuration_extra(self) -> None:
        """_configuration_extra key is removed from output."""
        data = {
            "name": "My Config",
            "_configuration_extra": {"runtime": {"backend": "snowflake"}},
            "parameters": {"sql": "SELECT 1"},
        }
        result = normalize_for_comparison(data)
        assert "_configuration_extra" not in result
        assert "name" in result

    def test_replaces_encrypted_values(self) -> None:
        """KBC::ProjectSecure::abc is replaced with ENCRYPTED_PLACEHOLDER."""
        data = {
            "parameters": {
                "#token": "KBC::ProjectSecure::abc123nonce456",
                "url": "https://api.example.com",
            }
        }
        result = normalize_for_comparison(data)
        assert result["parameters"]["#token"] == ENCRYPTED_PLACEHOLDER
        assert result["parameters"]["url"] == "https://api.example.com"

    def test_does_not_mutate_original(self) -> None:
        """Original dict is unchanged after normalization."""
        data = {
            "version": 2,
            "_keboola": {"component_id": "test"},
            "parameters": {
                "#secret": "KBC::ProjectSecure::xyz",
                "url": "https://example.com",
            },
        }
        original = copy.deepcopy(data)
        normalize_for_comparison(data)

        assert data == original
        assert data["version"] == 2
        assert "_keboola" in data
        assert data["parameters"]["#secret"] == "KBC::ProjectSecure::xyz"

    def test_nested_encryption(self) -> None:
        """Encrypted values deep in nested dicts/lists are replaced."""
        data = {
            "parameters": {
                "connections": [
                    {
                        "host": "db.example.com",
                        "#password": "KBC::ComponentSecure::pass123",
                        "nested": {
                            "#api_key": "KBC::ConfigSecure::key456",
                        },
                    }
                ]
            }
        }
        result = normalize_for_comparison(data)

        connection = result["parameters"]["connections"][0]
        assert connection["#password"] == ENCRYPTED_PLACEHOLDER
        assert connection["nested"]["#api_key"] == ENCRYPTED_PLACEHOLDER
        assert connection["host"] == "db.example.com"


# ===================================================================
# TestConfigHash
# ===================================================================


class TestConfigHash:
    """Tests for config_hash()."""

    def test_same_content_same_hash(self) -> None:
        """Identical configs produce the same hash."""
        config = {
            "name": "My Config",
            "parameters": {"url": "https://api.example.com"},
        }
        h1 = config_hash(config)
        h2 = config_hash(config)
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        """Changed parameters produce a different hash."""
        config1 = {
            "name": "My Config",
            "parameters": {"url": "https://api.example.com"},
        }
        config2 = {
            "name": "My Config",
            "parameters": {"url": "https://api.changed.com"},
        }
        assert config_hash(config1) != config_hash(config2)

    def test_encryption_nonces_ignored(self) -> None:
        """Two configs with different encrypted markers for same key produce SAME hash."""
        config1 = {
            "parameters": {
                "#token": "KBC::ProjectSecure::nonce_aaa_111",
                "url": "https://example.com",
            }
        }
        config2 = {
            "parameters": {
                "#token": "KBC::ProjectSecure::nonce_bbb_222",
                "url": "https://example.com",
            }
        }
        assert config_hash(config1) == config_hash(config2)

    def test_ordering_independent(self) -> None:
        """Dict key order does not affect the hash."""
        config1 = {
            "name": "Config",
            "parameters": {"a": 1, "b": 2},
            "description": "test",
        }
        config2 = {
            "description": "test",
            "parameters": {"b": 2, "a": 1},
            "name": "Config",
        }
        assert config_hash(config1) == config_hash(config2)


# ===================================================================
# TestDeepDiff
# ===================================================================


class TestDeepDiff:
    """Tests for deep_diff()."""

    def test_changed_scalar(self) -> None:
        """Parameter value changed shows old -> new."""
        local = {"parameters": {"url": "https://new.example.com"}}
        remote = {"parameters": {"url": "https://old.example.com"}}

        result = deep_diff(local, remote)

        assert len(result) == 1
        assert "parameters.url changed:" in result[0]
        assert "'https://old.example.com'" in result[0]
        assert "'https://new.example.com'" in result[0]

    def test_added_key(self) -> None:
        """Key in local but not remote shows as added."""
        local = {"parameters": {"url": "https://example.com", "timeout": 30}}
        remote = {"parameters": {"url": "https://example.com"}}

        result = deep_diff(local, remote)

        assert len(result) == 1
        assert "parameters.timeout added" in result[0]

    def test_removed_key(self) -> None:
        """Key in remote but not local shows as removed."""
        local = {"parameters": {"url": "https://example.com"}}
        remote = {"parameters": {"url": "https://example.com", "timeout": 30}}

        result = deep_diff(local, remote)

        assert len(result) == 1
        assert "parameters.timeout removed" in result[0]

    def test_encrypted_value_masked(self) -> None:
        """Shows 'changed (encrypted)' not actual values."""
        local = {"parameters": {"#token": "KBC::ProjectSecure::new_nonce"}}
        remote = {"parameters": {"#token": "KBC::ProjectSecure::old_nonce"}}

        # Both values normalize to ENCRYPTED_PLACEHOLDER, so they are equal
        # and should produce no diff.
        result = deep_diff(local, remote)
        assert result == []

    def test_encrypted_vs_plaintext_masked(self) -> None:
        """When one side is encrypted placeholder, shows 'changed (encrypted)'."""
        local = {
            "parameters": {
                "#token": "KBC::ProjectSecure::new_nonce",
                "url": "https://example.com",
            }
        }
        remote = {
            "parameters": {
                "url": "https://example.com",
            }
        }

        result = deep_diff(local, remote)

        assert len(result) == 1
        assert "parameters.#token added" in result[0]

    def test_list_length_changed(self) -> None:
        """Lists of different lengths produce a length-change message."""
        local = {"items": [1, 2, 3]}
        remote = {"items": [1, 2]}

        result = deep_diff(local, remote)

        assert len(result) == 1
        assert "items list length changed" in result[0]
        assert "2 -> 3" in result[0]

    def test_empty_diff_for_identical(self) -> None:
        """Identical configs produce empty list."""
        config = {
            "name": "Test",
            "parameters": {"url": "https://example.com"},
        }
        result = deep_diff(config, config)
        assert result == []

    def test_max_lines_respected(self) -> None:
        """Diff output is capped at DIFF_MAX_LINES."""
        # Create configs with many differences (more than DIFF_MAX_LINES)
        local: dict[str, Any] = {"parameters": {}}
        remote: dict[str, Any] = {"parameters": {}}
        for i in range(DIFF_MAX_LINES + 10):
            local["parameters"][f"key_{i}"] = f"new_value_{i}"
            remote["parameters"][f"key_{i}"] = f"old_value_{i}"

        result = deep_diff(local, remote)

        assert len(result) <= DIFF_MAX_LINES


# ===================================================================
# TestComputeChangeset
# ===================================================================


class TestComputeChangeset:
    """Tests for compute_changeset()."""

    def test_added_config(self) -> None:
        """Local config with no remote match produces 'added' change."""
        local_configs = [
            {
                "component_id": "keboola.ex-http",
                "config_id": "",  # no ID = new config
                "config_name": "New Extractor",
                "path": "extractor/keboola.ex-http/new-extractor",
                "data": {"name": "New Extractor", "parameters": {"url": "https://example.com"}},
            }
        ]
        remote_configs: dict[str, dict[str, Any]] = {}

        changes = compute_changeset(local_configs, remote_configs)

        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert changes[0].component_id == "keboola.ex-http"
        assert changes[0].config_name == "New Extractor"

    def test_modified_config(self) -> None:
        """Local differs from remote produces 'modified' with details."""
        local_configs = [
            {
                "component_id": "keboola.ex-http",
                "config_id": "cfg-001",
                "config_name": "My Extractor",
                "path": "extractor/keboola.ex-http/my-extractor",
                "data": {
                    "name": "My Extractor",
                    "parameters": {"url": "https://new.example.com"},
                },
            }
        ]
        remote_configs = {
            "keboola.ex-http/cfg-001": {
                "name": "My Extractor",
                "parameters": {"url": "https://old.example.com"},
            },
        }

        changes = compute_changeset(local_configs, remote_configs)

        assert len(changes) == 1
        assert changes[0].change_type == "modified"
        assert changes[0].config_id == "cfg-001"
        assert len(changes[0].details) > 0
        # Details should mention the URL change
        detail_text = " ".join(changes[0].details)
        assert "parameters.url" in detail_text

    def test_deleted_config(self) -> None:
        """Remote config not in local list produces 'deleted'."""
        local_configs: list[dict[str, Any]] = []
        remote_configs = {
            "keboola.ex-http/cfg-001": {
                "name": "Old Extractor",
                "parameters": {"url": "https://example.com"},
            },
        }

        changes = compute_changeset(local_configs, remote_configs)

        assert len(changes) == 1
        assert changes[0].change_type == "deleted"
        assert changes[0].component_id == "keboola.ex-http"
        assert changes[0].config_id == "cfg-001"
        assert changes[0].config_name == "Old Extractor"

    def test_unchanged_config(self) -> None:
        """Identical local and remote produces no changeset entry."""
        config_data = {
            "name": "My Extractor",
            "parameters": {"url": "https://example.com"},
        }
        local_configs = [
            {
                "component_id": "keboola.ex-http",
                "config_id": "cfg-001",
                "config_name": "My Extractor",
                "path": "extractor/keboola.ex-http/my-extractor",
                "data": config_data,
            }
        ]
        remote_configs = {
            "keboola.ex-http/cfg-001": config_data,
        }

        changes = compute_changeset(local_configs, remote_configs)

        assert len(changes) == 0

    def test_mixed_changeset(self) -> None:
        """Combination of added, modified, deleted, and unchanged."""
        local_configs = [
            # Unchanged config
            {
                "component_id": "keboola.ex-http",
                "config_id": "cfg-001",
                "config_name": "Unchanged",
                "path": "extractor/keboola.ex-http/unchanged",
                "data": {"name": "Unchanged", "parameters": {"url": "https://example.com"}},
            },
            # Modified config
            {
                "component_id": "keboola.ex-db",
                "config_id": "cfg-002",
                "config_name": "Modified",
                "path": "extractor/keboola.ex-db/modified",
                "data": {"name": "Modified", "parameters": {"host": "new-host.com"}},
            },
            # Added config (no ID)
            {
                "component_id": "keboola.wr-snowflake",
                "config_id": "",
                "config_name": "New Writer",
                "path": "writer/keboola.wr-snowflake/new-writer",
                "data": {"name": "New Writer", "parameters": {"table": "output"}},
            },
        ]
        remote_configs = {
            # Unchanged - same data
            "keboola.ex-http/cfg-001": {
                "name": "Unchanged",
                "parameters": {"url": "https://example.com"},
            },
            # Modified - different data
            "keboola.ex-db/cfg-002": {
                "name": "Modified",
                "parameters": {"host": "old-host.com"},
            },
            # Deleted - not in local
            "keboola.snowflake-transformation/cfg-003": {
                "name": "Deleted Transform",
                "parameters": {},
            },
        }

        changes = compute_changeset(local_configs, remote_configs)

        change_types = {c.change_type for c in changes}
        assert "added" in change_types
        assert "modified" in change_types
        assert "deleted" in change_types

        added = [c for c in changes if c.change_type == "added"]
        modified = [c for c in changes if c.change_type == "modified"]
        deleted = [c for c in changes if c.change_type == "deleted"]

        assert len(added) == 1
        assert added[0].component_id == "keboola.wr-snowflake"

        assert len(modified) == 1
        assert modified[0].config_id == "cfg-002"

        assert len(deleted) == 1
        assert deleted[0].config_id == "cfg-003"
        assert deleted[0].config_name == "Deleted Transform"


# ===================================================================
# TestConfigChange
# ===================================================================


class TestConfigChange:
    """Tests for ConfigChange.to_dict() serialization."""

    def test_to_dict_serialization(self) -> None:
        """to_dict() includes all expected fields."""
        change = ConfigChange(
            change_type="modified",
            component_id="keboola.ex-http",
            config_id="cfg-001",
            config_name="Test Config",
            path="extractor/keboola.ex-http/test-config",
            details=["parameters.url changed: 'old' -> 'new'"],
        )
        d = change.to_dict()

        assert d["change_type"] == "modified"
        assert d["component_id"] == "keboola.ex-http"
        assert d["config_id"] == "cfg-001"
        assert d["config_name"] == "Test Config"
        assert d["path"] == "extractor/keboola.ex-http/test-config"
        assert len(d["details"]) == 1
