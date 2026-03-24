"""Tests for sync secrets module -- encrypted value detection."""

import pytest

from keboola_agent_cli.sync.secrets import (
    find_encrypted_paths,
    is_encrypted_value,
    is_secret_key,
)


class TestIsEncryptedValue:
    """Tests for is_encrypted_value()."""

    @pytest.mark.parametrize(
        "value",
        [
            "KBC::ProjectSecure::abc123",
            "KBC::ComponentSecure::xyz",
            "KBC::ConfigSecure::secret",
            "KBC::ProjectWideSecure::wide",
        ],
    )
    def test_is_encrypted_value_true(self, value: str) -> None:
        """All four encryption prefixes are detected."""
        assert is_encrypted_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "plain-text-value",
            "",
            "kbc::projectsecure::lowercase",
            "KBC::Unknown::something",
            "not-encrypted",
        ],
    )
    def test_is_encrypted_value_false_strings(self, value: str) -> None:
        """Plain strings and wrong prefixes are not encrypted."""
        assert is_encrypted_value(value) is False

    @pytest.mark.parametrize("value", [42, None, True, 3.14, [], {}])
    def test_is_encrypted_value_false_non_strings(self, value: object) -> None:
        """Non-string types are never encrypted values."""
        assert is_encrypted_value(value) is False


class TestIsSecretKey:
    """Tests for is_secret_key()."""

    @pytest.mark.parametrize("key", ["#password", "#token", "#api_key", "#"])
    def test_is_secret_key_true(self, key: str) -> None:
        """Keys starting with '#' are secret keys."""
        assert is_secret_key(key) is True

    @pytest.mark.parametrize("key", ["password", "token", "", "api_key", "hash#tag"])
    def test_is_secret_key_false(self, key: str) -> None:
        """Keys not starting with '#' are not secret keys."""
        assert is_secret_key(key) is False


class TestFindEncryptedPaths:
    """Tests for find_encrypted_paths()."""

    def test_find_encrypted_paths_flat(self) -> None:
        """Flat dict with encrypted values returns correct paths."""
        data = {
            "#token": "KBC::ProjectSecure::abc",
            "name": "plain",
            "#password": "KBC::ConfigSecure::xyz",
        }
        paths = find_encrypted_paths(data)

        assert "#token" in paths
        assert "#password" in paths
        assert "name" not in paths

    def test_find_encrypted_paths_nested(self) -> None:
        """Nested dicts and lists with encrypted values are found."""
        data = {
            "parameters": {
                "#token": "KBC::ProjectSecure::abc",
                "nested": {
                    "#secret": "KBC::ComponentSecure::def",
                },
            },
            "plain": "not-secret",
        }
        paths = find_encrypted_paths(data)

        assert "parameters.#token" in paths
        assert "parameters.nested.#secret" in paths
        assert len(paths) == 2

    def test_find_encrypted_paths_empty(self) -> None:
        """Empty dict returns empty list."""
        assert find_encrypted_paths({}) == []

    def test_find_encrypted_paths_list_with_dicts(self) -> None:
        """Lists containing dicts with encrypted values are discovered."""
        data = {
            "rows": [
                {"#key": "KBC::ProjectSecure::first"},
                {"plain": "ok"},
                {"#key": "KBC::ProjectSecure::third"},
            ],
        }
        paths = find_encrypted_paths(data)

        assert "rows[0].#key" in paths
        assert "rows[2].#key" in paths
        assert len(paths) == 2

    def test_find_encrypted_paths_encrypted_value_on_regular_key(self) -> None:
        """An encrypted value on a regular (non-#) key is also detected."""
        data = {
            "api_token": "KBC::ProjectSecure::hidden",
        }
        paths = find_encrypted_paths(data)
        assert "api_token" in paths
