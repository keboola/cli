"""Pydantic v2 models for .keboola/manifest.json (v2, camelCase via aliases).

Mirrors the manifest format used by the Keboola Go CLI so that
directories written by kbagent are compatible with `kbc` tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME, MANIFEST_VERSION

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class ManifestProject(BaseModel):
    """Project identification inside the manifest."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    api_host: str = Field(alias="apiHost")


class ManifestGitBranching(BaseModel):
    """Git branching settings."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    enabled: bool = False
    default_branch: str = Field(default="main", alias="defaultBranch")


class ManifestNaming(BaseModel):
    """Naming templates that control the filesystem layout."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    branch: str = "{branch_name}"
    config: str = "{component_type}/{component_id}/{config_name}"
    config_row: str = Field(default="rows/{config_row_name}", alias="configRow")
    scheduler_config: str = Field(default="schedules/{config_name}", alias="schedulerConfig")
    shared_code_config: str = Field(
        default="_shared/{target_component_id}", alias="sharedCodeConfig"
    )
    shared_code_config_row: str = Field(
        default="codes/{config_row_name}", alias="sharedCodeConfigRow"
    )
    variables_config: str = Field(default="variables", alias="variablesConfig")
    variables_values_row: str = Field(
        default="values/{config_row_name}", alias="variablesValuesRow"
    )
    data_app_config: str = Field(default="app/{component_id}/{config_name}", alias="dataAppConfig")


class ManifestBranch(BaseModel):
    """A branch entry in the manifest."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ManifestConfigRow(BaseModel):
    """A single configuration row reference."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    path: str


class ManifestConfiguration(BaseModel):
    """A single configuration reference inside the manifest."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    branch_id: int = Field(alias="branchId")
    component_id: str = Field(alias="componentId")
    id: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    rows: list[ManifestConfigRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Root manifest
# ---------------------------------------------------------------------------


class Manifest(BaseModel):
    """Root model for .keboola/manifest.json (schema version 2)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    version: int = MANIFEST_VERSION
    project: ManifestProject
    allow_target_env: bool = Field(default=True, alias="allowTargetEnv")
    git_branching: ManifestGitBranching = Field(alias="gitBranching")
    sort_by: str = Field(default="id", alias="sortBy")
    naming: ManifestNaming
    allowed_branches: list[str] = Field(default_factory=list, alias="allowedBranches")
    ignored_components: list[str] = Field(default_factory=list, alias="ignoredComponents")
    branches: list[ManifestBranch] = Field(default_factory=list)
    configurations: list[ManifestConfiguration] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Load / Save helpers
# ---------------------------------------------------------------------------


def load_manifest(project_root: Path) -> Manifest:
    """Load .keboola/manifest.json from *project_root*.

    Raises:
        FileNotFoundError: if the manifest file does not exist.
        ValueError: if the JSON cannot be parsed into a valid Manifest.
    """
    manifest_path = project_root / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found at {manifest_path}. Is this a Keboola project directory?"
        )

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    return Manifest.model_validate(raw)


def save_manifest(project_root: Path, manifest: Manifest) -> None:
    """Save *manifest* to .keboola/manifest.json.

    Uses ``by_alias=True`` so all keys are written in camelCase,
    matching the format expected by the Go CLI.
    """
    keboola_dir = project_root / KEBOOLA_DIR_NAME
    keboola_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = keboola_dir / MANIFEST_FILENAME
    payload = manifest.model_dump(mode="json", by_alias=True)
    manifest_path.write_text(
        json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
