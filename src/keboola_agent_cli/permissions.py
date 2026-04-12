"""Firewall-style permission engine for CLI commands and MCP tools.

Provides an operation registry mapping every CLI command and MCP tool category
to a risk level, and a PermissionEngine that evaluates allow/deny policies
with pattern matching (exact, glob, category).
"""

import fnmatch

from .errors import PermissionDeniedError
from .models import PermissionPolicy

# Risk categories for all CLI operations.
# read = no side effects, write = creates/modifies, destructive = deletes, admin = org-level
OPERATION_REGISTRY: dict[str, str] = {
    # Project management
    "project.add": "admin",
    "project.list": "read",
    "project.remove": "admin",
    "project.edit": "admin",
    "project.status": "read",
    "project.refresh": "admin",
    # Config browsing & management
    "config.list": "read",
    "config.detail": "read",
    "config.search": "read",
    "config.update": "write",
    "config.delete": "destructive",
    "config.new": "write",
    # Job history
    "job.list": "read",
    "job.detail": "read",
    # Lineage
    "lineage.show": "read",
    # Sharing
    "sharing.list": "read",
    "sharing.share": "write",
    "sharing.unshare": "write",
    "sharing.link": "write",
    "sharing.unlink": "write",
    # Organization
    "org.setup": "admin",
    # Branch lifecycle
    "branch.list": "read",
    "branch.create": "write",
    "branch.use": "write",
    "branch.reset": "write",
    "branch.delete": "destructive",
    "branch.merge": "write",
    # Workspace lifecycle
    "workspace.create": "write",
    "workspace.list": "read",
    "workspace.detail": "read",
    "workspace.delete": "destructive",
    "workspace.password": "read",
    "workspace.load": "write",
    "workspace.query": "write",
    "workspace.from-transformation": "write",
    # MCP tools
    "tool.list": "read",
    "tool.call": "write",
    # Component discovery
    "component.list": "read",
    "component.detail": "read",
    # Storage browsing
    "storage.buckets": "read",
    "storage.bucket-detail": "read",
    "storage.tables": "read",
    "storage.table-detail": "read",
    "storage.download-table": "read",
    # Storage write
    "storage.create-bucket": "write",
    "storage.create-table": "write",
    "storage.upload-table": "write",
    # Storage destructive
    "storage.delete-table": "destructive",
    "storage.delete-bucket": "destructive",
    # Encryption
    "encrypt.values": "write",
    # Sync / git workflow
    "sync.init": "write",
    "sync.pull": "write",
    "sync.status": "read",
    "sync.diff": "read",
    "sync.push": "write",
    "sync.branch-link": "write",
    "sync.branch-unlink": "write",
    "sync.branch-status": "read",
    # Top-level commands
    "init": "admin",
    "doctor": "read",
    "version": "read",
    "update": "admin",
    "changelog": "read",
    "context": "read",
    "repl": "read",
    # Permissions (always allowed -- listed for completeness)
    "permissions.list": "read",
    "permissions.show": "read",
    "permissions.set": "admin",
    "permissions.reset": "admin",
    "permissions.check": "read",
}

# Prefixes for classifying MCP tools (mirrors mcp_service.py WRITE_PREFIXES)
_MCP_WRITE_PREFIXES = ("create_", "update_", "add_", "set_")
_MCP_DESTRUCTIVE_PREFIXES = ("delete_", "remove_")


def classify_mcp_tool(tool_name: str) -> str:
    """Classify an MCP tool by its name prefix.

    Returns:
        Risk category: 'read', 'write', or 'destructive'.
    """
    if tool_name.startswith(_MCP_DESTRUCTIVE_PREFIXES):
        return "destructive"
    if tool_name.startswith(_MCP_WRITE_PREFIXES):
        return "write"
    return "read"


