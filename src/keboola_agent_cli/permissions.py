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
    "project.description-get": "read",
    "project.description-set": "write",
    "project.use": "write",
    "project.current": "read",
    "project.info": "read",
    "project.invite": "admin",
    "project.member-list": "read",
    "project.invitation-list": "read",
    "project.invitation-cancel": "admin",
    "project.member-remove": "destructive",
    "project.member-set-role": "admin",
    # Feature flags (super-admin manage token). Reads are safe; enabling a
    # feature is an org-level decision (admin); removing one is destructive.
    "feature.list": "read",
    "feature.project-show": "read",
    "feature.project-add": "admin",
    "feature.project-remove": "destructive",
    "feature.user-show": "read",
    "feature.user-add": "admin",
    "feature.user-remove": "destructive",
    # Data Streams (OTLP). Listing/inspecting sources is read-only; creating a
    # source provisions ingest infrastructure (write); deleting one is destructive.
    "stream.list": "read",
    "stream.detail": "read",
    "stream.create-source": "write",
    "stream.delete": "destructive",
    # Storage API scoped tokens: minting/rotating is a write (creates a
    # credential), revoking is destructive (a live token stops working).
    "token.create": "write",
    "token.refresh": "write",
    "token.delete": "destructive",
    # Config browsing & management
    "config.list": "read",
    "config.detail": "read",
    "config.search": "read",
    "config.update": "write",
    "config.set-default-bucket": "write",
    "config.rename": "write",
    "config.delete": "destructive",
    "config.new": "write",
    "config.variables-set": "write",
    "config.variables-get": "read",
    "config.variables-clear": "destructive",
    "config.metadata-list": "read",
    "config.get-metadata": "read",
    "config.set-metadata": "write",
    "config.delete-metadata": "destructive",
    "config.set-folder": "write",
    "config.row-create": "write",
    "config.row-update": "write",
    "config.row-delete": "destructive",
    "config.oauth-url": "read",
    # Job history
    "job.list": "read",
    "job.detail": "read",
    "job.run": "write",
    "job.terminate": "destructive",
    # Lineage
    "lineage.build": "read",
    "lineage.info": "read",
    "lineage.show": "read",
    "lineage.server": "read",
    # Sharing
    "sharing.list": "read",
    "sharing.edges": "read",
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
    "branch.metadata-list": "read",
    "branch.metadata-get": "read",
    "branch.metadata-set": "write",
    "branch.metadata-delete": "destructive",
    # Workspace lifecycle
    "workspace.create": "write",
    "workspace.list": "read",
    "workspace.detail": "read",
    "workspace.delete": "destructive",
    "workspace.password": "read",
    "workspace.load": "write",
    "workspace.query": "write",
    "workspace.from-transformation": "write",
    "workspace.gc": "destructive",
    # Scheduled agent tasks (kbagent agent ...; touches only local
    # agents.json + spawns subprocesses via the runner).
    "agent.list": "read",
    "agent.show": "read",
    "agent.create": "write",
    "agent.update": "write",
    "agent.delete": "destructive",
    # Run is classified write -- ai_agent/cli_command actions can mutate
    # external state via subprocesses; deny-writes should block this.
    "agent.run": "write",
    "agent.runs": "read",
    "agent.run-detail": "read",
    "agent.run-events": "read",
    "agent.test": "write",
    "agent.cron-preview": "read",
    "agent.prompt-improve": "write",
    # MCP tools
    "tool.list": "read",
    "tool.call": "write",
    # Kai (Keboola AI Assistant)
    "kai.ping": "read",
    "kai.preflight": "read",
    "kai.ask": "read",
    "kai.chat": "write",
    "kai.chat-detail": "read",
    "kai.history": "read",
    # Component discovery
    "component.list": "read",
    "component.detail": "read",
    # Developer Portal (since 0.48.0)
    # Developer Portal — top-level commands on `dev-portal` (the identity
    # sub-app's leaves are listed separately below under dev-portal.identity.*).
    # Categories follow data-app.secrets-* precedent: credential add/edit are
    # `write`, not `admin` (admin is reserved for org-level operations).
    "dev-portal.identity": "read",  # parent-callback descent (allow into sub-app)
    "dev-portal.list": "read",
    "dev-portal.get": "read",
    "dev-portal.create": "write",
    "dev-portal.patch": "write",
    "dev-portal.upload-icon": "write",
    "dev-portal.publish": "admin",
    "dev-portal.deprecate": "destructive",
    # Data apps (Data Science API + keboola.data-apps Storage component)
    "data-app.list": "read",
    "data-app.detail": "read",
    "data-app.password": "read",
    "data-app.logs": "read",
    "data-app.runs": "read",
    "data-app.create": "write",
    "data-app.deploy": "write",
    "data-app.start": "write",
    "data-app.stop": "write",
    "data-app.delete": "destructive",
    # Data apps - secrets + validate-repo (new in 0.28.0)
    "data-app.secrets-set": "write",
    "data-app.secrets-list": "read",
    "data-app.secrets-get": "read",
    "data-app.secrets-remove": "destructive",
    "data-app.validate-repo": "read",
    # Data apps - git-repo introspection + managed-repo credentials
    "data-app.git-repo": "read",
    "data-app.git-branches": "read",
    "data-app.git-entrypoints": "read",
    "data-app.git-credentials": "read",
    "data-app.git-credentials-create": "write",
    # Developer Portal — identity sub-app leaves (composed by the
    # identity_app callback as "dev-portal.identity.<subcommand>")
    "dev-portal.identity.add": "write",
    "dev-portal.identity.list": "read",
    "dev-portal.identity.remove": "write",
    "dev-portal.identity.edit": "write",
    "dev-portal.identity.use": "write",
    "dev-portal.identity.current": "read",
    "dev-portal.identity.verify": "read",
    # Storage browsing
    "storage.buckets": "read",
    "storage.bucket-detail": "read",
    "storage.tables": "read",
    "storage.table-detail": "read",
    "storage.download-table": "read",
    # Storage write
    "storage.create-bucket": "write",
    "storage.create-table": "write",
    "storage.add-column": "write",
    "storage.upload-table": "write",
    # clone-table pulls a prod table into a dev branch (materialization); it
    # creates a branch-local copy and never deletes -- write, not destructive.
    "storage.clone-table": "write",
    # Storage files
    "storage.files": "read",
    "storage.file-detail": "read",
    "storage.file-download": "read",
    "storage.file-upload": "write",
    "storage.file-tag": "write",
    "storage.load-file": "write",
    "storage.unload-table": "read",
    # Storage destructive
    "storage.delete-table": "destructive",
    "storage.delete-column": "destructive",
    "storage.delete-bucket": "destructive",
    "storage.file-delete": "destructive",
    "storage.swap-tables": "destructive",
    "storage.truncate-table": "destructive",
    # Storage descriptions
    "storage.describe-bucket": "write",
    "storage.describe-table": "write",
    "storage.describe-column": "write",
    "storage.describe-batch": "write",
    # Encryption
    "encrypt.values": "write",
    # Semantic layer (metastore) — new in 0.41.0
    "semantic-layer.show": "read",
    "semantic-layer.validate": "read",
    "semantic-layer.export": "read",
    "semantic-layer.diff": "read",
    "semantic-layer.search-context": "read",
    "semantic-layer.get-context": "read",
    # The `model` sub-app: the parent `semantic-layer` callback fires first
    # with ctx.invoked_subcommand == "model" and synthesizes operation key
    # ``semantic-layer.model``. We expose that key at the LEAST-privileged
    # leaf risk (read) so the parent permits descent — the model sub-app's
    # own callback then runs with the per-leaf keys below and enforces the
    # actual gate. Without the parent-level key the fail-closed default
    # treats ``semantic-layer.model`` as ``write`` and denies even
    # ``model list`` under ``--deny-writes``.
    "semantic-layer.model": "read",
    "semantic-layer.model.list": "read",
    "semantic-layer.model.create": "write",
    "semantic-layer.model.delete": "destructive",
    # The add/edit/remove sub-apps: same parent-callback pattern as `model`
    # above -- the parent semantic-layer callback fires with the collapsed
    # key, then the sub-app callback composes per-leaf operation keys via
    # the standard ``check_cli_permission`` helper. Every leaf inside one
    # sub-app shares the same risk class (add=write, edit=write,
    # remove=destructive), so the per-leaf keys all carry that class.
    "semantic-layer.add": "write",
    "semantic-layer.add.metric": "write",
    "semantic-layer.add.dataset": "write",
    "semantic-layer.add.relationship": "write",
    "semantic-layer.add.constraint": "write",
    "semantic-layer.add.glossary": "write",
    "semantic-layer.edit": "write",
    "semantic-layer.edit.metric": "write",
    "semantic-layer.edit.dataset": "write",
    "semantic-layer.edit.constraint": "write",
    "semantic-layer.edit.relationship": "write",
    "semantic-layer.edit.glossary": "write",
    "semantic-layer.import": "write",
    "semantic-layer.promote": "write",
    "semantic-layer.build": "write",
    # `token --encrypt` calls EncryptService, same blast radius as
    # `encrypt.values` which is `write`. Classified `write` for parity:
    # users opting out via --deny-writes block both consistently.
    "semantic-layer.token": "write",
    "semantic-layer.remove": "destructive",
    "semantic-layer.remove.metric": "destructive",
    "semantic-layer.remove.dataset": "destructive",
    "semantic-layer.remove.constraint": "destructive",
    "semantic-layer.remove.relationship": "destructive",
    "semantic-layer.remove.glossary": "destructive",
    # `reference-data` sub-app: dimension-member records (e.g. a Chart of
    # Accounts). Parent key at the least-privileged level (read) so the
    # top-level `semantic-layer` callback does not over-block `list` / `get`;
    # per-leaf keys carry the real classification.
    "semantic-layer.reference-data": "read",
    "semantic-layer.reference-data.list": "read",
    "semantic-layer.reference-data.get": "read",
    "semantic-layer.reference-data.set": "write",
    "semantic-layer.reference-data.delete": "destructive",
    # Raw HTTP client against `kbagent serve` (used by AI subprocesses).
    # Categorised by the underlying HTTP method: GET = read, mutating verbs
    # = write. The serve's own routes enforce their own permissions on top.
    "http.get": "read",
    "http.post": "write",
    "http.patch": "write",
    "http.delete": "destructive",
    # Sync / git workflow
    "sync.init": "read",
    "sync.pull": "read",
    "sync.status": "read",
    "sync.diff": "read",
    "sync.push": "write",
    "sync.clone": "write",
    "sync.branch-link": "write",
    "sync.branch-unlink": "write",
    "sync.branch-status": "read",
    # Flow operations
    "flow.list": "read",
    "flow.detail": "read",
    "flow.schema": "read",
    "flow.validate": "read",
    "flow.new": "write",
    "flow.update": "write",
    "flow.delete": "destructive",
    "flow.schedule": "write",
    "flow.schedule-remove": "destructive",
    # Schedule discovery / audit (read-only)
    "schedule.list": "read",
    "schedule.detail": "read",
    "schedule.find": "read",
    # Top-level commands
    "search": "read",
    "init": "admin",
    "doctor": "read",
    "version": "read",
    "update": "admin",
    "changelog": "read",
    "context": "read",
    "repl": "read",
    "serve": "admin",
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
