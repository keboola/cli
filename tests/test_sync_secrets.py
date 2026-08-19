"""Tests for sync secrets module -- encrypted value detection."""

import pytest

from keboola_agent_cli.sync.secrets import (
    ENCRYPTED_PREFIXES,
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
        """The AWS-form prefixes are detected."""
        assert is_encrypted_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            # Azure Key Vault (#607 / #612) -- these read as plaintext before 0.85.1.
            "KBC::ProjectSecureKV::abc123",
            "KBC::ComponentSecureKV::xyz",
            "KBC::ConfigSecureKV::secret",
            "KBC::ProjectWideSecureKV::wide",
            "KBC::SecureKV::generic",
            # Google KMS.
            "KBC::ProjectSecureGKMS::abc123",
            "KBC::ComponentSecureGKMS::xyz",
            "KBC::BranchTypeSecureGKMS::branch",
            # Branch-type scopes, AWS form.
            "KBC::BranchTypeSecure::branch",
            "KBC::BranchTypeConfigSecure::branch-config",
            "KBC::ProjectWideBranchTypeSecure::wide-branch",
        ],
    )
    def test_is_encrypted_value_true_per_cloud(self, value: str) -> None:
        """Every scope exists once per cloud; all three forms are markers."""
        assert is_encrypted_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "KBC::Encrypted==legacy",
            "KBC::ComponentEncrypted==legacy",
            "KBC::ComponentProjectEncrypted==legacy",
        ],
    )
    def test_is_encrypted_value_true_legacy(self, value: str) -> None:
        """Pre-2019 ciphers use ``==`` rather than ``::`` and still count."""
        assert is_encrypted_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "plain-text-value",
            "",
            "kbc::projectsecure::lowercase",
            "KBC::Unknown::something",
            "not-encrypted",
            # A cloud suffix that does not exist must not be waved through
            # just because it starts like a real one.
            "KBC::ProjectSecureKVX::nope",
            "KBC::ProjectSecureKMS::not-a-real-cipher",
        ],
    )
    def test_is_encrypted_value_false_strings(self, value: str) -> None:
        """Plain strings and wrong prefixes are not encrypted."""
        assert is_encrypted_value(value) is False

    def test_prefix_family_is_complete(self) -> None:
        """8 scopes x 3 clouds + 3 legacy ciphers, matching the platform registry.

        Source: keboola/keboola-operator
        ``internal/encryptor/wrapper/registry.go``. A scope added there must be
        added here too, or its ciphertext silently reads as plaintext.
        """
        assert len(ENCRYPTED_PREFIXES) == 27
        assert len(set(ENCRYPTED_PREFIXES)) == 27
        for scope in ("Secure", "ComponentSecure", "ProjectSecure", "ConfigSecure"):
            for suffix in ("", "KV", "GKMS"):
                assert f"KBC::{scope}{suffix}::" in ENCRYPTED_PREFIXES

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
