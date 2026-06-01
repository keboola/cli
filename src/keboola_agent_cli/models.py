"""Pydantic models shared across all layers of the application."""

import sys
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def normalize_stack_url(value: str) -> str:
    """Normalize a user-supplied Keboola stack URL to its scheme+host base.

    Accepts, in order of forgiveness:
      - a bare host                ``connection.keboola.com``
      - a full base URL            ``https://connection.keboola.com``
      - a full base URL + slash    ``https://connection.keboola.com/``
      - a full project deep-link   ``https://connection.keboola.com/admin/projects/10105/dashboard``

    and reduces every form to ``https://<host>`` (path/query/fragment dropped).
    A missing scheme defaults to ``https://``. Any *explicit* non-https scheme
    (``http://``, ``file://``, ``ftp://``, ...) is rejected -- this is an
    SSRF / protocol-abuse guard, so we never silently upgrade a typed-out
    ``http://`` to https.

    Raises:
        ValueError: empty input, an explicit non-https scheme, or no host.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("Stack URL must not be empty.")
    # No scheme typed -> assume https so urlparse sees a netloc, not a path.
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ValueError(
            f"Stack URL must use https:// scheme, got: {parsed.scheme or '(none)'}://. "
            "Plain HTTP, file://, and other protocols are not allowed."
        )
    if not parsed.netloc:
        raise ValueError(
            f"Stack URL has no host: {value!r}. Expected e.g. "
            "'connection.keboola.com' or 'https://connection.keboola.com'."
        )
    return f"https://{parsed.netloc}"


class ProjectConfig(BaseModel):
    """Configuration for a single Keboola project connection."""

    stack_url: str = Field(description="Keboola stack URL, e.g. https://connection.keboola.com")
    token: str = Field(description="Storage API token")
    project_name: str = Field(
        default="", description="Human-readable project name (populated on add)"
    )
    project_id: int | None = Field(
        default=None, description="Keboola project ID (populated on add)"
    )
    active_branch_id: int | None = Field(
        default=None,
        description="Active development branch ID (None = main/production branch)",
    )
    org_id: int | None = Field(
        default=None,
        description="Organization ID (populated via `org setup` or when verify_token returns it)",
    )
    org_name: str | None = Field(
        default=None,
        description="Organization name (populated via `org setup` or when verify_token returns it)",
    )
    ephemeral: bool = Field(
        default=False,
        exclude=True,
        description=(
            "True for an in-memory project synthesized from KBC_TOKEN + "
            "KBC_STORAGE_API_URL (headless mode, issue #359). Excluded from "
            "serialization and stripped by ConfigStore.save() so the env "
            "token is never written to disk."
        ),
    )

    @field_validator("stack_url")
    @classmethod
    def validate_stack_url_scheme(cls, v: str) -> str:
        """Normalize the stack URL to ``https://<host>`` (see ``normalize_stack_url``).

        Accepts a bare host, a full base URL, or a full project deep-link and
        reduces it to the scheme+host base; rejects explicit non-https schemes
        (SSRF / protocol-abuse guard).
        """
        return normalize_stack_url(v)


class DeveloperPortalIdentity(BaseModel):
    """One Developer Portal identity (service account or admin email).

    DP login is email + password (with MFA on personal accounts), producing
    a short-lived bearer that lives only in process memory. The username +
    password are persisted in config.json under the same 0600 protection as
    KB Storage tokens; the bearer is never written to disk.
    """

    username: str = Field(description="Email or service-account id used as the login subject")
    password: str = Field(description="DP password — same protection as KB tokens")
    role_hint: str = Field(
        default="vendor",
        description=(
            "Identity role: 'vendor' (default) or 'admin'. Load-bearing -- "
            "write commands route to different apps-api endpoints based on "
            "role: 'admin' uses PATCH /admin/apps/{app} (permissive schema, "
            "can set complexity/categories/forwardToken/processTimeout/etc.); "
            "'vendor' uses PATCH /vendors/{vendor}/apps/{app} (those fields "
            "are forbidden()). kbagent does not verify the server-side role "
            "of the credential -- if you set 'admin' but the account isn't "
            "actually a portal admin, the write fails at the apps-api with "
            "an unambiguous 403."
        ),
    )
    vendor: str | None = Field(
        default=None,
        description=(
            "Optional default vendor for this identity (e.g. 'keboola'). "
            "Used as a default for commands that take --vendor; never "
            "overrides an explicit flag."
        ),
    )
    portal_url: str = Field(
        default="https://apps-api.keboola.com",
        description="DP base URL. Override for staging/test portals.",
    )

    @field_validator("portal_url")
    @classmethod
    def validate_portal_url(cls, v: str) -> str:
        """Enforce HTTPS scheme on portal URL to prevent SSRF and protocol abuse."""
        if not v.startswith("https://"):
            raise ValueError(f"Portal URL must use https:// scheme, got: {v!r}")
        return v

    @field_validator("role_hint", mode="before")
    @classmethod
    def validate_role_hint(cls, v: object) -> str:
        """Normalise `role_hint` to the validated enum {vendor, admin}.

        Before v0.51.1 the field was free-text and documented as "not
        validated against the portal", so existing on-disk configs may
        carry arbitrary strings (e.g. 'keboola-admin', empty string,
        non-string types from hand-edits). A strict raise would crash
        `ConfigStore.load()` -> the entire CLI on startup for every
        pre-0.51.1 user with a non-standard value; that's a worse UX
        than a silent downgrade.

        Behaviour:
        - "vendor" / "admin" (case-insensitive, whitespace-stripped)
          pass through normalised.
        - Anything else is downgraded to "vendor" with a one-shot stderr
          warning. The user still sees what happened; the CLI keeps
          working. To force admin routing they can rerun
          `dev-portal identity edit --alias A --role-hint admin`.
        """
        if not isinstance(v, str):
            v = "" if v is None else str(v)
        normalized = v.strip().lower()
        if normalized in ("vendor", "admin"):
            return normalized
        sys.stderr.write(
            f"Warning: role_hint={v!r} is not 'vendor' or 'admin' -- "
            "downgrading to 'vendor'. Use `kbagent dev-portal identity "
            "edit --alias <A> --role-hint admin` to switch.\n"
        )
        return "vendor"


class PermissionPolicy(BaseModel):
    """Firewall-style permission policy for CLI and MCP operations.

    Controls which operations are allowed/blocked:
    - mode='allow' (default-allow): everything allowed unless in deny list
    - mode='deny' (default-deny): everything denied unless in allow list

    Patterns support exact names, globs, and categories:
    - Exact: 'branch.delete', 'tool:create_config'
    - Glob: 'sync.*', 'tool:create_*'
    - Category: 'cli:write', 'cli:read', 'tool:write', 'tool:read'
    """

    mode: str = Field(
        default="allow",
        description="Base mode: 'allow' (default-allow) or 'deny' (default-deny)",
    )
    allow: list[str] = Field(
        default_factory=list,
        description="Allowed operation patterns (used when mode='deny')",
    )
    deny: list[str] = Field(
        default_factory=list,
        description="Denied operation patterns (used when mode='allow')",
    )

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Enforce valid mode values."""
        if v not in ("allow", "deny"):
            raise ValueError(f"Permission mode must be 'allow' or 'deny', got: {v!r}")
        return v


