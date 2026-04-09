"""Pydantic models shared across all layers of the application."""

from typing import Any

from pydantic import BaseModel, Field, field_validator


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

    @field_validator("stack_url")
    @classmethod
    def validate_stack_url_scheme(cls, v: str) -> str:
        """Enforce HTTPS scheme on stack URL to prevent SSRF and protocol abuse."""
        if not v.startswith("https://"):
            raise ValueError(
                f"Stack URL must use https:// scheme, got: {v!r}. "
                "Plain HTTP, file://, and other protocols are not allowed."
            )
        return v


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


class SuccessResponse(BaseModel):
    """Structured success response for JSON output mode."""

    status: str = Field(default="ok", description="Always 'ok' for success responses")
    data: Any = Field(default=None, description="Response payload")
