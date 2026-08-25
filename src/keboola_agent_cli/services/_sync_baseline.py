"""API-derived manifest baselines and legacy-hash compatibility (issue #686).

``pull_config_hash`` is the 3-way diff's base: the normalized hash of the
config *as the API returns it*. ``sync pull`` and the remote side of
``sync diff`` always computed it that way; ``sync push`` instead recomputed it
from the files on disk, so any config whose local<->API round-trip is not
hash-stable was reported ``~ REMOTE MODIFIED`` by every subsequent diff --
forever, with the working tree byte-identical to the remote.

This module holds the one producer push now uses (:func:`config_baseline` /
:func:`row_baseline`) plus the migration helpers that keep manifests written by
a pre-#686 kbagent readable:

- :func:`effective_stored_hash` -- lenient comparison for entries without
  ``metadata.config_hash_version``.
- :func:`raise_on_legacy_boundary` -- refuses to push a legacy tree whose
  ``transform.sql`` cannot represent the remote's statement boundaries, which
  would silently collapse several statements into one.

:func:`detect_force_pull_conflicts` lives here for the same reason: it is the
third place a stored baseline is weighed against a fresh API hash, and it must
apply the same leniency or a ``--force`` pull would abort on a shape-only
difference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import (
    ALWAYS_IGNORED_COMPONENTS,
    CONFIG_FILENAME,
    CONFIG_HASH_VERSION,
    CONFIG_HASH_VERSION_KEY,
)
from ..errors import ErrorCode, KeboolaApiError
from ..sync.code_extraction import (
    is_sql_transformation_component,
    marker_less_roundtrip,
    merge_code_files,
)
from ..sync.config_format import api_config_to_local, api_row_to_local
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


@dataclass
class BaselineStamp:
    """The ``pull_config_hash`` a push should record, or the reason it cannot.

    ``stamped=False`` means the API state could not be established (the
    mutation response was partial AND the follow-up detail fetch failed). The
    caller must then leave the previous ``pull_config_hash`` untouched -- never
    fall back to a hash computed from disk, which is the very asymmetry #686
    is about, and which would read a response missing ``isDisabled`` as
    "enabled".
    """

    cfg_hash: str = ""
    stamped: bool = False
    warning: dict[str, str] | None = None

    @property
    def version(self) -> int | None:
        """The hash-shape version to store, or ``None`` when nothing is stamped.

        The version is only ever written next to a hash actually computed with
        the current producer -- never next to a preserved legacy hash.
        """
        return CONFIG_HASH_VERSION if self.stamped else None


def _usable_payload(response: Any) -> dict[str, Any] | None:
    """Return *response* when it is a full configuration/row object.

    The Storage API makes no ``include`` promise on PUT/POST, and mocked
    clients in tests routinely answer ``{"id": "..."}``. A payload counts as
    full only when it carries a ``configuration`` key -- an id-only response
    would hash as an empty, enabled config and stamp a baseline that never
    matches. The value may be a list: PHP serializes an empty configuration as
    ``[]`` and ``api_config_to_local`` already normalizes that to ``{}``.
    """
    if not isinstance(response, dict):
        return None
    if not isinstance(response.get("configuration"), (dict, list)):
        return None
    return response


def _fetch_warning(component_id: str, config_id: str, exc: Exception) -> dict[str, str]:
    """Build the push-envelope warning for an unstampable baseline."""
    message = (
        f"Could not read back {component_id}/{config_id} after the write, so the "
        f"manifest baseline was left unchanged; run 'kbagent sync pull' to refresh "
        f"it. Cause: {exc}"
    )
    logger.warning("%s", message)
    return {
        "change_type": "baseline_stamp",
        "component_id": component_id,
        "config_id": config_id,
        "message": message,
    }


def config_baseline(
    client: Any,
    *,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    response: Any,
) -> BaselineStamp:
    """Compute the post-write baseline hash for a configuration.

    Prefers the mutation response; falls back to ``get_config_detail`` when it
    is partial. A failed fetch yields an unstamped result plus a warning.
    """
    payload = _usable_payload(response)
    if payload is None:
        try:
            payload = _usable_payload(
                client.get_config_detail(
                    component_id=component_id,
                    config_id=config_id,
                    branch_id=branch_id,
                )
            )
        except Exception as exc:
            return BaselineStamp(warning=_fetch_warning(component_id, config_id, exc))
        if payload is None:
            return BaselineStamp(
                warning=_fetch_warning(
                    component_id, config_id, ValueError("configuration missing from the response")
                )
            )
    return BaselineStamp(
        cfg_hash=config_hash(api_config_to_local(component_id, payload, config_id)),
        stamped=True,
    )


def row_baseline(
    client: Any,
    *,
    component_id: str,
    config_id: str,
    row_id: str,
    branch_id: int | None,
    response: Any,
) -> BaselineStamp:
    """Compute the post-write baseline hash for a configuration row.

    Same contract as :func:`config_baseline`, via ``get_config_row``. Row
    hashes are NOT script-normalized (``api_row_to_local`` never was), so no
    shape migration applies to them -- but a partial response would still drop
    ``isDisabled`` and strand a permanent phantom diff, which is why rows take
    the identical read-back path.
    """
    payload = _usable_payload(response)
    if payload is None:
        try:
            payload = _usable_payload(
                client.get_config_row(
                    component_id=component_id,
                    config_id=config_id,
                    row_id=row_id,
                    branch_id=branch_id,
                )
            )
        except Exception as exc:
            return BaselineStamp(warning=_fetch_warning(component_id, f"{config_id}/{row_id}", exc))
        if payload is None:
            return BaselineStamp(
                warning=_fetch_warning(
                    component_id,
                    f"{config_id}/{row_id}",
                    ValueError("configuration missing from the response"),
                )
            )
    return BaselineStamp(
        cfg_hash=config_hash(api_row_to_local(payload, component_id)),
        stamped=True,
    )


def apply_stamp(metadata: dict[str, Any], stamp: BaselineStamp) -> None:
    """Record a baseline on a manifest entry's metadata dict.

    A no-op when the stamp failed: the previous ``pull_config_hash`` (and its
    version marker, if any) survives untouched, so the state stays *visibly*
    stale rather than confidently wrong.
    """
    if not stamp.stamped:
        return
    metadata["pull_config_hash"] = stamp.cfg_hash
    metadata[CONFIG_HASH_VERSION_KEY] = CONFIG_HASH_VERSION


# ---------------------------------------------------------------------------
# Migration: manifests written before the shape change
# ---------------------------------------------------------------------------


def is_legacy_hash(
    stored: str,
    *,
    component_id: str,
    config_id: str,
    raw_remote: dict[str, Any],
) -> bool:
    """True iff *stored* is the pre-#686 hash of this very remote config.

    Computed from the RAW API config through the old collapse normalization --
    never by re-collapsing already-split data, which would not reproduce the
    same bytes. A match proves the entry differs from the current producer in
    the script shape ALONE: every other field is pinned by the same hash, so
    the leniency cannot mask real drift.
    """
    if not stored:
        return False
    legacy = api_config_to_local(component_id, raw_remote, config_id, legacy_scripts=True)
    return stored == config_hash(legacy)


def effective_stored_hash(
    metadata: dict[str, Any],
    *,
    component_id: str,
    config_id: str,
    raw_remote: dict[str, Any] | None,
    remote_local: dict[str, Any] | None,
) -> str:
    """Return the entry's baseline hash, upgraded when it is legacy-shaped.

    Versioned entries (``config_hash_version`` present) are compared strictly.
    An unversioned entry whose stored hash is the legacy-shape hash of the
    CURRENT remote is treated as if it held the new-shape hash -- that is the
    whole migration: one ``sync pull`` re-stamps it and the leniency stops
    applying. Anything else (a real remote edit, a real local edit) is
    returned untouched, so the leniency cannot mask drift.
    """
    stored = str(metadata.get("pull_config_hash", "") or "")
    if not stored or metadata.get(CONFIG_HASH_VERSION_KEY):
        return stored
    if raw_remote is None or remote_local is None:
        return stored
    remote_hash = config_hash(remote_local)
    if stored == remote_hash:
        return stored
    if is_legacy_hash(
        stored, component_id=component_id, config_id=config_id, raw_remote=raw_remote
    ):
        return remote_hash
    return stored


def needs_shape_migration(
    metadata: dict[str, Any],
    *,
    component_id: str,
    config_id: str,
    raw_remote: dict[str, Any],
    api_cfg_hash: str,
) -> bool:
    """True iff this entry's baseline is a pre-#686 hash of the same remote.

    The pull-side counterpart of :func:`effective_stored_hash`: the remote is
    unchanged, only the recorded shape is old, so the pull must re-run
    extraction (to write the boundary markers) and re-stamp -- unless the
    local files were edited, in which case they are preserved untouched.
    """
    stored = str(metadata.get("pull_config_hash", "") or "")
    if not stored or metadata.get(CONFIG_HASH_VERSION_KEY) or stored == api_cfg_hash:
        return False
    return is_legacy_hash(
        stored, component_id=component_id, config_id=config_id, raw_remote=raw_remote
    )


def extras_modified(service: SyncService, config_dir: Path, extra_hashes: dict[str, str]) -> bool:
    """True iff any companion file recorded at pull time changed on disk.

    The pull overwrite-guard has only ever compared ``_config.yml``, so an
    edited ``transform.sql`` beside an untouched ``_config.yml`` was
    overwritten. That is pre-existing behaviour everywhere EXCEPT the shape
    migration, which rewrites code files for a remote that did not change --
    there, silently discarding a local edit would be new damage, so the
    migration checks the companions too.
    """
    for fname, stored_hash in (extra_hashes or {}).items():
        fpath = config_dir / fname
        if not fpath.exists() or service._file_hash(fpath) != stored_hash:
            return True
    return False


def _scripts_by_code(config_data: dict[str, Any]) -> list[list[Any]] | None:
    """Collect every ``blocks[].codes[].script`` array, in document order."""
    parameters = config_data.get("parameters")
    if not isinstance(parameters, dict):
        return None
    blocks = parameters.get("blocks")
    if not isinstance(blocks, list):
        return None
    scripts: list[list[Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            return None
        for code in block.get("codes") or []:
            if not isinstance(code, dict):
                return None
            script = code.get("script")
            scripts.append(list(script) if isinstance(script, list) else [])
    return scripts


def _boundary_only_difference(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    """True iff local is exactly the marker-less rendering of the remote.

    Any other difference -- a real SQL edit, a reindent, a different block
    layout -- is a genuine local change and must push normally.
    """
    local_scripts = _scripts_by_code(local)
    remote_scripts = _scripts_by_code(remote)
    if local_scripts is None or remote_scripts is None:
        return False
    if len(local_scripts) != len(remote_scripts):
        return False
    found = False
    for local_script, remote_script in zip(local_scripts, remote_scripts, strict=True):
        if local_script == remote_script:
            continue
        if marker_less_roundtrip(remote_script) != local_script:
            return False  # a genuine edit -- let it push
        found = True
    return found


def raise_on_legacy_boundary(
    service: SyncService,
    client: Any,
    *,
    component_id: str,
    config_id: str,
    config_dir: Path,
    manifest: Manifest,
    branch_id: int | None,
) -> None:
    """Refuse a push that would collapse the remote's statement boundaries.

    A tree pulled before #686 has a ``transform.sql`` with no boundary markers.
    When the remote's ``script[]`` elements carry no trailing semicolons, that
    file cannot express where one statement ends -- so ``merge_code_files``
    returns ONE element holding everything and the push silently rewrites
    production into the ``MULTI_STATEMENT_COUNT=1`` crash shape of issues
    #119/#120/#274, while ``sync diff`` reports "in sync".

    Scope is deliberately narrow: SQL transformations only, only entries with
    no ``config_hash_version`` (a pulled-since-#686 tree carries markers and
    cannot hit this), and only when the statement TEXT is identical while the
    element counts differ. A genuine edit proceeds. A failed remote read
    proceeds too -- this is a safety net, not a gate.

    Raises:
        KeboolaApiError: with :data:`ErrorCode.SYNC_LEGACY_BOUNDARY`, caught by
            the push loop and accumulated as a per-change error.
    """
    if not is_sql_transformation_component(component_id):
        return
    entry = next(
        (
            c
            for c in manifest.configurations
            if c.component_id == component_id and c.id == config_id
        ),
        None,
    )
    if entry is None or entry.metadata.get(CONFIG_HASH_VERSION_KEY):
        return
    local_data = service._read_config_file(config_dir)
    if local_data is None:
        return
    try:
        remote_raw = client.get_config_detail(
            component_id=component_id, config_id=config_id, branch_id=branch_id
        )
    except Exception:
        logger.debug("Legacy boundary guard skipped: %s/%s unreadable", component_id, config_id)
        return
    if _usable_payload(remote_raw) is None:
        return
    merge_code_files(component_id, local_data, config_dir)
    if not _boundary_only_difference(
        local_data, api_config_to_local(component_id, remote_raw, config_id)
    ):
        return
    raise KeboolaApiError(
        message=(
            f"Refusing to push {component_id}/{config_id}: this working tree predates "
            f"statement-boundary tracking, so pushing it would merge separate SQL "
            f"statements into one (the MULTI_STATEMENT_COUNT=1 failure). The content "
            f"is otherwise identical to the remote. Run 'kbagent sync pull' for this "
            f"project first, then push again."
        ),
        status_code=0,
        error_code=ErrorCode.SYNC_LEGACY_BOUNDARY,
    )


# ---------------------------------------------------------------------------
# Force-pull conflict detection
# ---------------------------------------------------------------------------


def _is_conflict(
    service: SyncService,
    config_file: Path,
    old_pull_hash: str,
    old_cfg_hash: str,
    api_cfg_hash: str,
) -> bool:
    """True iff the file is locally modified AND the remote also changed.

    A 3-way conflict needs both a stored ``pull_hash`` (the synced file
    state) and a stored ``pull_config_hash`` (the synced remote state);
    without either we cannot prove a conflict, so return False -- be
    conservative, ``--force`` must not abort on incomplete bookkeeping.
    A missing local file is not a content conflict (nothing to lose).
    """
    if not old_pull_hash or not old_cfg_hash:
        return False
    if not config_file.exists():
        return False
    locally_modified = service._file_hash(config_file) != old_pull_hash
    remote_changed = api_cfg_hash != old_cfg_hash
    return locally_modified and remote_changed


def detect_force_pull_conflicts(
    service: SyncService,
    components: list[dict[str, Any]],
    branch_dir: Path,
    *,
    existing_keys: set[str],
    existing_paths: dict[str, str],
    existing_file_hashes: dict[str, str],
    existing_metadata: dict[str, dict[str, Any]],
    existing_rows: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    """Return configs/rows a ``--force`` pull would clobber as conflicts.

    A *conflict* is a config (or row) that is BOTH locally modified (its
    on-disk ``_config.yml`` hash differs from the manifest ``pull_hash``)
    AND changed on the remote since the last pull (the freshly fetched
    config hash differs from ``pull_config_hash``).  That is the only case
    where ``--force`` must stop: local and remote have diverged, so neither
    "take remote" nor "keep local" is safe without the user deciding.

    Configs only locally modified (remote unchanged) are NOT conflicts --
    ``--force`` preserves them so their pending delta stays pushable.
    Brand-new remote configs and configs whose local file is missing are
    skipped (nothing local to lose).  Read-only: hashes but writes nothing.

    The stored config hash goes through :func:`effective_stored_hash`, so a
    baseline written before the script-shape change (issue #686) is not
    mistaken for a remote edit and does not abort an otherwise clean
    ``--force`` pull. Rows are compared strictly -- their hash producer never
    changed.
    """
    conflicts: list[dict[str, str]] = []
    for component in components:
        component_id = component.get("id", "")
        if component_id in ALWAYS_IGNORED_COMPONENTS:
            continue
        for cfg in component.get("configurations", []):
            config_id = str(cfg.get("id", ""))
            lookup_key = f"{component_id}/{config_id}"
            if lookup_key not in existing_keys:
                continue  # brand-new remote config -- nothing local to lose

            rel_path = existing_paths.get(lookup_key, "")
            remote_local = api_config_to_local(component_id, cfg, config_id)
            if _is_conflict(
                service,
                branch_dir / rel_path / CONFIG_FILENAME,
                existing_file_hashes.get(lookup_key, ""),
                effective_stored_hash(
                    existing_metadata.get(lookup_key, {}),
                    component_id=component_id,
                    config_id=config_id,
                    raw_remote=cfg,
                    remote_local=remote_local,
                ),
                config_hash(remote_local),
            ):
                conflicts.append(
                    {
                        "scope": "config",
                        "component_id": component_id,
                        "config_id": config_id,
                        "config_name": str(cfg.get("name", "untitled")),
                        "path": rel_path,
                    }
                )

            # Row-level conflicts (same 3-way rule, per row).
            config_dir = branch_dir / rel_path
            for row in cfg.get("rows", []):
                row_id = str(row.get("id", ""))
                existing_row = existing_rows.get(f"{component_id}/{config_id}/{row_id}")
                if not existing_row:
                    continue
                row_rel_path = existing_row.get("path", "")
                if _is_conflict(
                    service,
                    config_dir / row_rel_path / CONFIG_FILENAME,
                    existing_row.get("pull_hash", ""),
                    existing_row.get("pull_config_hash", ""),
                    config_hash(api_row_to_local(row, component_id)),
                ):
                    conflicts.append(
                        {
                            "scope": "row",
                            "component_id": component_id,
                            "config_id": config_id,
                            "config_name": (
                                f"{cfg.get('name', 'untitled')}/{row.get('name', 'untitled')}"
                            ),
                            "path": f"{rel_path}/{row_rel_path}",
                            "row_id": row_id,
                        }
                    )
    return conflicts
