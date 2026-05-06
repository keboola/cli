"""Hint definitions for the search command."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── search ────────────────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="search.search",
        description="Search for items by name or content across one or more projects",
        steps=[
            HintStep(
                comment=(
                    "Textual mode: call GET /v2/storage/global-search to match item names. "
                    "Requires the project's numeric ID (from verify_token). "
                    "Pass types[] to restrict to specific item types. "
                    "Config-based mode: use ConfigService.search_configs() instead to scan "
                    "full configuration JSON bodies for the query string."
                ),
                client=ClientCall(
                    method="global_search",
                    args={
                        "query": "{query}",
                        "project_id": "<project_id from verify_token().project_id>",
                        "types": "{item_type}",
                        "limit": "{limit}",
                    },
                    result_var="search_response",
                    result_hint="dict with 'all' (int) and 'items' (list[dict])",
                ),
                service=ServiceCall(
                    service_class="SearchService",
                    service_module="search_service",
                    method="search",
                    args={
                        "query": "{query}",
                        "aliases": "{project}",
                        "item_types": "{item_type}",
                        "search_type": "{search_type}",
                        "limit": "{limit}",
                    },
                ),
            ),
        ],
        notes=[
            "Textual search uses GET /v2/storage/global-search (name-based, fast). "
            "The endpoint requires projectIds[], types[], limit, offset, and branchTypes[].",
            "Config-based search scans full config JSON bodies via ConfigService.search_configs(). "
            "It is slower but finds matches inside configuration parameters, rows, etc.",
            "The project's numeric ID must be obtained first via "
            "KeboolaClient.verify_token().project_id before calling global_search().",
            "Item types: bucket, table, flow, transformation, configuration, configuration-row. "
            "User-facing aliases: 'config' maps to 'configuration', 'data-app' to 'configuration'.",
            "Results include: id, name, type, fullPath, componentId, projectId, projectName.",
        ],
    )
)
