"""Pydantic v2 models for .keboola/manifest.json (v2, camelCase via aliases).

Mirrors the manifest format used by the Keboola Go CLI so that
directories written by kbagent are compatible with `kbc` tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def _posix_path(value: str) -> str:
    r"""Normalise a manifest path to forward slashes.

    The manifest is a **tracked** file -- the point of a sync tree is that a
    team shares it through git -- so its paths must mean the same thing on
    every machine. The asymmetry is what makes this bite: ``Path()`` on Windows
    happily accepts ``a/b``, but on POSIX ``a\b`` is a single filename that
    merely contains a backslash. So a manifest written on Windows silently
    stops resolving for everyone else on the team, while the reverse direction
    works fine and hides the problem.

    Applied on load as well as on write, so a manifest already committed by a
    Windows kbagent repairs itself on the next read instead of needing a hand
    edit. Component and config names are slugified by :mod:`.naming` long
    before they reach a path, so a backslash here is always a separator and
    never part of a real name.
    """
    return value.replace("\\", "/")


class ManifestBranch(BaseModel):
    """A branch entry in the manifest."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return _posix_path(value)


class ManifestConfigRow(BaseModel):
    """A single configuration row reference.

    ``metadata`` mirrors :class:`ManifestConfiguration.metadata` and stores
    pull-time hashes (``pull_hash``, ``pull_config_hash``) so the row-level
    diff can distinguish local-changed, remote-changed, and conflict states.
    Older manifests that lack the field load with an empty dict and upgrade
    on the next successful pull.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return _posix_path(value)


class ManifestConfiguration(BaseModel):
    """A single configuration reference inside the manifest."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    branch_id: int = Field(alias="branchId")
    component_id: str = Field(alias="componentId")
    id: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    rows: list[ManifestConfigRow] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return _posix_path(value)


# ---------------------------------------------------------------------------
# Root manifest
# ---------------------------------------------------------------------------


class Manifest(BaseModel):
    """Root model for .keboola/manifest.json (schema version 3)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    version: int = MANIFEST_VERSION
    project: ManifestProject
    allow_target_env: bool = Field(default=True, alias="allowTargetEnv")
    git_branching: ManifestGitBranching = Field(
        default_factory=ManifestGitBranching, alias="gitBranching"
    )
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