class AppConfig(BaseModel):
    """Top-level application configuration persisted to config.json."""

    version: int = Field(default=1, description="Config schema version for future migrations")
    default_project: str = Field(default="", description="Alias of the default project")
    max_parallel_workers: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max concurrent threads for multi-project operations (env: KBAGENT_MAX_PARALLEL_WORKERS)",
    )
    permissions: PermissionPolicy | None = Field(
        default=None,
        description="Firewall-style permission policy (None = no restrictions)",
    )
    projects: dict[str, ProjectConfig] = Field(
        default_factory=dict,
        description="Map of alias -> ProjectConfig",
    )
    dev_portal_identities: dict[str, DeveloperPortalIdentity] = Field(
        default_factory=dict,
        description="Map of alias -> DeveloperPortalIdentity",
    )
    default_dev_portal_identity: str = Field(
        default="",
        description="Alias of the default identity for `kbagent dev-portal` commands",
    )


class TokenVerifyResponse(BaseModel):
    """Response from the Keboola token verification endpoint."""

    token_id: str = Field(description="Token identifier")
    token_description: str = Field(description="Human-readable token description")
    project_id: int | None = Field(default=None, description="Keboola project numeric ID")
    project_name: str = Field(description="Keboola project name")
    owner_name: str = Field(description="Project owner name")
    default_backend: str = Field(
        default="snowflake",
        description="Project default backend (snowflake, bigquery, etc.)",
    )
    features: list[str] = Field(
        default_factory=list,
        description="Project feature flags (e.g. agent-chat, storage-types)",
    )
    org_id: int | None = Field(
        default=None,
        description="Organization ID parsed from owner.organization (when present)",
    )
    org_name: str | None = Field(
        default=None,
        description="Organization name parsed from owner.organization (when present)",
    )


class ComponentDetail(BaseModel):
    """Component detail from Keboola AI Service /docs/components/{id} endpoint."""

    component_id: str = Field(alias="componentId")
    component_name: str = Field(alias="componentName")
    component_type: str = Field(alias="componentType")
    component_categories: list[str] = Field(default_factory=list, alias="componentCategories")
    component_flags: list[str] = Field(default_factory=list, alias="componentFlags")
    description: str = Field(default="")
    long_description: str = Field(default="", alias="longDescription")
    documentation: str = Field(default="")
    documentation_url: str = Field(default="", alias="documentationUrl")
    configuration_schema: dict[str, Any] = Field(default_factory=dict, alias="configurationSchema")
    configuration_row_schema: dict[str, Any] = Field(
        default_factory=dict, alias="configurationRowSchema"
    )
    root_configuration_examples: list[dict[str, Any]] = Field(
        default_factory=list, alias="rootConfigurationExamples"
    )
    row_configuration_examples: list[dict[str, Any]] = Field(
        default_factory=list, alias="rowConfigurationExamples"
    )

    model_config = {"populate_by_name": True}


