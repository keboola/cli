"""Phase B of ``sync push``: row creates, updates and deletes, behind a version check (#827, R1-R4).

Extracted from ``sync_service.py`` (file-size budget). The Storage API row
endpoints take no ``If-Match``: a row write is unconditional, so a change
made in the Keboola UI to a config or its rows after the diff read it would
be overwritten silently. Before the FIRST row work of a parent config --
adoption, promote bookkeeping and the row write -- push reads the parent and
compares its version with the version push expects (R3):

- a parent this push created: no check (nobody else knows it yet);
- a parent Phase A updated: the version our own PUT answered with. A PUT
  that answered 412 and was verified by reading the config back proves the
  config's fields only, never that no row changed: its rows are blocked;
- any other parent: the version the diff read (``parent_remote_version``).

Anything else -- no expected version, a failed read, a 404, a read without
a version, a different version -- blocks every row change of that parent:
each gets an ``errors[]`` entry (``SYNC_CONFLICT``, or the failed read's own
code), nothing is written for them and their files and manifest entries stay
as they are. The parent then joins ``held``, so no later phase writes it.
The result is reused for the parent's later rows: each of our own row writes
raises its version. Rows are processed grouped by parent (R4), so unrelated
work does not stretch one parent's window.

Limit: the check narrows the window, it does not close it. A UI edit
between the check and the parent's last row write (many rows, retries) is
still overwritten. Only an ``If-Match`` on the row endpoints closes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.manifest import Manifest
from ._encryption import SecretCache
from ._sync_push_create import (
    CreateLedger,
    CreateTarget,
    is_fatal_push_error,
    register_adopted_parent,
)
from ._sync_push_ops import push_row_change
from ._sync_push_update import (
    PUT_RESPONSE,
    ParentWrite,
    config_error_code,
    held_row_error,
)

if TYPE_CHECKING:
    from ._sync_promote_remap import PromoteRemap
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


@dataclass
class RowPhase:
    """What Phase B reads and fills (the push's state for its rows)."""

    service: SyncService
    client: Any
    project_root: Path
    manifest: Manifest
    branch_id: int | None
    target: CreateTarget
    ledger: CreateLedger
    remap: PromoteRemap | None
    checkpoint: partial[Any]
    held: set[tuple[str, str]]
    parent_writes: dict[tuple[str, str], ParentWrite]
    allow_plaintext_fallback: bool
    warnings: list[dict[str, Any]]
    known_secrets: SecretCache
    errors: list[dict[str, Any]]
    pushed_details: list[dict[str, Any]]
    created_row_id_map: dict[tuple[str, str], str]
    created_rows_by_parent: dict[str, list[str]]


@dataclass
class RowTotals:
    """What Phase B did: the counts and whether the manifest changed."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    manifest_dirty: bool = False
    # (component_id, parent id) -> None when its rows may be written, else
    # the errors[] template of its blocked rows (R3, once per parent).
    checks: dict[tuple[str, str], dict[str, str] | None] = field(default_factory=dict)


def group_by_parent(row_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The row changes grouped by parent, parents in order of first appearance (R4).

    The order within a parent is kept, so a parent's deletes and creates run
    as the diff listed them.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for change in row_changes:
        parent = change.get("parent_config_id") or change.get("parent_path") or ""
        groups.setdefault((change["component_id"], parent), []).append(change)
    return [change for group in groups.values() for change in group]


def _blocked(code: str, component_id: str, parent_id: str, why: str) -> dict[str, str]:
    """The errors[] template of the rows of a parent the precheck blocked."""
    return {
        "error_code": str(code),
        "message": (
            f"Row not pushed: its configuration {component_id}/{parent_id} {why}. Nothing was "
            "written for this row; its files and manifest entry are unchanged. Row writes "
            "take no version precondition, so push does not write rows of a configuration "
            "it cannot prove unchanged. Run `kbagent sync push` again: the next diff reads "
            "the configuration's current version, and a row that was itself changed in "
            "Keboola then shows as a conflict and is not pushed."
        ),
    }


def _no_version(component_id: str, parent_id: str, where: str) -> dict[str, str]:
    """The errors[] template when the stack reports no configuration version (NB-4).

    Pushing again cannot help: without a version push can never check the
    configuration, so its rows stay blocked. Current Storage always reports
    one; this state is a stack defect to report.
    """
    return {
        "error_code": str(ErrorCode.SYNC_CONFLICT),
        "message": (
            f"Row not pushed: the Keboola stack did not report a version for configuration "
            f"{component_id}/{parent_id} ({where}), so push cannot check that it is unchanged "
            "before writing its rows, and pushing again will not help. Nothing was written for "
            "this row. Current Keboola Storage always reports a version: report this to "
            "Keboola support with the stack URL."
        ),
    }


def _expected_version(phase: RowPhase, change: dict[str, Any], key: tuple[str, str]) -> Any:
    """The version the parent must be at, or a reason it cannot be checked (R3)."""
    if key in phase.ledger.recovered_versions:  # an earlier push's create (item 2)
        return phase.ledger.recovered_versions[key]
    write = phase.parent_writes.get(key)
    if write is None:
        return change.get("parent_remote_version")
    if write.provenance != PUT_RESPONSE:
        return (
            "was updated in this push, but its update was confirmed by reading it back, which "
            "does not prove that no row changed in Keboola"
        )
    return write.version


def check_parent(phase: RowPhase, change: dict[str, Any], parent_id: str) -> dict[str, str] | None:
    """None when the rows of *parent_id* may be written, else their error template (R3)."""
    component_id = change["component_id"]
    if phase.ledger.created_here(component_id, parent_id):
        return None
    expected = _expected_version(phase, change, (component_id, parent_id))
    if isinstance(expected, str):
        return _blocked(ErrorCode.SYNC_CONFLICT, component_id, parent_id, expected)
    if not isinstance(expected, int) or isinstance(expected, bool):
        return _no_version(component_id, parent_id, "in the listing the diff read")
    try:
        current = phase.client.get_config_detail(
            component_id=component_id, config_id=parent_id, branch_id=phase.branch_id
        )
    except KeboolaApiError as exc:
        why = f"could not be read to check its version ({exc.message})"
        return _blocked(exc.error_code or ErrorCode.API_ERROR, component_id, parent_id, why)
    version = current.get("version") if isinstance(current, dict) else None
    if not isinstance(version, int) or isinstance(version, bool):
        return _no_version(component_id, parent_id, "when push read it before its rows")
    if version != expected:
        why = (
            f"changed in Keboola after this push read it (version {expected}, now {version}; "
            "the configuration or one of its rows was edited)"
        )
        return _blocked(ErrorCode.SYNC_CONFLICT, component_id, parent_id, why)
    return None


def _failed_parent_error(code: str, change: dict[str, Any]) -> dict[str, str]:
    """The errors[] entry of a new row whose new configuration failed to be created."""
    component_id, parent_path = change["component_id"], change.get("parent_path", "")
    return {
        "change_type": change["change_type"],
        "component_id": component_id,
        "config_id": change.get("config_id", ""),
        "parent_config_id": "",
        "parent_path": parent_path,
        "error_code": code,
        "message": (
            f"Row {parent_path}/{change.get('path', '')} was not created because the create of "
            f"its configuration {component_id} at {parent_path} failed in this push with {code} "
            "(see the configuration's error). Nothing was written for the row. The next "
            "`kbagent sync push` creates both."
        ),
    }


def _row_error(change: dict[str, Any], template: dict[str, str], parent_id: str) -> dict[str, str]:
    """A blocked row's errors[] entry; names its parent's id (also a recovered new one)."""
    entry = {
        "change_type": change["change_type"],
        "component_id": change["component_id"],
        "config_id": change.get("config_id", ""),
        "parent_config_id": change.get("parent_config_id") or parent_id,
        **template,
    }
    if change.get("parent_path"):
        entry["parent_path"] = change["parent_path"]
    return entry


def push_rows(phase: RowPhase, row_changes: list[dict[str, Any]]) -> RowTotals:
    """Run Phase B over *row_changes* (moved from ``SyncService.push``)."""
    totals = RowTotals()
    service, ledger, remap = phase.service, phase.ledger, phase.remap
    for change in group_by_parent(row_changes):
        change_type = change["change_type"]
        component_id = change["component_id"]
        config_id = change["config_id"]
        source_row_id = change.get("source_row_id") or config_id  # risk 71
        parent_config_id = change.get("parent_config_id", "")
        if (component_id, parent_config_id) in phase.held:  # its config failed (#827 F3)
            code = config_error_code(phase.errors, component_id, parent_config_id)
            phase.errors.append(held_row_error(code, change))
            continue
        failed = ledger.failed_parent_code(change)
        if failed is not None:  # its new config failed (round 1 item 5, Devin review)
            phase.errors.append(_failed_parent_error(failed, change))
            continue
        try:
            # The parent's id on the target after Phase A: a placeholder
            # maps to its ULID (KFR-05), a new config dir to the config
            # created there (D7) -- a parent that was not created skips
            # the row with an error. An adopted dir is registered (D8).
            effective_parent_id = ledger.row_parent(change)
            key = (component_id, effective_parent_id)
            if key not in totals.checks:  # before any bookkeeping of its rows (R3)
                totals.checks[key] = check_parent(phase, change, effective_parent_id)
            blocked = totals.checks[key]
            if blocked is not None:
                phase.errors.append(_row_error(change, blocked, effective_parent_id))
                continue
            totals.manifest_dirty = True  # before a create's checkpoint (D25)
            if change.get("parent_path") and parent_config_id:
                register_adopted_parent(
                    service,
                    phase.client,
                    manifest=phase.manifest,
                    target=phase.target,
                    component_id=component_id,
                    config_id=parent_config_id,
                    config_path=change["parent_path"],
                )
            if remap is not None:
                remap.refresh(
                    phase.manifest,
                    phase.branch_id,
                    ledger.created_id_map,
                    phase.created_row_id_map,
                )
            new_row_id = push_row_change(
                service,
                phase.client,
                change_type=change_type,
                component_id=component_id,
                parent_config_id=effective_parent_id,
                row_id=config_id,
                source_row_id=source_row_id,
                row_path_str=change.get("path", ""),
                project_root=phase.project_root,
                manifest=phase.manifest,
                branch_id=phase.branch_id,
                allow_plaintext_fallback=phase.allow_plaintext_fallback,
                warnings=phase.warnings,
                known_secrets=phase.known_secrets,
                checkpoint=phase.checkpoint,
                remap=remap,
            )
            if change_type == "added":
                totals.created += 1
                if new_row_id:
                    if source_row_id:
                        phase.created_row_id_map[(effective_parent_id, source_row_id)] = new_row_id
                    phase.created_rows_by_parent.setdefault(effective_parent_id, []).append(
                        new_row_id
                    )
            elif change_type == "modified":
                totals.updated += 1
            elif change_type == "deleted":
                totals.deleted += 1
            phase.pushed_details.append(change)

        except Exception as exc:
            if is_fatal_push_error(exc):
                raise
            totals.manifest_dirty = True
            service._record_push_error(phase.errors, change_type, component_id, config_id, exc)
    # A blocked parent is written by no later phase either (#827 D4).
    phase.held.update(key for key, blocked in totals.checks.items() if blocked is not None)
    return totals
