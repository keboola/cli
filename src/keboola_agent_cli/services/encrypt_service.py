"""Encryption service -- encrypt secret values via Keboola Encryption API."""

import logging
from typing import Any

from ..errors import ConfigError
from .base import BaseService

logger = logging.getLogger(__name__)


class EncryptService(BaseService):
    """Encrypt #-prefixed secret values using Keboola Encryption API.

    The Keboola Encryption API is one-way -- there is no decrypt endpoint.
    Encrypted values (KBC:: prefix) can only be used by Keboola components.
    """

    def encrypt(
        self,
        alias: str,
        component_id: str,
        input_data: dict[str, Any],
    ) -> dict[str, str]:
        """Encrypt secret values for a given project and component.

        Args:
            alias: Project alias.
            component_id: Keboola component ID (e.g. 'keboola.ex-db-snowflake').
            input_data: Dict with #-prefixed keys and plaintext string values.

        Returns:
            Dict with same keys, values replaced by ciphertext.

        Raises:
            ConfigError: If input validation fails (missing # prefix, non-string values).
            KeboolaApiError: If the encryption API call fails.
        """
        # Validate input
        errors: list[str] = []
        for key, value in input_data.items():
            if not key.startswith("#"):
                errors.append(f"Key '{key}' must start with '#'")
            if not isinstance(value, str):
                errors.append(f"Value for '{key}' must be a string, got {type(value).__name__}")
        if errors:
            raise ConfigError("; ".join(errors))

        # Filter out already-encrypted values (KBC:: prefix) -- pass through unchanged
        to_encrypt: dict[str, str] = {}
        already_encrypted: dict[str, str] = {}
        for key, value in input_data.items():
            if isinstance(value, str) and value.startswith("KBC::"):
                already_encrypted[key] = value
            else:
                to_encrypt[key] = value

        if not to_encrypt:
            return already_encrypted

        # Resolve project and call API
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            project_id = project.project_id
            if not project_id:
                token_info = client.verify_token()
                project_id = token_info.project_id

            if not project_id:
                raise ConfigError(
                    f"Cannot determine project ID for '{alias}'. "
                    "Try running 'kbagent project status' to refresh project info."
                )

            encrypted = client.encrypt_values(
                project_id=project_id,
                component_id=component_id,
                data=to_encrypt,
            )
            # Merge back already-encrypted values
            encrypted.update(already_encrypted)
            return encrypted
        finally:
            client.close()
