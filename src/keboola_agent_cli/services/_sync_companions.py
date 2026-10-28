"""The companion files of a config: baseline, comparison and pull reconciliation.

A config's local representation is ``_config.yml`` plus the companion files its
component extracts (``_description.md``, ``transform.sql``, ``transform.py``,
``code.py``, ``pyproject.toml`` -- :func:`owned_companions` says which). Three
bugs came from treating that set as "whatever files pull happened to write":

- a companion created after the pull (a new ``_description.md`` for a config
  pulled with an empty description) was invisible to diff and never pushed;
- a companion the remote no longer has (a cleared description or package list)
  stayed on disk, recorded as clean, and the next push of any edit merged it
  back and restored the cleared value on the remote;
- a companion of another component (a user's ``pyproject.toml`` next to an
  extractor) was hashed as if it were part of the config.

The manifest baseline therefore covers the companion SET, not only the files
present at pull time. ``pull_extra_hashes`` keeps its legacy shape -- the
present files and their hashes -- and ``pull_extra_hashes_version: 1`` says the
coverage is every companion the component owns: an owned companion with no key
is KNOWN ABSENT. Absence is never written as an explicit ``null``: an older
kbagent iterates the map and reads every key whose file is missing as a local
edit, so it would see nearly every config as locally modified and its pull
would stop updating them.

Migration (D3): an entry without the version key but with ``pull_hash`` and a
``pull_extra_hashes`` map is a snapshot of the five filenames the old code
scanned, so a missing key for one of those is also known absent. An entry
without ``pull_hash`` or without the map (a create checkpoint, an adopted
entry) has an UNKNOWN companion baseline: only the recorded keys are compared,
and nothing it lacks may ever be deleted as stale.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME, CONFIG_HASH_VERSION, CONFIG_HASH_VERSION_KEY
from ..sync.code_extraction import DESCRIPTION_FILENAME, owned_companions, render_code_files

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

COMPANION_VERSION_KEY = "pull_extra_hashes_version"

# The filenames every kbagent before the companion registry hashed when found
# on disk: a legacy map is a complete snapshot of these, and of nothing else.
LEGACY_COMPANIONS: frozenset[str] = frozenset(
    {DESCRIPTION_FILENAME, "transform.sql", "transform.py", "code.py", "pyproject.toml"}
)

# What each version of the marker certifies, FROZEN: version 1 covers exactly
# the five legacy names. An older kbagent that rewrites the map keeps the
# marker it found, and it scanned those five names only -- so a companion
# filename the registry gains later must come with a NEW version (and its own
# frozen set here). Otherwise a retained ``1`` would certify absence of a
# file the old writer never looked for.
COMPANION_VERSION = 1
COMPANION_COVERAGE: dict[int, frozenset[str]] = {1: LEGACY_COMPANIONS}

# Config metadata: obsolete companions a pull kept because it could not prove
# them unchanged (unknown baseline, or edited while pull ran). While such a
# file exists the config diffs as a conflict, never as an edit to push: the
# remote cleared that value (R4). Cleared once the file is gone or a pull
# reconciles it (``--theirs``).
COMPANION_CONFLICT_KEY = "companion_conflict"

# ``code`` of the companion entries in a command's ``warnings[]``.
UNOWNED_COMPANION_CODE = "UNOWNED_COMPANION"
COMPANION_KEPT_CODE = "COMPANION_KEPT"


@dataclass(frozen=True)
class CompanionBaseline:
    """What the manifest says about a config's companion files.

    Attributes:
        known: The recorded map is a complete snapshot of ``coverage``: an
            owned companion without a key was absent. When False, only the
            recorded keys say anything.
        coverage: The companion files the snapshot speaks for.
        hashes: Recorded hash per covered companion that was present.
    """

    known: bool
    coverage: frozenset[str]
    hashes: dict[str, str] = field(default_factory=dict)


def companion_baseline(component_id: str, metadata: dict[str, Any]) -> CompanionBaseline:
    """Read the companion baseline of one manifest entry (versioned or legacy)."""
    owned = frozenset(owned_companions(component_id))
    recorded = metadata.get("pull_extra_hashes")
    # A snapshot never speaks for a companion type its writer did not scan:
    # a legacy map for the five legacy names, a versioned one for its frozen set.
    coverage = owned & COMPANION_COVERAGE.get(
        metadata.get(COMPANION_VERSION_KEY) or 0, LEGACY_COMPANIONS
    )
    known = bool(metadata.get("pull_hash")) and isinstance(recorded, dict)
    hashes = {
        str(name): str(value)
        for name, value in (recorded if isinstance(recorded, dict) else {}).items()
        if name in coverage and value
    }
    return CompanionBaseline(known=known, coverage=coverage, hashes=hashes)


def current_companions(service: SyncService, config_dir: Path, component_id: str) -> dict[str, str]:
    """Hash of every owned companion file present in *config_dir* now."""
    return {
        name: service._file_hash(config_dir / name)
        for name in owned_companions(component_id)
        if (config_dir / name).is_file()
    }


def record_companions(metadata: dict[str, Any], hashes: dict[str, str]) -> None:
    """Stamp a complete companion snapshot (present files; the rest known absent).

    Also ends a kept-companion conflict: the snapshot is now what is on disk.
    """
    metadata["pull_extra_hashes"] = dict(hashes)
    metadata[COMPANION_VERSION_KEY] = COMPANION_VERSION
    metadata.pop(COMPANION_CONFLICT_KEY, None)


def companion_conflict(config_dir: Path, metadata: dict[str, Any]) -> bool:
    """True while a companion a pull kept as obsolete is still on disk (R4)."""
    kept = metadata.get(COMPANION_CONFLICT_KEY)
    return isinstance(kept, list) and any((config_dir / str(name)).is_file() for name in kept)


def _changed_companions(
    base: CompanionBaseline, current: dict[str, str], *, edits_only: bool = False
) -> list[str]:
    """Covered companions whose presence or hash differs from the baseline.

    *edits_only* counts only files that exist now (a surviving edit); a
    recorded file that is gone is then not counted.
    """
    if base.known:
        names = set(base.coverage) if not edits_only else set(current)
        return sorted(
            name
            for name in names & base.coverage
            if current.get(name) != base.hashes.get(name) and (name in current or not edits_only)
        )
    # Unknown baseline: only what was recorded can be judged.
    return sorted(
        name
        for name, value in base.hashes.items()
        if current.get(name) != value and (name in current or not edits_only)
    )


def companions_modified(
    service: SyncService, config_dir: Path, component_id: str, metadata: dict[str, Any]
) -> bool:
    """True iff a covered companion appeared, vanished or changed since the snapshot."""
    base = companion_baseline(component_id, metadata)
    return bool(_changed_companions(base, current_companions(service, config_dir, component_id)))


def local_unchanged(
    service: SyncService, config_dir: Path, component_id: str, metadata: dict[str, Any]
) -> bool:
    """True iff ``_config.yml`` byte-matches ``pull_hash`` and no companion changed.

    The shortcut ``sync diff`` / ``sync status`` / ``pull --theirs`` take to
    reuse the stored API hash instead of a lossy code-merge round trip.
    """
    pull_hash = metadata.get("pull_hash", "")
    config_file = config_dir / CONFIG_FILENAME
    if not pull_hash or not config_file.exists():
        return False
    if service._file_hash(config_file) != pull_hash:
        return False
    return not companions_modified(service, config_dir, component_id, metadata)


def config_locally_modified(
    service: SyncService, config_dir: Path, component_id: str, metadata: dict[str, Any]
) -> bool:
    """True iff the config's local representation differs from the pull state.

    Covers ``_config.yml`` AND the companion set (issue #792 B, D2): a new,
    deleted or edited companion is a local modification. Without a recorded
    ``pull_hash`` nothing can be proven (False). A missing ``_config.yml`` is
    a gone directory that pull re-materializes (#472) -- unless edited
    companions survive it (D6): re-materializing would overwrite them.
    """
    if not metadata.get("pull_hash"):
        return False
    config_file = config_dir / CONFIG_FILENAME
    current = current_companions(service, config_dir, component_id)
    base = companion_baseline(component_id, metadata)
    if not config_file.exists():
        return bool(_changed_companions(base, current, edits_only=True))
    if service._file_hash(config_file) != metadata["pull_hash"]:
        return True
    return bool(_changed_companions(base, current))


# ---------------------------------------------------------------------------
# Pull: write the whole representation, remove obsolete owned companions
# ---------------------------------------------------------------------------


@dataclass
class PulledConfig:
    """What pull wrote for one config.

    Attributes:
        file_hash: Hash of ``_config.yml`` now (``""`` on a dry run).
        written: The representation was (or, on a dry run, would be) written.
        extra_hashes: The companion snapshot to record. Leaves out a kept
            obsolete companion, so diff shows it as a local change.
        kept: Obsolete companions pull did not delete (not provably unchanged).
    """

    file_hash: str
    written: bool
    extra_hashes: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)


def _cleanup_plan(
    service: SyncService,
    config_dir: Path,
    component_id: str,
    metadata: dict[str, Any],
    *,
    theirs: bool,
) -> dict[str, str]:
    """Companions pull may delete if obsolete, with the hash each must still have.

    ``--theirs`` takes every owned companion as it is now (remote wins, D5).
    Otherwise only a companion a KNOWN baseline records with exactly its
    current hash: an unknown baseline never authorizes a delete (risk 4).
    """
    current = current_companions(service, config_dir, component_id)
    if theirs:
        return current
    base = companion_baseline(component_id, metadata)
    if not base.known:
        return {}
    return {name: value for name, value in current.items() if base.hashes.get(name) == value}


def _representation_matches(
    service: SyncService, config_dir: Path, component_id: str, local_data: dict[str, Any]
) -> bool:
    """True iff the owned companions on disk are exactly the ones the remote implies."""
    desired = render_code_files(component_id, copy.deepcopy(local_data))
    return set(current_companions(service, config_dir, component_id)) == set(desired)


def write_pulled_config(
    service: SyncService,
    config_dir: Path,
    component_id: str,
    local_data: dict[str, Any],
    metadata: dict[str, Any],
    *,
    remote_unchanged: bool,
    theirs: bool,
    dry_run: bool,
) -> PulledConfig:
    """Write a config's representation from the remote, or keep it (D4, D5).

    Called only for a config that is not locally modified (or under
    ``--theirs``). *remote_unchanged* says the remote matches the stored base;
    the write is skipped only when the files on disk also match that state AND
    hold exactly the companions the remote implies. The second condition
    repairs a tree an older pull left a stale companion in, recorded as clean
    (risk 1). Each obsolete companion is hashed again right before it is
    deleted: a file edited in between is kept and reported (risk 11).
    """
    config_file = config_dir / CONFIG_FILENAME
    if remote_unchanged and theirs:
        remote_unchanged = local_unchanged(service, config_dir, component_id, metadata)
    if remote_unchanged and not _representation_matches(
        service, config_dir, component_id, local_data
    ):
        remote_unchanged = False
    if remote_unchanged:
        return PulledConfig(
            file_hash=service._file_hash(config_file),
            written=False,
            extra_hashes=current_companions(service, config_dir, component_id),
        )
    if dry_run:
        return PulledConfig(file_hash="", written=True)
    plan = _cleanup_plan(service, config_dir, component_id, metadata, theirs=theirs)
    files = render_code_files(component_id, local_data)
    for name, content in files.items():
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / name).write_text(content, encoding="utf-8")
    file_hash = service._write_config_file(config_dir, local_data)
    kept: list[str] = []
    for name in owned_companions(component_id):
        path = config_dir / name
        if name in files or not path.is_file():
            continue
        if name in plan and service._file_hash(path) == plan[name]:
            path.unlink()
        else:
            kept.append(name)
    current = current_companions(service, config_dir, component_id)
    return PulledConfig(
        file_hash=file_hash,
        written=True,
        extra_hashes={name: value for name, value in current.items() if name not in kept},
        kept=kept,
    )


def preserved_baseline(old_metadata: dict[str, Any]) -> dict[str, Any]:
    """The baseline a pull records for a config it skipped as locally modified.

    The old one, verbatim -- ``pull_hash``, ``pull_config_hash``, the companion
    snapshot, the version markers and a kept-companion conflict: advancing any
    of them would turn a real conflict into an ordinary-looking local edit
    (issue #792 B). The preserved hash was not produced by the current
    producer, so its version marker is carried, never stamped (issue #686).
    """
    keys = (
        "pull_hash",
        "pull_config_hash",
        "pull_extra_hashes",
        COMPANION_VERSION_KEY,
        COMPANION_CONFLICT_KEY,
        CONFIG_HASH_VERSION_KEY,
    )
    return {key: old_metadata[key] for key in keys if key in old_metadata}


def pulled_baseline(file_hash: str, cfg_hash: str, pulled: PulledConfig) -> dict[str, Any]:
    """The baseline a pull records for a config it wrote (or found in sync).

    Freshly computed with the current producer, so the shape version is
    stamped (issue #686). A companion pull had to keep makes it a conflict
    until the file is gone (R4).
    """
    metadata: dict[str, Any] = {
        "pull_hash": file_hash,
        "pull_config_hash": cfg_hash,
        CONFIG_HASH_VERSION_KEY: CONFIG_HASH_VERSION,
    }
    record_companions(metadata, pulled.extra_hashes)
    if pulled.kept:
        metadata[COMPANION_CONFLICT_KEY] = list(pulled.kept)
    return metadata


def kept_companion_warning(component_id: str, rel_path: str, name: str) -> dict[str, str]:
    """``companion_warnings`` entry for an obsolete companion pull did not delete."""
    message = (
        f"{rel_path}/{name}: the remote no longer has this value, but pull could not prove "
        f"the file unchanged since the last sync, so it kept it. Until the file is gone, "
        f"`kbagent sync diff` lists the config as a conflict and push leaves it alone: delete "
        f"the file to accept the remote, or run `kbagent sync pull --theirs`."
    )
    logger.warning("%s", message)
    return {
        "code": COMPANION_KEPT_CODE,
        "component_id": component_id,
        "path": f"{rel_path}/{name}",
        "message": message,
    }


# ---------------------------------------------------------------------------
# Diff diagnostics (D11) and the description layout (D10)
# ---------------------------------------------------------------------------


def unowned_companion_warnings(
    config_dir: Path, component_id: str, rel_path: str
) -> list[dict[str, str]]:
    """Diff ``companion_warnings`` for a companion filename this component does not use.

    A ``code.py`` beside an extractor, or a ``transform.py`` renamed to
    ``code.py``, is ignored by push: merge dispatches by component, so the
    file is never sent. Said out loud instead of silently.
    """
    owned = set(owned_companions(component_id))
    return [
        {
            "code": UNOWNED_COMPANION_CODE,
            "component_id": component_id,
            "path": f"{rel_path}/{name}",
            "message": (
                f"{rel_path}/{name} is not part of a {component_id} configuration, so sync "
                f"never pushes, compares or deletes it. Its files are: {', '.join(sorted(owned))}."
            ),
        }
        for name in sorted(LEGACY_COMPANIONS - owned)
        if (config_dir / name).is_file()
    ]


def canonical_description(local_data: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """The ``_config.yml`` layout a fresh pull writes for an empty description (L2).

    Without ``_description.md`` an empty description is spelled inline as
    ``description: ''`` right after ``name`` -- where pull puts it. A
    non-empty inline description is left as it is.
    """
    if (config_dir / DESCRIPTION_FILENAME).exists() or local_data.get("description"):
        return local_data
    out: dict[str, Any] = {}
    anchor = "name" if "name" in local_data else "version"
    for key, value in local_data.items():
        if key == "description":
            continue
        out[key] = value
        if key == anchor:
            out["description"] = ""
    if "description" not in out:
        out = {"description": "", **out}
    return out
