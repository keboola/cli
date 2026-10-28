"""Manifest + local-file writeback after a push (extracted from sync_service.py).

Free functions that record a freshly-created config/row in the manifest, write
the API-assigned ULID + encrypted secrets back into the local ``_config.yml``,
and propagate ``KBC.*`` metadata. Only :func:`writeback_after_push` needs the
``SyncService`` (for its ``_write_config_file`` helper); the rest are pure.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import FOLDER_METADATA_KEY
from ..errors import KeboolaApiError
from ..sync.branch_scope import (
    PROMOTED_FROM_KEY,
    UNKNOWN_BASE_KEY,
    UNKNOWN_BASE_UNREAD,
    PromotedFrom,
    branch_tree_path,
    find_branch_entry,
    promote_source,
    promoted_from,
    same_branch,
)
from ..sync.config_format import BASE_CONFIG_HASH_KEY
from ..sync.manifest import ManifestConfigRow, ManifestConfiguration
from ._encryption import apply_encrypted_to_local
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_companions import canonical_description, record_companions
from ._sync_models import AUTHORED_ROW_KEY, PROMOTED_ROW_KEY, LocalConfigHashes, WritebackResult
from ._sync_untracked import adopted_row_entries

if TYPE_CHECKING:
    from ..sync.manifest import Manifest
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


def stamp_created_config(
    client: Any,
    *,
    manifest: Manifest,
    component_id: str,
    branch_id: int | None,
    config_path_str: str,
    new_id: str,
    hashes: LocalConfigHashes,
    response: Any,
    warnings: list[dict[str, str]],
    promoted: PromotedFrom | None = None,
) -> WritebackResult:
    """Record a created config with an API-derived ``pull_config_hash`` (#686).

    ``pull_hash`` describes the local file and stays disk-derived; the config
    hash is the API's own view of what was just written, so the next
    ``sync diff`` compares like with like. *promoted* is set on the promote
    path (D13): the new entry then records which source-tree directory and
    config it stands for, since its files are not in its own tree.
    """
    stamp = config_baseline(
        client,
        component_id=component_id,
        config_id=new_id,
        branch_id=branch_id,
        response=response,
    )
    if stamp.warning is not None:
        warnings.append(stamp.warning)
    writeback = writeback_create_config_in_manifest(
        manifest=manifest,
        component_id=component_id,
        branch_id=branch_id,
        config_path_str=config_path_str,
        new_id=new_id,
        file_hash=hashes.file_hash,
        cfg_hash=stamp.cfg_hash,
        extra_hashes=hashes.extra_hashes,
    )
    apply_stamp(writeback.entry.metadata, stamp)
    record_companions(writeback.entry.metadata, hashes.extra_hashes)
    if promoted is not None:
        writeback.entry.metadata[PROMOTED_FROM_KEY] = promoted.as_metadata()
    return writeback


def link_promote_entries(
    manifest: Manifest, *, tree: str, component_id: str, path: str, config_id: str
) -> None:
    """Point promote entries of an authored *path* at the config just created for it.

    A config authored in the source tree and promoted before it existed there
    records ``config_id=""`` (D19). Once a push creates it on the source
    tree's own branch, its association names that config, so a later rename
    of the directory by a pull moves the promote entry with it (D13a).
    """
    for cfg in manifest.configurations:
        association = promoted_from(cfg)
        if (
            association is not None
            and association.tree == tree
            and not association.config_id
            and cfg.component_id == component_id
            and cfg.path == path
        ):
            cfg.metadata[PROMOTED_FROM_KEY] = PromotedFrom(tree, config_id).as_metadata()


def reconcile_promote_links(manifest: Manifest) -> bool:
    """Link every promote entry to the source config and rows it stands for (risk 76).

    Run on every push and idempotent: an entry whose association is still
    empty while its source directory is now tracked with an id (a create
    whose link a crash or an older version skipped) names that config, and
    every entry's rows learn the source rows they came from. Returns whether
    the manifest changed.
    """
    changed = False
    for cfg in manifest.configurations:
        association = promoted_from(cfg)
        if association is None:
            continue
        source = promote_source(manifest, cfg, association)
        if source is None:
            continue
        if not association.config_id:
            cfg.metadata[PROMOTED_FROM_KEY] = PromotedFrom(
                association.tree, source.id
            ).as_metadata()
            changed = True
        before = [row.metadata.get(PROMOTED_ROW_KEY) for row in cfg.rows]
        link_promote_rows(manifest, tree=association.tree, source=source)
        changed = changed or before != [row.metadata.get(PROMOTED_ROW_KEY) for row in cfg.rows]
    return changed


def link_promote_rows(manifest: Manifest, *, tree: str, source: ManifestConfiguration) -> None:
    """Record which *source* row each row of its promote entries came from (risk 74).

    A config authored in the source tree and promoted before it existed there
    gets its rows on the branch first; when the source tree's own branch later
    assigns their ids, a promote entry's row in the same row directory is
    recorded as created from that row -- also when the entry's own
    association was already linked. Only a row the promote created from a
    row file with no source id (marked authored, risk 83) is linked, once:
    a row copied into a shadow or created from a known source row records
    its source when it is made, and a path alone never proves a later row is
    the one it copied (risk 81).
    """
    source_rows = {row.path: row.id for row in source.rows}
    for cfg in manifest.configurations:
        association = promoted_from(cfg)
        if (
            cfg is source
            or association is None
            or association.tree != tree
            or association.config_id != source.id
            or cfg.component_id != source.component_id
        ):
            continue
        for row in cfg.rows:
            if row.metadata.get(AUTHORED_ROW_KEY) and row.path in source_rows:
                row.metadata[PROMOTED_ROW_KEY] = source_rows[row.path]
                row.metadata.pop(AUTHORED_ROW_KEY)


def stamp_updated_config(
    client: Any,
    *,
    manifest: Manifest,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    config_path_str: str,
    hashes: LocalConfigHashes,
    response: Any,
    warnings: list[dict[str, str]],
    source_tree: str,
    config_dir: Path | None = None,
) -> None:
    """Refresh a pushed config's manifest bookkeeping from the API state (#686).

    When the API state cannot be established (partial response AND a failed
    read-back), ``pull_config_hash`` is left exactly as it was: visibly stale
    beats confidently wrong, and a disk-derived value is what created the
    phantom drift in the first place.

    Only the entry of *branch_id* is ever changed. A dev branch created from
    production holds the same config ids, so ``push --branch DEV`` from
    ``main/`` updates a config the manifest tracks for PRODUCTION; that entry
    is left alone (production still lacks the edit) and a dev-branch shadow is
    recorded instead (:func:`promote_shadow`; only on the promote path, D20).
    Any other update that finds no entry of *branch_id* is an adopted-by-id
    config (issue #497) -- an untracked local file whose
    ``_keboola.config_id`` resolved on the branch. It is registered at
    *config_path_str*, the directory just pushed, never at another branch's
    path or with another branch's rows. Its own row dirs (in *config_dir*)
    that carry the id of a row the config has are registered too, with an
    unknown base (D8): left out, the next diff read them as new rows and the
    next push created every one of them a second time.
    """
    stamp = config_baseline(
        client,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        response=response,
    )
    if stamp.warning is not None:
        warnings.append(stamp.warning)
    target = find_branch_entry(manifest, component_id, config_id, branch_id) or promote_shadow(
        client,
        manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        source_tree=source_tree,
        path=config_path_str,
        response=response,
    )
    if target is not None:
        target.metadata["pull_hash"] = hashes.file_hash
        record_companions(target.metadata, hashes.extra_hashes)
        apply_stamp(target.metadata, stamp)
        return
    entry = writeback_create_config_in_manifest(
        manifest=manifest,
        component_id=component_id,
        branch_id=branch_id,
        config_path_str=config_path_str,
        new_id=config_id,
        file_hash=hashes.file_hash,
        cfg_hash=stamp.cfg_hash,
        extra_hashes=hashes.extra_hashes,
    ).entry
    apply_stamp(entry.metadata, stamp)
    record_companions(entry.metadata, hashes.extra_hashes)
    if config_dir is not None:
        entry.rows = adopted_row_entries(
            config_dir, _branch_row_ids(client, component_id, config_id, branch_id, response)
        )


def promote_shadow(
    client: Any,
    manifest: Manifest,
    *,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    source_tree: str,
    path: str | None = None,
    response: Any = None,
) -> ManifestConfiguration | None:
    """Record the *branch_id* entry for a source-tree config written to it.

    Only on the promote path (D20): *branch_id* has no tree of its own, so
    ``push --branch DEV`` reads *source_tree* (``main/``), whose entries belong
    to production. A dev branch created from production holds the same config
    ids, so the first write to DEV for such a directory needs an entry of its
    own: a *shadow* at the same path, recording which source config it stands
    for (``promoted_from``, D13), so ``scope_manifest`` diffs that directory
    against it from then on and the production entry keeps describing
    production. The source is PRODUCTION's entry of that id (the promote source
    tree is the default tree, production's) -- chosen by ``same_branch``, never
    by tree path, which maps an unregistered branch to the default tree too
    (D28), and never "the first entry with that id on any branch". When *path*
    is given, it must be the directory just pushed.

    The shadow starts as a copy of the production entry's baselines -- the dev
    branch was a copy of production when it was created, and a baseline that
    claimed more would hide a dev-side change made since. The caller then
    re-stamps whatever it just wrote. Rows are copied too, with their
    production baselines, but only rows that exist on the dev branch: a copied
    row is "on the target", so the next diff updates it in place instead of
    creating it again, while a row the dev branch lacks stays untracked and
    is created like before. When the dev rows cannot be read every row is
    copied, the safe side (a missing one then reads ``remote_deleted``,
    which push never acts on, rather than ``added``, which duplicates).

    Returns ``None`` off the promote path or when *source_tree* does not
    track the config.
    """
    if branch_tree_path(manifest, branch_id) == source_tree:
        return None
    source = next(
        (
            c
            for c in manifest.configurations
            if c.component_id == component_id
            and c.id == config_id
            and same_branch(manifest, c.branch_id, None)
        ),
        None,
    )
    if source is None or (path is not None and source.path != path):
        return None
    on_branch = _branch_row_ids(client, component_id, config_id, branch_id, response)
    metadata = copy.deepcopy(source.metadata)
    metadata[PROMOTED_FROM_KEY] = PromotedFrom(source_tree, config_id).as_metadata()
    shadow = ManifestConfiguration(
        branchId=branch_id or 0,
        componentId=component_id,
        id=config_id,
        path=source.path,
        metadata=metadata,
        rows=_shadow_rows(source.rows, on_branch),
    )
    manifest.configurations.append(shadow)
    return shadow


def _shadow_rows(
    rows: list[ManifestConfigRow], on_branch: set[str] | None
) -> list[ManifestConfigRow]:
    """The source rows a shadow tracks: those on the branch, or all with an unknown base.

    When the branch's rows cannot be read, each row is tracked with no
    baseline and ``unknown_base: unread`` (D22): diff then lists a row the
    branch lacks as ``added`` (the next push creates it once), and a row the
    branch has but that differs as a conflict -- never as an edit to send.
    """
    if on_branch is not None:
        copied = [copy.deepcopy(row) for row in rows if row.id in on_branch]
    else:
        copied = [
            ManifestConfigRow(
                id=row.id,
                path=row.path,
                metadata={
                    "pull_hash": "",
                    "pull_config_hash": "",
                    UNKNOWN_BASE_KEY: UNKNOWN_BASE_UNREAD,
                },
            )
            for row in rows
        ]
    for row in copied:
        # The source row each copy stands for, recorded now: a later path
        # match cannot tell it from a new row reusing the path (risk 81).
        row.metadata[PROMOTED_ROW_KEY] = row.id
    return copied


def _branch_row_ids(
    client: Any, component_id: str, config_id: str, branch_id: int | None, response: Any
) -> set[str] | None:
    """Row ids of the config on *branch_id*, ``None`` when they cannot be read."""
    payload = response if isinstance(response, dict) and "rows" in response else None
    if payload is None:
        try:
            payload = client.get_config_detail(
                component_id=component_id, config_id=config_id, branch_id=branch_id
            )
        except Exception:
            logger.warning(
                "Could not read the rows of %s/%s on branch %s", component_id, config_id, branch_id
            )
            return None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    return {str(row.get("id", "")) for row in rows if isinstance(row, dict)}


def writeback_create_config_in_manifest(
    *,
    manifest: Manifest,
    component_id: str,
    branch_id: int | None,
    config_path_str: str,
    new_id: str,
    file_hash: str,
    cfg_hash: str,
    extra_hashes: dict[str, str],
) -> WritebackResult:
    """Record a freshly-created config in the manifest.

    If a placeholder entry already exists at ``(branch_id, component_id, path)``
    -- the FIIA / scaffold emit pattern -- update it in place, preserving any
    user-declared metadata (e.g. ``KBC.configuration.folderName``) and
    refreshing only the bookkeeping hashes. Otherwise append a new entry.

    Matching includes ``branch_id`` because a single manifest can hold entries
    from multiple branches in git-branching mode; matching on
    ``(component_id, path)`` alone would risk updating the wrong branch's entry
    when the same logical path exists under two branches. Production is
    spelled ``None``, ``0`` or the default branch id, so the comparison goes
    through :func:`same_branch`.

    Returns a :class:`WritebackResult` carrying the entry and its pre-overwrite
    ``previous_id`` so the create pass can remap any child row parents /
    transformation variable links from the placeholder id to the freshly-assigned
    ULID.
    """
    target_branch = branch_id or 0
    for entry in manifest.configurations:
        if (
            same_branch(manifest, entry.branch_id, branch_id)
            and entry.component_id == component_id
            and entry.path == config_path_str
        ):
            previous_id = entry.id
            entry.id = new_id
            entry.metadata["pull_hash"] = file_hash
            entry.metadata["pull_config_hash"] = cfg_hash
            entry.metadata["pull_extra_hashes"] = extra_hashes
            return WritebackResult(entry=entry, previous_id=previous_id)
    new_entry = ManifestConfiguration(
        branchId=target_branch,
        componentId=component_id,
        id=new_id,
        path=config_path_str,
        metadata={
            "pull_hash": file_hash,
            "pull_config_hash": cfg_hash,
            "pull_extra_hashes": extra_hashes,
        },
    )
    manifest.configurations.append(new_entry)
    return WritebackResult(entry=new_entry, previous_id="")


def writeback_create_row_in_manifest(
    *,
    parent: ManifestConfiguration,
    row_path_str: str,
    new_row_id: str,
    file_hash: str,
    cfg_hash: str,
) -> ManifestConfigRow:
    """Record a freshly-created row under its parent in the manifest.

    Mirrors :func:`writeback_create_config_in_manifest` for rows: update any
    placeholder row entry in place, otherwise append.
    """
    for row in parent.rows:
        if row.path == row_path_str:
            row.id = new_row_id
            row.metadata["pull_hash"] = file_hash
            row.metadata["pull_config_hash"] = cfg_hash
            return row
    new_row = ManifestConfigRow(
        id=new_row_id,
        path=row_path_str,
        metadata={"pull_hash": file_hash, "pull_config_hash": cfg_hash},
    )
    parent.rows.append(new_row)
    return new_row


def manifest_folder(
    manifest: Manifest, component_id: str, branch_id: int | None, config_path_str: str
) -> str | None:
    """Return the UI folder the manifest records for a config about to be created.

    The manifest stores the folder under the legacy metadata key name
    (``FOLDER_METADATA_KEY``); the create POST sends it as the configuration's
    ``folder`` field. Matches the entry the way the create writeback does, on
    ``(branch_id, component_id, path)``. ``None`` when there is no folder.
    """
    for entry in manifest.configurations:
        if (
            same_branch(manifest, entry.branch_id, branch_id)
            and entry.component_id == component_id
            and entry.path == config_path_str
        ):
            folder = entry.metadata.get(FOLDER_METADATA_KEY)
            return str(folder) if folder else None
    return None


def propagate_kbc_metadata(
    client: Any,
    entry: ManifestConfiguration,
    branch_id: int | None,
) -> str | None:
    """POST any ``KBC.*`` keys from the manifest entry to the metadata API.

    Bookkeeping keys (``pull_hash``, ``pull_config_hash``, ...) live in the same
    metadata dict but are filtered by the ``KBC.`` prefix. The folder key
    (``FOLDER_METADATA_KEY``) is skipped: the create POST already sent it as
    the configuration's ``folder`` field, and no first-party client writes
    that key to the metadata API any more. Called only on CREATE;
    updates use ``kbagent config set-metadata`` explicitly. The metadata API
    stores configuration-level annotations only -- this is **not** a secret
    store; do not place tokens or passwords under ``KBC.*`` keys.

    Returns ``None`` on success (or when there are no KBC.* keys to propagate).
    Returns the API error message on a non-fatal write failure: the config is
    already created on the remote and the manifest writeback is complete, so a
    single failed metadata POST is reported back to the push loop as an
    accumulated error rather than aborting the rest of the push.
    """
    entries = [
        (key, str(value))
        for key, value in entry.metadata.items()
        if key.startswith("KBC.") and key != FOLDER_METADATA_KEY
    ]
    if not entries:
        return None
    try:
        client.set_config_metadata(
            component_id=entry.component_id,
            config_id=entry.id,
            entries=entries,
            branch_id=branch_id,
        )
    except KeboolaApiError as exc:
        logger.warning(
            "Failed to propagate KBC.* metadata for %s/%s: %s",
            entry.component_id,
            entry.id,
            exc,
        )
        return exc.message
    return None


def writeback_after_push(
    service: SyncService,
    local_data: dict[str, Any],
    config_dir: Path,
    config_id: str,
    pushed_configuration: dict[str, Any],
    *,
    is_row: bool = False,
    row_id: str = "",
) -> None:
    """Update local ``_config.yml`` after a successful push.

    Writes back the API-assigned ``_keboola.config_id`` (on first create) and
    the encrypted secret values (so local matches remote state). A scaffold's
    ``_keboola.base_config_hash`` is dropped: after the push the manifest
    holds the baseline (issue #792 E). A config whose description is empty
    gets the ``description: ''`` line a fresh pull writes (L2), so its
    ``pull_hash`` is the one a pull would record. A row gets *row_id* in its
    ``_keboola`` footer, as a pull writes it: without it the next pull of a
    tree that lost the row's manifest entry (a ``pull --branch`` and back)
    could not find the row's directory by its id, wrote the row into a second
    directory, and the first one diffed as a new row -- a duplicate on the
    next push (R2). The promote path passes no *row_id*: ``main/`` keeps its
    own ids (D19).
    """
    if not is_row:
        local_data = canonical_description(local_data, config_dir)
    elif row_id:
        local_data.setdefault("_keboola", {})["row_id"] = row_id
    keboola_meta = local_data.setdefault("_keboola", {})
    keboola_meta.pop(BASE_CONFIG_HASH_KEY, None)
    if config_id:
        keboola_meta["config_id"] = config_id

    pushed_params = pushed_configuration.get("parameters", {})
    local_params = local_data.get("parameters", {})
    if pushed_params and local_params:
        apply_encrypted_to_local(local_params, pushed_params)

    service._write_config_file(config_dir, local_data)
    logger.debug("Updated local config at %s after push", config_dir)
