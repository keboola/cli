"""MCP tool -> native CLI command parity map (epic #390, issue #478 phase 2).

The MCP passthrough (``tool call`` / ``tool list`` / ``agent --type
mcp_tool``) is deprecated in favor of native commands. This module is the
single source of truth for the mapping, consumed by:

- the deprecation warnings printed by ``tool call`` / ``tool list``
  (each names the native equivalent for the exact tool being used),
- ``scripts/check_mcp_parity.py`` -- the CI canary that diffs the live
  keboola-mcp-server catalog (``TOOLS.md``) against this map, so a new
  server tool opens a red run instead of silently widening the gap,
- ``tests/test_mcp_parity_map.py`` -- offline invariants (every mapped
  command resolves to a registered CLI operation).

Keep entries in sync with the CATALOG the canary fetches; an unmapped tool
is a parity BUG by definition (that is the point of the canary).
"""

from __future__ import annotations

from dataclasses import dataclass

# Removal target for the MCP passthrough (epic #390 phase 3). Deprecated since
# 0.74.0; until 0.80.0 the notice said only "a future release", which gives a
# user nothing to plan against -- especially for `agent --type mcp_tool`, whose
# tasks live in `<config_dir>/agents.json` on disk and would simply start
# failing on their next cron tick. Every surface that mentions the deprecation
# quotes these two so they cannot drift apart.
#
# Deliberately NOT in constants.py, which is otherwise the home for values like
# these: this module must stay stdlib-only and standalone-importable so
# `scripts/check_mcp_parity.py` can load it on a bare python3 in the canary
# workflow, and constants.py imports httpx. Moving them there turns the canary
# red -- verified.
MCP_REMOVAL_VERSION: str = "0.85.0"
MCP_REMOVAL_TARGET_DATE: str = "end of August 2026"

# Warning for `agent --type mcp_tool`. Lives here rather than in commands/ so
# BOTH front doors can reach it without the server importing the command layer:
# `kbagent agent list` and the `kbagent serve` /agents route must flag the same
# tasks, and the Web UI population is the LEAST likely to ever run
# `kbagent doctor`.
MCP_TOOL_ACTION_DEPRECATION = (
    "agent action type 'mcp_tool' is deprecated (epic #390); prefer --type "
    "cli_command with the native kbagent command (see `kbagent tool list` "
    f"cli_equivalent column). It is REMOVED in kbagent v{MCP_REMOVAL_VERSION} "
    f"({MCP_REMOVAL_TARGET_DATE}) -- migrate scheduled tasks before then or they "
    "will start failing on their next run."
)


def annotate_mcp_tool_deprecation(task: dict) -> dict:
    """Add an additive ``deprecation`` key to a task using the MCP passthrough.

    Additive and only on affected tasks, so every existing consumer sees a
    byte-identical payload -- the same contract ``tool list`` / ``tool call``
    already use. Mutates and returns the same dict.
    """
    if (task.get("action") or {}).get("type") == "mcp_tool":
        task["deprecation"] = MCP_TOOL_ACTION_DEPRECATION
    return task


@dataclass(frozen=True)
class ParityEntry:
    """Native-CLI equivalent of one MCP tool.

    ``command`` is the canonical replacement in CLI notation (also used to
    derive the ``group.subcommand`` operation key for registry checks).
    ``note`` carries display-only context (alternatives, caveats).
    """

    command: str
    note: str = ""