class ComponentSuggestion(BaseModel):
    """Single result from AI Service /suggest/component endpoint."""

    component_id: str = Field(alias="componentId")
    score: float = Field(default=0.0)
    source: str = Field(default="")

    model_config = {"populate_by_name": True}


class ErrorResponse(BaseModel):
    """Structured error response for JSON output mode."""

    code: str = Field(description="Machine-readable error code, e.g. INVALID_TOKEN")
    error_type: str = Field(
        default="unknown",
        description="Broad error category: authentication, network, configuration, not_found, validation, api, unknown",
    )
    message: str = Field(description="Human-readable error description")
    project: str = Field(default="", description="Project alias related to the error, if any")
    retryable: bool = Field(default=False, description="Whether the operation can be retried")
    details: dict | None = Field(
        default=None,
        description=(
            "Optional structured context keyed by the producer (e.g. logTail "
            "for failed Queue jobs). Absent when empty so JSON consumers can "
            "assume 'details in err' implies non-empty payload."
        ),
    )


class SuccessResponse(BaseModel):
    """Structured success response for JSON output mode."""

    status: str = Field(default="ok", description="Always 'ok' for success responses")
    data: Any = Field(default=None, description="Response payload")


class ProjectMember(BaseModel):
    """Active project member as returned by GET /manage/projects/{id}/users.

    The Manage API returns audit-relevant fields beyond what kbagent renames
    explicitly: ``created``, ``expires``, ``invitor``, ``approver``, ``features``,
    ``canAccessLogs``, ``isSuperAdmin``, ``canApproveMergeRequests``. We allow
    extras through unmodified so admins inspecting `--json` output get the full
    audit trail (who invited whom, when, status flags), not a narrow whitelist.
    """

    id: int = Field(description="Numeric Keboola user ID")
    email: str = Field(description="Member email address")
    name: str = Field(default="", description="Display name (may be empty for stub accounts)")
    role: str = Field(description="Project role: admin | guest | readOnly | share")
    status: str = Field(default="active", description="Membership status")
    mfa_enabled: bool = Field(default=False, alias="mfaEnabled")

    model_config = {"populate_by_name": True, "extra": "allow"}


class Feature(BaseModel):
    """A Keboola feature flag, from GET /manage/features or a project/user object.

    The Manage API has no published schema for features and the field set
    varies by stack version. Only ``name`` is treated as stable -- it is the
    identifier passed to the add/remove endpoints. Every field defaults to a
    safe empty value and extras pass through unmodified so ``--json`` output
    keeps whatever the stack returned (``id``, ``projectFeature``,
    ``adminFeature``, ``canBeManagedViaApi``, ...).

    Features embedded in a project/user ``features`` array may be returned as
    bare strings rather than objects; the service layer normalises those to
    ``{"name": <string>}`` before validation.
    """

    name: str = Field(default="", description="Feature code -- the value used to add/remove it")
    title: str = Field(default="", description="Human-readable name shown in the UI")
    description: str = Field(default="")
    type: str = Field(default="", description="Feature category (project | admin | global | ...)")

    model_config = {"populate_by_name": True, "extra": "allow"}


class InvitationUser(BaseModel):
    """Invited user inside an Invitation object."""

    id: int | None = Field(default=None)
    email: str
    name: str = Field(default="")


class ProjectInvitation(BaseModel):
    """Pending project invitation as returned by GET /manage/projects/{id}/invitations.

    Extras (``created``, ``expires``, ``creator``) pass through unmodified so
    callers can audit when invitations were created and by whom.
    """

    id: int = Field(description="Invitation ID -- pass to DELETE to cancel")
    role: str = Field(description="Role offered to the invitee")
    reason: str = Field(default="")
    user: InvitationUser = Field(description="The invited user (email + resolved id)")

    model_config = {"populate_by_name": True, "extra": "allow"}


class MemberInviteRow(BaseModel):
    """Per-row outcome of a bulk-invite operation.

    `status` = 'ok' (created), 'noop' (already invited or already a member),
    'failed' (any other error). `note` carries the human-readable explanation.
    """

    email: str
    project: str = Field(description="Alias or numeric ID as it appeared in the source CSV row")
    project_id: int | None = Field(default=None, description="Resolved numeric project ID")
    role: str = Field(default="")
    status: str = Field(description="ok | noop | failed")
    note: str = Field(default="")
    invitation_id: int | None = Field(default=None)


class BulkInviteResult(BaseModel):
    """Aggregate result of `kbagent project invite --from-csv`."""

    total: int
    succeeded: int
    noop: int
    failed: int
    rows: list[MemberInviteRow] = Field(default_factory=list)
    dry_run: bool = Field(default=False)
