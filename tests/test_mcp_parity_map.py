"""Offline invariants for the MCP tool -> CLI parity map (epic #390 phase 2)."""

from keboola_agent_cli.mcp_parity import (
    MCP_TOOL_PARITY,
    deprecation_message,
    native_equivalent,
    parity_operation_key,
)
from keboola_agent_cli.permissions import OPERATION_REGISTRY


class TestParityMapInvariants:
    def test_every_entry_resolves_to_a_registered_operation(self) -> None:
        """The canonical command of every entry must be a real CLI operation.

        This is the anti-drift lock: renaming or removing a native command
        without updating the parity map fails here, offline, in regular CI.
        """
        missing = {
            tool: parity_operation_key(entry)
            for tool, entry in MCP_TOOL_PARITY.items()
            if parity_operation_key(entry) not in OPERATION_REGISTRY
        }
        assert not missing, f"parity entries pointing at unknown operations: {missing}"

    def test_known_catalog_tools_are_mapped(self) -> None:
        """Spot-check the six freshly ported tools plus the two intentional
        non-ports -- the canary (scripts/check_mcp_parity.py) covers the full
        live catalog online."""
        for tool in (
            "docs_query",
            "get_config_examples",
            "get_semantic_schema",
            "run_sync_action",
            "create_sql_transformation",
            "update_sql_transformation",
            "get_flow_examples",
            "query_data",
            "validate_semantic_query",
        ):
            assert native_equivalent(tool) is not None, tool

    def test_unmapped_tool_message_points_at_the_epic(self) -> None:
        msg = deprecation_message("frobnicate_project")
        assert "no native equivalent" in msg
        assert "issues/390" in msg

    def test_mapped_tool_message_names_the_command(self) -> None:
        msg = deprecation_message("run_job")
        assert "kbagent job run" in msg
        assert "deprecated" in msg

    def test_notes_do_not_leak_into_operation_keys(self) -> None:
        for tool, entry in MCP_TOOL_PARITY.items():
            key = parity_operation_key(entry)
            assert " " not in key, (tool, key)
            assert "`" not in key, (tool, key)