# Every tool in the keboola-mcp-server catalog -> its native replacement.
# Display strings; the FIRST TWO tokens of ``command`` must form a valid
# OPERATION_REGISTRY key (enforced by tests/test_mcp_parity_map.py).
MCP_TOOL_PARITY: dict[str, ParityEntry] = {
    # -- Component tools ---------------------------------------------------
    "add_config_row": ParityEntry("config row-create"),
    "create_config": ParityEntry("config new", note="use --push for one-shot remote create"),
    "create_sql_transformation": ParityEntry("transformation create"),
    "get_components": ParityEntry("component list", note="or `component detail`"),
    "get_config_examples": ParityEntry("config examples"),
    "get_configs": ParityEntry("config list", note="or `config detail`"),
    "run_sync_action": ParityEntry("component sync-action"),
    "update_config": ParityEntry("config update"),
    "update_config_row": ParityEntry("config row-update"),
    "update_sql_transformation": ParityEntry("transformation edit"),
    # -- Documentation -----------------------------------------------------
    "docs_query": ParityEntry("docs query"),
    # -- Flows ---------------------------------------------------------------
    "create_conditional_flow": ParityEntry("flow new"),
    "create_flow": ParityEntry(
        "flow new",
        note="legacy keboola.orchestrator flows were dropped in 0.57.0; "
        "kbagent creates conditional flows (keboola.flow) only",
    ),
    "get_flow_examples": ParityEntry("flow examples"),
    "get_flow_schema": ParityEntry("flow schema", note="--full for the JSON Schema"),
    "get_flows": ParityEntry("flow list", note="or `flow detail`"),
    "modify_flow": ParityEntry("flow update"),
    "update_flow": ParityEntry("flow update"),
    # -- Jobs ----------------------------------------------------------------
    "get_jobs": ParityEntry("job list", note="or `job detail`"),
    "run_job": ParityEntry("job run", note="add --wait to collect the result"),
    # -- OAuth ---------------------------------------------------------------
    "create_oauth_url": ParityEntry("config oauth-url"),
    # -- Data apps -----------------------------------------------------------
    "create_python_js_data_app_git_credential": ParityEntry("data-app git-credentials-create"),
    "delete_python_js_data_app_draft": ParityEntry("data-app delete"),
    "deploy_data_app": ParityEntry("data-app deploy", note="or `data-app stop`"),
    "get_data_apps": ParityEntry("data-app list", note="or `data-app detail`"),
    "modify_python_js_data_app": ParityEntry(
        "data-app create", note="update via `config update` + `data-app secrets-set`"
    ),
    "modify_streamlit_data_app": ParityEntry(
        "data-app create", note="update via `config update` + `data-app secrets-set`"
    ),
    # -- Project -------------------------------------------------------------
    "get_project_info": ParityEntry("project info"),
    "update_project_description": ParityEntry("project description-set"),
    # -- SQL -----------------------------------------------------------------
    "query_data": ParityEntry(
        "workspace query",
        note="intentionally unported (#390): workspace query composes multi-step "
        "SQL over a persistent workspace instead of an implicit one-shot SELECT",
    ),
    # -- Search --------------------------------------------------------------
    "find_component_id": ParityEntry("component list", note="use --query"),
    "search": ParityEntry("search"),
    # -- Semantic layer ------------------------------------------------------
    "get_semantic_context": ParityEntry(
        "semantic-layer show", note="or `semantic-layer get-context`"
    ),
    "get_semantic_schema": ParityEntry("semantic-layer schema"),
    "search_semantic_context": ParityEntry("semantic-layer search-context"),
    "validate_semantic_query": ParityEntry(
        "semantic-layer validate",
        note="intentionally unported (#390): model-level validation; the MCP "
        "tool's per-query string heuristics were rejected as drift-prone",
    ),
    # -- Storage -------------------------------------------------------------
    "get_buckets": ParityEntry("storage buckets", note="or `storage bucket-detail`"),
    "get_tables": ParityEntry("storage tables", note="or `storage table-detail`"),
    "update_descriptions": ParityEntry(
        "storage describe-batch",
        note="or describe-table / describe-bucket / describe-column",
    ),
}

# Single-token commands whose registry key is just the command name.
_SINGLE_TOKEN_OPERATIONS: frozenset[str] = frozenset({"search"})


def parity_operation_key(entry: ParityEntry) -> str:
    """Derive the OPERATION_REGISTRY key for an entry's canonical command."""
    tokens = entry.command.split()
    if tokens[0] in _SINGLE_TOKEN_OPERATIONS:
        return tokens[0]
    return f"{tokens[0]}.{tokens[1]}"


def native_equivalent(tool_name: str) -> ParityEntry | None:
    """Native replacement for *tool_name*, or None for an unmapped tool."""
    return MCP_TOOL_PARITY.get(tool_name)


def deprecation_message(tool_name: str) -> str:
    """One-line deprecation warning for ``tool call`` (human + JSON envelope)."""
    entry = native_equivalent(tool_name)
    if entry is None:
        return (
            f"MCP passthrough is deprecated (epic #390) and tool {tool_name!r} has "
            f"no native equivalent yet -- please report it at "
            f"https://github.com/keboola/cli/issues/390 before the group is "
            f"removed in v{MCP_REMOVAL_VERSION} ({MCP_REMOVAL_TARGET_DATE})."
        )
    suffix = f" ({entry.note})" if entry.note else ""
    return (
        f"MCP passthrough is deprecated (epic #390); use `kbagent {entry.command}` "
        f"instead{suffix}. The `tool` group is REMOVED in kbagent "
        f"v{MCP_REMOVAL_VERSION} ({MCP_REMOVAL_TARGET_DATE})."
    )
