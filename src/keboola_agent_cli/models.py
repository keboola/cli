"""Pydantic models shared across all layers of the application."""

from typing import Any

from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    """Configuration for a single Keboola project connection."""

    stack_url: str = Field(description="Keboola stack URL, e.g. https://connection.keboola.com")
    token: str = Field(description="Storage API token")
    project_name: str = Field(default="", description="Human-readable project name (populated on add)")
    project_id: int = Field(default=0, description="Keboola project ID (populated on add)")


class AppConfig(BaseModel):
    """Top-level application configuration persisted to config.json."""

    version: int = Field(default=1, description="Config schema version for future migrations")
    default_project: str = Field(default="", description="Alias of the default project")
    projects: dict[str, ProjectConfig] = Field(
        default_factory=dict,
        description="Map of alias -> ProjectConfig",
    )


class TokenVerifyResponse(BaseModel):
    """Response from the Keboola token verification endpoint."""

    token_id: str = Field(default="", description="Token identifier")
    token_description: str = Field(default="", description="Human-readable token description")
    project_id: int = Field(default=0, description="Keboola project numeric ID")
    project_name: str = Field(default="", description="Keboola project name")
    owner_name: str = Field(default="", description="Project owner name")


class ErrorResponse(BaseModel):
    """Structured error response for JSON output mode."""

    code: str = Field(description="Machine-readable error code, e.g. INVALID_TOKEN")
    message: str = Field(description="Human-readable error description")
    project: str = Field(default="", description="Project alias related to the error, if any")
    retryable: bool = Field(default=False, description="Whether the operation can be retried")


class SuccessResponse(BaseModel):
    """Structured success response for JSON output mode."""

    status: str = Field(default="ok", description="Always 'ok' for success responses")
    data: Any = Field(default=None, description="Response payload")
