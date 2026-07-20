"""Tests for VariablesService -- high-level variables-as-attachment UX.

Covers the 3 service verbs (get, set, clear) and the three set sub-paths:
auto-create, merge, replace. Encryption + fail-closed + close() semantics are
inherited from sync_service's _encrypt_secrets_in_config (tested separately).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.variables_service import (
    VARIABLES_COMPONENT_ID,
    VariablesService,
)

SAMPLE_VERIFY = TokenVerifyResponse(
    token_id="tok-1",
    token_description="kbagent",
    project_id=258,
    project_name="Test",
    owner_name="Me",
)


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.verify_token.return_value = SAMPLE_VERIFY
    return client


def _service(tmp_config_dir: Path, client: MagicMock) -> VariablesService:
    store = setup_single_project(tmp_config_dir)
    return VariablesService(
        config_store=store,
        client_factory=lambda url, token: client,
    )


class TestGetVariables:
    def test_returns_linked_false_when_no_variables_id(self, tmp_config_dir: Path) -> None:
        """A parent config with no variables_id reports linked=False and empty values."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-1",
            "name": "my-transform",
            "configuration": {"parameters": {"some": "thing"}},
        }
        svc = _service(tmp_config_dir, client)

        result = svc.get_variables(alias="prod", component_id="keboola.x", config_id="cfg-1")

        assert result["linked"] is False
        assert result["values"] == {}
        assert result["variables_id"] is None
        assert result["values_id"] is None
        client.close.assert_called_once()

    def test_returns_values_from_default_row(self, tmp_config_dir: Path) -> None:
        """With variables_id but no values_id, resolves to first row (default convention)."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            {
                "id": "cfg-1",
                "configuration": {"variables_id": "vars-1"},
            },
            {
                "id": "vars-1",
                "configuration": {"variables": [{"name": "region", "type": "string"}]},
                "rows": [
                    {
                        "id": "row-1",
                        "configuration": {"values": [{"name": "region", "value": "eu"}]},
                    }
                ],
            },
        ]
        svc = _service(tmp_config_dir, client)

        result = svc.get_variables(alias="prod", component_id="keboola.x", config_id="cfg-1")

        assert result["linked"] is True
        assert result["values"] == {"region": "eu"}
        assert result["values_id"] == "row-1"

    def test_closes_client_on_api_error(self, tmp_config_dir: Path) -> None:
        """best_practices.md §3: client.close() is called even when the API raises."""
        client = _mock_client()
        client.get_config_detail.side_effect = KeboolaApiError(
            message="not found", status_code=404, error_code="NOT_FOUND"
        )
        svc = _service(tmp_config_dir, client)

        with pytest.raises(KeboolaApiError):
            svc.get_variables(alias="prod", component_id="keboola.x", config_id="cfg-1")
        client.close.assert_called_once()


class TestSetVariablesAutoCreate:
    def test_creates_backing_config_and_row_when_parent_not_linked(
        self, tmp_config_dir: Path
    ) -> None:
        """First call on an unlinked parent: create config + row + patch parent."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-1",
            "name": "my-transform",
            "configuration": {"parameters": {}},
        }
        client.create_config.return_value = {"id": "vars-auto-1"}
        client.create_config_row.return_value = {"id": "row-auto-1"}
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-1",
            variables={"year_start": "2016", "region": "eu"},
        )

        assert result["action"] == "created"
        assert result["variables_id"] == "vars-auto-1"
        assert result["values_id"] == "row-auto-1"
        assert result["values"] == {"year_start": "2016", "region": "eu"}

        # create_config was called with schema derived from var keys
        create_kwargs = client.create_config.call_args.kwargs
        assert create_kwargs["component_id"] == VARIABLES_COMPONENT_ID
        assert create_kwargs["name"] == "my-transform-vars"
        assert {v["name"] for v in create_kwargs["configuration"]["variables"]} == {
            "year_start",
            "region",
        }

        # Row was created with values payload
        row_kwargs = client.create_config_row.call_args.kwargs
        assert {v["name"] for v in row_kwargs["configuration"]["values"]} == {
            "year_start",
            "region",
        }

        # Parent config was patched to link variables_id + values_id
        parent_update_kwargs = client.update_config.call_args.kwargs
        assert parent_update_kwargs["component_id"] == "keboola.snowflake-transformation"
        assert parent_update_kwargs["configuration"]["variables_id"] == "vars-auto-1"
        assert parent_update_kwargs["configuration"]["variables_values_id"] == "row-auto-1"
        client.close.assert_called_once()

    def test_auto_create_with_empty_parent_name_falls_back_to_config_id(
        self, tmp_config_dir: Path
    ) -> None:
        """Parent without a name gets `<config_id>-vars` as the auto-created name."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-999",
            "name": "",
            "configuration": {},
        }
        client.create_config.return_value = {"id": "vars-auto"}
        client.create_config_row.return_value = {"id": "row-auto"}
        svc = _service(tmp_config_dir, client)

        svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-999",
            variables={"k": "v"},
        )

        assert client.create_config.call_args.kwargs["name"] == "cfg-999-vars"


class TestSetVariablesUpdate:
    def _linked_parent(self) -> dict:
        return {
            "id": "cfg-1",
            "name": "my-transform",
            "configuration": {
                "variables_id": "vars-existing",
                "variables_values_id": "row-existing",
                "parameters": {"irrelevant": True},
            },
        }

    def _linked_vars_cfg(
        self, values: list[dict] | None = None, schema: list[dict] | None = None
    ) -> dict:
        return {
            "id": "vars-existing",
            "configuration": {
                "variables": schema
                if schema is not None
                else [{"name": "region", "type": "string"}],
            },
            "rows": [
                {
                    "id": "row-existing",
                    "configuration": {
                        "values": values
                        if values is not None
                        else [{"name": "region", "value": "eu"}],
                    },
                }
            ],
        }

    def test_merge_keeps_existing_keys(self, tmp_config_dir: Path) -> None:
        """Default merge: new values overlay existing ones; other keys survive."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            self._linked_parent(),
            self._linked_vars_cfg(
                values=[
                    {"name": "region", "value": "eu"},
                    {"name": "year_start", "value": "2016"},
                ]
            ),
        ]
        client.update_config_row.return_value = {"id": "row-existing"}
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"region": "us-west"},  # only region changes
        )

        assert result["action"] == "updated"
        assert result["values"] == {"region": "us-west", "year_start": "2016"}

        put_kwargs = client.update_config_row.call_args.kwargs
        values_sent = {v["name"]: v["value"] for v in put_kwargs["configuration"]["values"]}
        assert values_sent == {"region": "us-west", "year_start": "2016"}

    def test_replace_overwrites_entire_values(self, tmp_config_dir: Path) -> None:
        """--replace drops existing keys not in the new set."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            self._linked_parent(),
            self._linked_vars_cfg(
                values=[
                    {"name": "region", "value": "eu"},
                    {"name": "year_start", "value": "2016"},
                ]
            ),
        ]
        client.update_config_row.return_value = {"id": "row-existing"}
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"region": "us-west"},
            replace=True,
        )

        assert result["values"] == {"region": "us-west"}

    def test_new_key_extends_schema(self, tmp_config_dir: Path) -> None:
        """A brand-new key appears in the schema after set (cosmetic sync)."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            self._linked_parent(),
            self._linked_vars_cfg(
                schema=[{"name": "region", "type": "string"}],
                values=[{"name": "region", "value": "eu"}],
            ),
        ]
        client.update_config_row.return_value = {"id": "row-existing"}
        svc = _service(tmp_config_dir, client)

        svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"year_start": "2016"},  # new key
        )

        # The schema-extension call to update_config should have been made on
        # the variables config itself (distinct from the parent update).
        # Two update_config calls may fire: schema + parent. The variables one
        # carries a `variables` key in its configuration.
        schema_calls = [
            c
            for c in client.update_config.call_args_list
            if c.kwargs.get("component_id") == VARIABLES_COMPONENT_ID
        ]
        assert len(schema_calls) == 1
        schema_names = {v["name"] for v in schema_calls[0].kwargs["configuration"]["variables"]}
        assert schema_names == {"region", "year_start"}

    def test_schema_sync_failure_is_logged_not_raised(self, tmp_config_dir: Path) -> None:
        """If schema update fails, the row update already succeeded -- don't bubble up."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            self._linked_parent(),
            self._linked_vars_cfg(),
        ]
        client.update_config_row.return_value = {"id": "row-existing"}

        def update_config_side_effect(**kwargs):
            if kwargs.get("component_id") == VARIABLES_COMPONENT_ID:
                raise KeboolaApiError(
                    message="schema update failed",
                    status_code=500,
                    error_code="INTERNAL",
                )
            return {"id": kwargs["config_id"]}

        client.update_config.side_effect = update_config_side_effect
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"new_key": "x"},
        )
        # The operation succeeds from the caller's view even though the
        # cosmetic schema update failed.
        assert result["action"] == "updated"
        assert "new_key" in result["values"]


class TestSetVariablesEncryption:
    def test_hash_prefixed_keys_are_encrypted_before_row_put(self, tmp_config_dir: Path) -> None:
        """#-prefixed keys go through encrypt_values before the PUT body is built."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            {
                "id": "cfg-1",
                "name": "my-transform",
                "configuration": {
                    "variables_id": "vars-1",
                    "variables_values_id": "row-1",
                },
            },
            {
                "id": "vars-1",
                "configuration": {"variables": []},
                "rows": [{"id": "row-1", "configuration": {"values": []}}],
            },
        ]
        client.update_config_row.return_value = {"id": "row-1"}
        client.encrypt_values.side_effect = lambda *, project_id, component_id, data: {
            k: "KBC::ComponentSecure::cipher" for k in data
        }
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"#api_token": "plain-secret"},
        )

        assert result["encrypted_keys"] == ["#api_token"]
        put_kwargs = client.update_config_row.call_args.kwargs
        values_sent = {v["name"]: v["value"] for v in put_kwargs["configuration"]["values"]}
        assert values_sent["#api_token"] == "KBC::ComponentSecure::cipher"

    def test_encryption_failure_fail_closed(self, tmp_config_dir: Path) -> None:
        """Encryption failure raises ENCRYPTION_FAILED (no plaintext PUT)."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            {
                "id": "cfg-1",
                "name": "t",
                "configuration": {
                    "variables_id": "vars-1",
                    "variables_values_id": "row-1",
                },
            },
            {
                "id": "vars-1",
                "configuration": {"variables": []},
                "rows": [{"id": "row-1", "configuration": {"values": []}}],
            },
        ]
        client.encrypt_values.side_effect = Exception("encryption unavailable")
        svc = _service(tmp_config_dir, client)

        with pytest.raises(KeboolaApiError) as excinfo:
            svc.set_variables(
                alias="prod",
                component_id="keboola.x",
                config_id="cfg-1",
                variables={"#api_token": "plain"},
            )
        assert excinfo.value.error_code == "ENCRYPTION_FAILED"
        client.update_config_row.assert_not_called()
        # best_practices.md §5: try/finally close() must still fire on error.
        client.close.assert_called_once()

    def test_plaintext_written_empty_on_successful_encryption(self, tmp_config_dir: Path) -> None:
        """A successful encryption leaves plaintext_written empty."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            {
                "id": "cfg-1",
                "name": "my-transform",
                "configuration": {
                    "variables_id": "vars-1",
                    "variables_values_id": "row-1",
                },
            },
            {
                "id": "vars-1",
                "configuration": {"variables": []},
                "rows": [{"id": "row-1", "configuration": {"values": []}}],
            },
        ]
        client.update_config_row.return_value = {"id": "row-1"}
        client.encrypt_values.side_effect = lambda *, project_id, component_id, data: {
            k: "KBC::ComponentSecure::cipher" for k in data
        }
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"#api_token": "plain-secret"},
        )

        assert result["plaintext_written"] == []

    def test_plaintext_written_lists_leaked_keys_on_fallback(self, tmp_config_dir: Path) -> None:
        """An allowed plaintext fallback surfaces the leaked key-PATH, never the value."""
        client = _mock_client()
        client.get_config_detail.side_effect = [
            {
                "id": "cfg-1",
                "name": "my-transform",
                "configuration": {
                    "variables_id": "vars-1",
                    "variables_values_id": "row-1",
                },
            },
            {
                "id": "vars-1",
                "configuration": {"variables": []},
                "rows": [{"id": "row-1", "configuration": {"values": []}}],
            },
        ]
        client.update_config_row.return_value = {"id": "row-1"}
        client.encrypt_values.side_effect = Exception("encryption unavailable")
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.x",
            config_id="cfg-1",
            variables={"#api_token": "plain-secret"},
            allow_plaintext_fallback=True,
        )

        # The row-hoisted secret flattens to this path; the plaintext value
        # must never appear in the structured field.
        assert result["plaintext_written"] == ["#values.[0].#api_token"]
        assert "plain-secret" not in result["plaintext_written"]
        # The escape hatch wrote the row with plaintext intact.
        put_kwargs = client.update_config_row.call_args.kwargs
        values_sent = {v["name"]: v["value"] for v in put_kwargs["configuration"]["values"]}
        assert values_sent["#api_token"] == "plain-secret"

    def test_plaintext_written_lists_leaked_keys_on_fallback_auto_create(
        self, tmp_config_dir: Path
    ) -> None:
        """Auto-create path also threads the leaked key-PATH out on fallback."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-1",
            "name": "my-transform",
            "configuration": {"parameters": {}},
        }
        client.create_config.return_value = {"id": "vars-auto-1"}
        client.create_config_row.return_value = {"id": "row-auto-1"}
        client.encrypt_values.side_effect = Exception("encryption unavailable")
        svc = _service(tmp_config_dir, client)

        result = svc.set_variables(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-1",
            variables={"#api_token": "plain-secret"},
            allow_plaintext_fallback=True,
        )

        assert result["action"] == "created"
        assert result["plaintext_written"] == ["#values.[0].#api_token"]
        assert "plain-secret" not in result["plaintext_written"]


class TestSetVariablesValidation:
    def test_empty_variables_dict_raises(self, tmp_config_dir: Path) -> None:
        """set_variables requires at least one var (CLI layer validates first, service is a belt)."""
        client = _mock_client()
        svc = _service(tmp_config_dir, client)

        with pytest.raises(ConfigError, match="at least one variable"):
            svc.set_variables(
                alias="prod",
                component_id="keboola.x",
                config_id="cfg-1",
                variables={},
            )


class TestClearVariables:
    def test_strips_link_from_parent_and_keeps_backing_config(self, tmp_config_dir: Path) -> None:
        """Clear removes both fields from parent configuration, leaves variables config alive."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-1",
            "configuration": {
                "variables_id": "vars-1",
                "variables_values_id": "row-1",
                "parameters": {"unrelated": "value"},
            },
        }
        svc = _service(tmp_config_dir, client)

        result = svc.clear_variables(alias="prod", component_id="keboola.x", config_id="cfg-1")

        assert result["was_linked"] is True
        assert result["unlinked_variables_id"] == "vars-1"
        assert result["unlinked_values_id"] == "row-1"

        put_kwargs = client.update_config.call_args.kwargs
        assert "variables_id" not in put_kwargs["configuration"]
        assert "variables_values_id" not in put_kwargs["configuration"]
        assert put_kwargs["configuration"]["parameters"] == {"unrelated": "value"}

        # The backing keboola.variables config MUST NOT be deleted.
        client.delete_config.assert_not_called()

    def test_no_op_when_parent_not_linked(self, tmp_config_dir: Path) -> None:
        """Clearing an already-unlinked config is a no-op (no update_config call)."""
        client = _mock_client()
        client.get_config_detail.return_value = {
            "id": "cfg-1",
            "configuration": {"parameters": {}},
        }
        svc = _service(tmp_config_dir, client)

        result = svc.clear_variables(alias="prod", component_id="keboola.x", config_id="cfg-1")

        assert result["was_linked"] is False
        client.update_config.assert_not_called()
