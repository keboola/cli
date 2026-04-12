"""Changelog data for kbagent releases.

Maintained manually: one-line summaries per version.
Run ``make changelog`` to scaffold new entries from GitHub releases.
"""

from __future__ import annotations

# Ordered newest-first.  Each value is a list of brief one-line descriptions.
CHANGELOG: dict[str, list[str]] = {
    "0.18.2": [
        "New: storage download-table -- export table data to CSV (#130)",
        "New: storage table-detail -- show columns, types, primary key (#130)",
        "Fix: Azure upload uses absUploadParams with write-capable SAS (#131)",
        "Fix: AWS upload uses federation token with SigV4 signing (#131)",
        "Fix: sync status detects code file changes (transform.sql etc.) (#132)",
        "Fix: sync status no longer shows phantom configs after branch switch (#132)",
        "Fix: SQL parser preserves content between BLOCK and CODE markers (#132)",
    ],
    "0.18.1": [
        "Changelog command: kbagent changelog (#126)",
        "What's new display after auto-update",
    ],
    "0.18.0": [
        "Auto-update on startup (opt-out: KBAGENT_AUTO_UPDATE=false)",
        "Fix: sync pull dev-branch writes to correct directory (#121)",
        "Sync command is now stable (BETA removed)",
    ],
    "0.17.5": [
        "Fix: preserve multi-element script[] arrays in sync pull/push (#120)",
    ],
    "0.17.4": [
        "Encrypt command for Keboola Encryption API (#117)",
        "Fix: sync push no longer falls back to plaintext (#117)",
    ],
    "0.17.3": [
        "Branch support (--branch) for all storage commands (#114)",
    ],
    "0.17.2": [
        "Token refresh command: project refresh (#110)",
        "MCP server resolution fix (#109)",
    ],
    "0.17.1": [
        "Storage write operations: create-bucket, create-table, upload-table (#100)",
    ],
    "0.17.0": [
        "Permissions firewall for AI agent sandboxing",
        "Storage delete commands: delete-table, delete-bucket",
    ],
    "0.16.6": [
        "Snowflake gotchas and SQL migration guidance in plugin docs",
    ],
    "0.16.5": [
        "Fix: sync diff encrypted value false positives",
    ],
    "0.16.4": [
        "Fix: sync push config creation and update reliability",
    ],
    "0.16.3": [
        "Sync push: create, update, delete configs via API",
        "3-way diff engine for conflict detection",
    ],
    "0.16.2": [
        "Fix: sync status and diff edge cases",
    ],
    "0.16.1": [
        "Fix: sync pull row handling and manifest consistency",
    ],
    "0.16.0": [
        "Cross-project bucket sharing commands (#72)",
        "Self-update command: kbagent update (#73)",
    ],
    "0.15.5": [
        "Claude Code plugin with SKILL.md and reference docs",
    ],
    "0.15.4": [
        "Component scaffold: kbagent config new (#68)",
    ],
    "0.15.3": [
        "Fix: component list pagination",
    ],
    "0.15.2": [
        "Component discovery: component list, component detail",
    ],
    "0.15.1": [
        "Fix: retryable flag in error responses",
        "Deduplicate HTTP clients via BaseHttpClient",
    ],
    "0.15.0": [
        "Non-admin org setup via --project-ids",
    ],
    "0.14.0": [
        "Org setup: bulk onboarding via kbagent org setup",
    ],
    "0.13.1": [
        "Fix: workspace query error handling",
    ],
    "0.13.0": [
        "Workspace query: run SQL on Snowflake workspaces",
    ],
    "0.12.1": [
        "Fix: workspace create with read-only mode",
    ],
    "0.12.0": [
        "Workspace lifecycle: create, list, delete, load tables",
    ],
    "0.11.0": [
        "Branch lifecycle: create, use, reset, delete, merge",
    ],
    "0.10.0": [
        "MCP tool integration: tool list, tool call",
    ],
    "0.9.0": [
        "Cross-project data lineage: lineage show",
    ],
    "0.8.0": [
        "Job history: job list, job detail",
    ],
    "0.7.6": [
        "Fix: config search regex edge cases",
    ],
    "0.7.5": [
        "Fix: config detail output formatting",
    ],
    "0.7.4": [
        "Fix: multi-project parallel execution stability",
    ],
    "0.7.3": [
        "Fix: config list component type filtering",
    ],
    "0.7.2": [
        "Fix: project status connection timeout handling",
    ],
    "0.7.0": [
        "Config search with regex and multi-project support",
    ],
    "0.6.7": [
        "Fix: token masking for short tokens",
    ],
    "0.6.6": [
        "Fix: JSON output consistency across commands",
    ],
    "0.6.5": [
        "Fix: config list pagination for large projects",
    ],
    "0.6.0": [
        "Config browsing: config list, config detail",
    ],
    "0.5.0": [
        "Storage API: buckets, tables, bucket-detail",
    ],
    "0.4.1": [
        "Fix: project edit validation",
    ],
    "0.4.0": [
        "Project management: add, list, remove, edit, status",
    ],
}

# Number of versions shown by default in ``kbagent changelog``
DEFAULT_CHANGELOG_LIMIT = 5

# Environment variable set by auto_update before re-exec
ENV_UPDATED_FROM = "KBAGENT_UPDATED_FROM"


def get_changelog(limit: int = DEFAULT_CHANGELOG_LIMIT) -> dict[str, list[str]]:
    """Return the *limit* most recent changelog entries."""
    items = list(CHANGELOG.items())[:limit]
    return dict(items)


def get_version_notes(version: str) -> list[str] | None:
    """Return changelog entries for a specific version, or None."""
    return CHANGELOG.get(version)


def format_whats_new(old_version: str, new_version: str) -> str:
    """Format a brief 'What's new' message for display after auto-update.

    Shows entries for the new version only (not intermediate versions).
    """
    notes = get_version_notes(new_version)
    if not notes:
        return ""
    lines = [f"  What's new in v{new_version}:"]
    for note in notes:
        lines.append(f"    - {note}")
    return "\n".join(lines) + "\n"
