"""Shared dataclasses + component-id constants for the sync service.

Extracted from ``sync_service.py`` (which had grown to ~4000 lines, far past the
1500-LOC ceiling) so the binding helpers in ``_sync_bindings.py`` can import them
without a circular import. ``sync_service`` re-exports the names that external
callers/tests rely on (e.g. ``CreatedConfig``), so the public surface is
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..sync.manifest import ManifestConfiguration

# Sibling component that backs a transformation's variable links. A
# transformation references it via ``configuration.variables_id`` (the config)
# and ``configuration.variables_values_id`` (a row id).
VARIABLES_COMPONENT_ID = "keboola.variables"

# Conditional-flow component. A flow runs other configs via
# ``configuration.tasks[].task.configId`` (job-type tasks); the Phase-D backfill
# remaps those ids placeholder/source -> ULID after a fresh create (e.g. clone).
FLOW_COMPONENT_ID = "keboola.flow"


@dataclass
class WritebackResult:
    """Outcome of recording a freshly-created config in the manifest.

    ``previous_id`` is the manifest entry's id **before** the placeholder ->
    ULID overwrite (empty string when a brand-new entry was appended). The
    create pass uses it to key ``created_id_map`` so row parents and
    transformation variable links can be remapped placeholder -> ULID.
    """

    entry: ManifestConfiguration
    previous_id: str


@dataclass
class CreatedConfig:
    """A config created during a single ``push`` create pass.

    Carries just enough to drive the Phase-C variable-link backfill: the
    component id, the API-assigned ULID, and the on-disk directory holding
    the (post-writeback) ``_config.yml``.
    """

    component_id: str
    config_id: str
    config_dir: Path


@dataclass
class VariableBindingResult:
    """Outcome of the Phase-C variable-link backfill.

    ``configs_rewritten`` counts transformations whose remote configuration +
    local ``_configuration_extra`` were rebound to ULIDs (drives the
    manifest-dirty flag). ``errors`` accumulates unresolved links so the push
    envelope surfaces them instead of leaving a broken link silently.
    """

    errors: list[dict[str, str]] = field(default_factory=list)
    configs_rewritten: int = 0


@dataclass
class FlowBindingResult:
    """Outcome of the Phase-D flow-task-link backfill (#426).

    ``configs_rewritten`` counts flows whose task ``configId``s were remapped to
    ULIDs (drives the manifest-dirty flag); ``tasks_remapped`` is the total task
    references rewritten; ``errors`` accumulates PUT failures so the push
    envelope surfaces them.
    """

    errors: list[dict[str, str]] = field(default_factory=list)
    configs_rewritten: int = 0
    tasks_remapped: int = 0


@dataclass
class LocalConfigHashes:
    """Hashes describing a config dir's on-disk state after a push.

    ``file_hash`` is the ``_config.yml`` content hash, ``cfg_hash`` the
    normalized config hash (see :func:`config_hash`), and ``extra_hashes``
    maps each extracted code/companion file to its hash. Stored on the
    manifest entry so the next ``sync diff`` recognises local == remote.
    """

    file_hash: str
    cfg_hash: str
    extra_hashes: dict[str, str] = field(default_factory=dict)