def _matches_pattern(operation: str, pattern: str) -> bool:
    """Check if an operation matches a permission pattern.

    Supports:
    - Exact: 'branch.delete' matches 'branch.delete'
    - Glob: 'sync.*' matches 'sync.push', 'tool:create_*' matches 'tool:create_config'
    - Category 'cli:read' matches all CLI ops with category 'read'
    - Category 'cli:write' matches all CLI ops with category 'write' or 'destructive' or 'admin'
    - Category 'tool:read' matches all MCP tools (tool:*) with read classification
    - Category 'tool:write' matches all MCP tools (tool:*) with write or destructive classification
    """
    # Category patterns: cli:read, cli:write, tool:read, tool:write
    if pattern in ("cli:read", "cli:write", "cli:destructive", "cli:admin"):
        # cli:* patterns only match CLI operations, never MCP tools
        if operation.startswith("tool:"):
            return False
        target_category = pattern.split(":")[1]
        # Fail-closed: unknown CLI ops default to 'write' so they are
        # blocked by cli:write policies. This prevents new commands from
        # bypassing restrictions if OPERATION_REGISTRY is not updated.
        op_category = OPERATION_REGISTRY.get(operation, "write")
        if target_category == "write":
            # cli:write matches write, destructive, and admin
            return op_category in ("write", "destructive", "admin")
        return op_category == target_category

    if pattern in ("tool:read", "tool:write", "tool:destructive"):
        target_category = pattern.split(":")[1]
        if not operation.startswith("tool:"):
            return False
        tool_name = operation[5:]  # strip 'tool:' prefix
        tool_category = classify_mcp_tool(tool_name)
        if target_category == "write":
            # tool:write matches write and destructive
            return tool_category in ("write", "destructive")
        return tool_category == target_category

    # Exact or glob match
    return fnmatch.fnmatch(operation, pattern)


class PermissionEngine:
    """Evaluates permission policies against operations.

    Thread-safe and stateless per call -- safe to share across contexts.
    """

    def __init__(self, policy: PermissionPolicy | None) -> None:
        self._policy = policy

    @property
    def active(self) -> bool:
        """Whether a permission policy is configured."""
        return self._policy is not None

    def is_allowed(self, operation: str) -> bool:
        """Check if an operation is allowed by the active policy.

        Returns True if no policy is configured (no restrictions).

        Fail-closed: CLI operations not in OPERATION_REGISTRY are treated as
        'write' for category matching. This ensures new commands added without
        updating the registry are blocked by policies like 'deny cli:write'.
        """
        if self._policy is None:
            return True

        # permissions.* commands are always allowed (prevent lockout)
        if operation.startswith("permissions."):
            return True

        denied = any(_matches_pattern(operation, p) for p in self._policy.deny)
        allowed = any(_matches_pattern(operation, p) for p in self._policy.allow)

        if self._policy.mode == "allow":
            # Default-allow: blocked only if deny matches (and allow doesn't override)
            return not (denied and not allowed)
        # Default-deny: allowed only if allow matches (and deny doesn't override)
        return bool(allowed and not denied)

    def check_or_raise(self, operation: str) -> None:
        """Check if an operation is allowed, raising PermissionDeniedError if not."""
        if not self.is_allowed(operation):
            raise PermissionDeniedError(operation)

    def list_operations(self) -> list[dict[str, str]]:
        """List all known operations with their category and allowed/denied status.

        Returns a list of dicts with keys: name, category, status ('allowed' or 'denied').
        Includes both CLI operations and MCP tool category summaries.
        """
        ops: list[dict[str, str]] = []

        # CLI operations
        for name, category in sorted(OPERATION_REGISTRY.items()):
            status = "allowed" if self.is_allowed(name) else "denied"
            ops.append({"name": name, "type": "cli", "category": category, "status": status})

        # MCP tool categories (virtual entries for reference)
        mcp_categories = [
            ("tool:read", "read", "All MCP read tools (get_*, list_*, search, find_*, docs_query)"),
            ("tool:write", "write", "All MCP write tools (create_*, update_*, add_*, set_*)"),
            (
                "tool:destructive",
                "destructive",
                "All MCP destructive tools (delete_*, remove_*)",
            ),
        ]
        for name, category, description in mcp_categories:
            # Check a representative tool for status
            representative = "tool:get_buckets" if category == "read" else "tool:create_config"
            if category == "destructive":
                representative = "tool:delete_config"
            status = "allowed" if self.is_allowed(representative) else "denied"
            ops.append(
                {
                    "name": name,
                    "type": "mcp",
                    "category": category,
                    "status": status,
                    "description": description,
                }
            )

        return ops
