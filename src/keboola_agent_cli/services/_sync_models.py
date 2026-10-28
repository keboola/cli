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
from typing import Any

from ..errors import ErrorCode
from ..sync.manifest import ManifestConfiguration

# Sibling component that backs a transformation's variable links. A
# transformation references it via ``configuration.variables_id`` (the config)
# and ``configuration.variables_values_id`` (a row id).
VARIABLES_COMPONENT_ID = "keboola.variables"

# Conditional-flow component. A flow runs other configs via
# ``configuration.tasks[].task.configId`` (job-type tasks); the Phase-D backfill
# remaps those ids placeholder/source -> ULID after a fresh create (e.g. clone).
FLOW_COMPONENT_ID = "keboola.flow"

# Legacy flow component. Its tasks carry ``task.componentId`` + ``task.configId``
# like a flow's, but no ``task.type``.
ORCHESTRATOR_COMPONENT_ID = "keboola.orchestrator"

# A schedule runs ``configuration.target.componentId`` /
# ``configuration.target.configurationId``.
SCHEDULER_COMPONENT_ID = "keboola.scheduler"

# The configs whose job is to run another config (Phase D).
RUN_TARGET_COMPONENTS = frozenset(
    {FLOW_COMPONENT_ID, ORCHESTRATOR_COMPONENT_ID, SCHEDULER_COMPONENT_ID}
)

# Sibling component that backs a transformation's shared-code links. A
# transformation references it via ``configuration.shared_code_id`` (the
# config) and ``configuration.shared_code_row_ids`` (row ids), and its scripts
# use each row as a ``{{<row id>}}`` placeholder.
SHARED_CODE_COMPONENT_ID = "keboola.shared-code"

# Manifest metadata of a promote write (``push --branch DEV`` reading main/)
# whose references may still name source ids on the target: the list of those
# source references, cleared once every one maps to a target id (D24, D30).
REBIND_PENDING_KEY = "rebind_pending"
# Row metadata: the source row a promote push created this row from (D24).
PROMOTED_ROW_KEY = "promoted_from_id"
# Config metadata: the source config a promoted entry stands for, kept when a
# pull of its branch moves the entry's files into that branch's own tree (its
# ``promoted_from`` then no longer applies) -- what a pending rebind needs.
PROMOTED_SOURCE_KEY = "promoted_source"
# Row metadata: a row a promote push created from a row file with no source id
# (authored in the source tree). Linked by path, once, when the source tree's
# own branch creates that row (risk 83).
AUTHORED_ROW_KEY = "promoted_authored"
# Config and row metadata outside the promote path: the placeholder id (a
# ``sync clone`` reference id, a scaffold's id) a push created this config or
# row from. Lets a later push map a reference still naming that id (D9).
CREATED_FROM_KEY = "created_from_id"
# Config metadata of a flow, orchestration or schedule a push created outside
# the promote path whose references may still name placeholder ids: set in its
# create checkpoint, cleared once every reference resolves (D9).
LINK_PENDING_KEY = "link_pending"
# Config metadata of a config registered by its id (adopted): every row id its
# local row dirs carried then, an ambiguous one included. A row dir that later
# carries one of them alone is that row, never a new one (round 6).
ADOPTED_ROW_IDS_KEY = "adopted_row_ids"


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
class CreateOutcome:
    """A config ``push_create`` POSTed, before any local bookkeeping (D25).

    The caller records the new id in the manifest and saves it FIRST, then
    writes the local file back (``pristine_data`` + the encrypted
    ``configuration``) and stamps the baselines. ``file_config_id`` is the id
    the local file may carry: empty on the promote path (D19, D23).
    """

    result: dict[str, Any]
    pristine_data: dict[str, Any]
    configuration: dict[str, Any]
    config_dir: Path
    file_config_id: str
    # Promote path: the source references this push still creates (D30).
    pending_refs: list[list[str]] = field(default_factory=list)


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
class TargetRead:
    """One linked target config as a push read it: its live row ids, or why it could not be read."""

    rows: set[str] | None
    error_code: ErrorCode | None = None
    message: str = ""
    # The target answered 404: it is known NOT to exist (a failed read proves nothing).
    gone: bool = False


@dataclass
class TargetCache:
    """The target reads of one push, shared by Phase C and Phase D (each target read once)."""

    reads: dict[tuple[str, str], TargetRead] = field(default_factory=dict)
    reported: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class VariableBindingResult:
    """Outcome of the Phase-C transformation-link backfill (variables + shared code).

    ``configs_rewritten`` counts the rewrites (one per variables or shared-code
    PUT) whose remote configuration + local ``_configuration_extra`` were
    rebound to ULIDs (drives the manifest-dirty flag). ``shared_code_links``
    counts the transformations whose shared-code link was re-pointed.
    ``errors`` accumulates unresolved links and failed PUTs so the push
    envelope surfaces them instead of leaving a broken link silently.
    ``warnings`` carries non-fatal baseline-stamping notices (issue #686).
    """

    # The ``link_pending`` flag of a transformation changed (Devin 4400588270).
    pending_changed: bool = False
    # The push's target reads, handed on to Phase D (#929 D32).
    targets: TargetCache = field(default_factory=TargetCache)
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    configs_rewritten: int = 0
    shared_code_links: int = 0


@dataclass
class FlowBindingResult:
    """Outcome of the Phase-D backfill (#426, CLI-24).

    Phase D remaps the configs that flows, legacy orchestrations and schedules
    run. ``configs_rewritten`` counts the configs whose task / target ids were
    remapped to ULIDs (drives the manifest-dirty flag). The other counters are
    the references rewritten per kind: flow task ``configId``s, orchestrator
    task ``configId``s, schedule targets, and task ``configRowIds`` entries.
    ``errors`` accumulates unmappable row ids and failed PUTs so the push
    envelope surfaces them. ``warnings`` carries non-fatal baseline-stamping
    notices (issue #686).
    """

    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    configs_rewritten: int = 0
    flow_tasks: int = 0
    orchestrator_tasks: int = 0
    schedule_targets: int = 0
    config_row_ids: int = 0
    # ``link_pending`` was set or cleared on an entry (D9): the manifest changed.
    pending_changed: bool = False

    def push_fields(self, shared_code_links: int) -> dict[str, Any]:
        """Return the push-result keys for the link remaps of Phase C and D.

        ``flow_task_remaps`` keeps its meaning from before CLI-24 (flow task
        ``configId``s only). ``link_remaps`` has one count per kind. Both are
        left out when nothing was remapped.
        """
        link_remaps = {
            "flow_tasks": self.flow_tasks,
            "orchestrator_tasks": self.orchestrator_tasks,
            "schedule_targets": self.schedule_targets,
            "shared_code": shared_code_links,
            "config_row_ids": self.config_row_ids,
        }
        fields: dict[str, Any] = {}
        if self.flow_tasks:
            fields["flow_task_remaps"] = self.flow_tasks
        if any(link_remaps.values()):
            fields["link_remaps"] = link_remaps
        return fields


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
