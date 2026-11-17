"""Pydantic v2 models for .keboola/manifest.json (v2, camelCase via aliases).

Mirrors the manifest format used by the Keboola Go CLI so that
directories written by kbagent are compatible with `kbc` tooling.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

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


def relative_path_problem(value: str) -> str | None:
    """Why *value* is not a plain relative path inside its base, or ``None`` (#827, F1).

    Refused: empty, absolute (``/x``, ``//server/share``), drive-qualified
    (``C:/x``; any segment with a colon, which Windows reads as a drive), and
    any empty, ``.`` or ``..`` segment.
    """
    if not value:
        return "it is empty"
    if value.startswith("/"):
        return "it is an absolute path"
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            return f"it has a {segment!r} segment" if segment else "it has an empty segment"
        if ":" in segment:
            return "it names a drive"
    return None


class ManifestPendingRemoval(BaseModel):
    """A directory a ``sync pull`` decided to delete and may not have deleted yet (#827).

    Pull saves the manifest without the removed entry and with this record
    BEFORE it deletes the directory; the final save drops the record once the
    directory is gone. A record that survives (a crash, a failed delete) makes
    diff and push treat *path* and everything below it as absent, and the next
    pull finishes or abandons the removal before it allocates any path.

    Attributes:
        tree: The branch tree, relative to the project root (``main``).
        path: The directory, relative to *tree*.
        kind: ``config`` or ``row``.
        file_hash: SHA-256 of its ``_config.yml`` when pull decided ("" if none).
        tree_hash: Digest of every file below *path* when pull decided.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    tree: str
    path: str
    kind: str
    component_id: str = Field(default="", alias="componentId")
    config_id: str = Field(default="", alias="configId")
    row_id: str = Field(default="", alias="rowId")
    file_hash: str = Field(default="", alias="fileHash")
    tree_hash: str = Field(default="", alias="treeHash")

    # Why the record is unsafe to act on, decided at load (F1). Never written.
    _problem: str | None = PrivateAttr(default=None)

    @field_validator("tree", "path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return _posix_path(value)

    @model_validator(mode="after")
    def _check_paths(self) -> ManifestPendingRemoval:
        for label, value in (("tree", self.tree), ("path", self.path)):
            problem = relative_path_problem(value)
            if problem is not None:
                self._problem = f"its {label} {value!r} is refused: {problem}"
                break
        return self

    @property
    def problem(self) -> str | None:
        """Why this record is never acted on (an unsafe path), or ``None``."""
        return self._problem


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
    # Opt-in (CLI-25): sync shared SQL workspaces (``keboola.sandboxes``) in
    # this tree. kbagent-only: ``kbc`` ignores unknown manifest keys on load.
    # ``save_manifest`` writes the key only while it is true.
    sync_workspaces: bool = Field(default=False, alias="syncWorkspaces")
    branches: list[ManifestBranch] = Field(default_factory=list)
    configurations: list[ManifestConfiguration] = Field(default_factory=list)
    # kbagent-only (#827): directories a pull has not finished deleting.
    # ``save_manifest`` writes the key only while it is non-empty; an older
    # kbagent keeps it as an unknown key (``extra="allow"``) and ignores it.
    pending_removals: list[ManifestPendingRemoval] = Field(
        default_factory=list, alias="pendingRemovals"
    )


class PendingPaths(frozenset[str]):
    """Paths a pull has not finished deleting, and the tracked paths inside them (#827, H1).

    A ``frozenset`` of the pending paths (so it unions and iterates like
    one) that also carries *exempt*: every tracked config and row directory.
    A tracked directory inside a pending folder (a config tracked again
    there) is never hidden by it -- only the rest of the folder is.
    """

    exempt: frozenset[str]

    def __new__(cls, paths: frozenset[str], exempt: frozenset[str] = frozenset()) -> Self:
        obj = super().__new__(cls, paths)
        obj.exempt = exempt
        return obj


def _at_or_below(path: str, prefixes: frozenset[str]) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in prefixes)


def under_pending(path: str, pending: frozenset[str]) -> bool:
    """True iff *path* (relative to its tree) is a pending removal or lies below one.

    Diff and push treat such a directory as absent: never a move target,
    never an untracked config to create or adopt, never a new row (#827, D11).
    A tracked directory strictly inside a pending folder, and everything below
    it, is not covered by THAT folder (H1) -- but a pending record at or below
    the tracked directory still covers its own path (I1): a row folder whose
    delete failed stays absent under its tracked configuration.
    """
    exempt = getattr(pending, "exempt", frozenset())
    for p in pending:
        if not (path == p or path.startswith(f"{p}/")):
            continue
        cancelled = any(
            t.startswith(f"{p}/") and (path == t or path.startswith(f"{t}/")) for t in exempt
        )
        if not cancelled:
            return True
    return False


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
    matching the format expected by the Go CLI. ``syncWorkspaces`` is left
    out while false and ``pendingRemovals`` while empty, so a tree that never
    used them keeps its manifest as it was.
    """
    keboola_dir = project_root / KEBOOLA_DIR_NAME
    keboola_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = keboola_dir / MANIFEST_FILENAME
    payload = manifest.model_dump(mode="json", by_alias=True)
    if not manifest.sync_workspaces:
        payload.pop("syncWorkspaces", None)
    if not manifest.pending_removals:
        payload.pop("pendingRemovals", None)
    data = (json.dumps(payload, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
    # Atomic: a temp file in the same directory, flushed to disk, then renamed
    # over the manifest. ``sync push`` saves after every create (a crash must
    # not lose the record of a created config), so an interrupted save must
    # leave the previous manifest whole, never a truncated one. The temp file
    # is created with 0666 so the OS applies the umask, exactly as a plain
    # write of a new file would; an existing manifest's mode is copied over.
    tmp_path = keboola_dir / f".{MANIFEST_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if manifest_path.exists():
            os.chmod(tmp_path, manifest_path.stat().st_mode & 0o777)
        os.replace(tmp_path, manifest_path)
        _fsync_dir(keboola_dir)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _fsync_dir(directory: Path) -> None:
    """Flush the rename to disk (POSIX; best effort, a no-op on Windows).

    ``os.replace`` is atomic, but only a sync of the directory makes the new
    name survive a power loss.
    """
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
