"""One read of a config's local files: hashed bytes are the parsed bytes (#827, D6).

Shared by ``sync push`` (:mod:`._sync_push_update`) and the link recovery of
Phases C and D: each decides from, sends, rewrites and stamps exactly the
bytes it read once, never a second read of the folder.
"""

from __future__ import annotations

import copy
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..errors import ErrorCode, KeboolaApiError
from ..sync.code_extraction import merge_code_files, owned_companions
from ._sync_companions import current_companions
from ._sync_disabled import file_disables
from ._sync_models import LocalConfigHashes

if TYPE_CHECKING:
    from .sync_service import SyncService

LOCAL_EDIT_DURING_PUSH = "LOCAL_EDIT_DURING_PUSH"


@dataclass(frozen=True)
class CapturedInput:
    """A config's local files as push read them, once.

    Attributes:
        data: ``_config.yml`` parsed -- what the writeback starts from.
        contents: The raw bytes read, by file name (``_config.yml`` and every
            owned companion present).
        merged: *data* with the companion files merged in -- what is sent.
        hashes: SHA-256 of exactly the bytes *data* and *merged* came from,
            and whether that ``_config.yml`` disables the config.
    """

    data: dict[str, Any]
    merged: dict[str, Any]
    hashes: LocalConfigHashes
    contents: dict[str, bytes] = field(default_factory=dict)


def local_state(service: SyncService, config_dir: Path, component_id: str) -> LocalConfigHashes:
    """Hashes of the config's ``_config.yml`` and companion files as they are on disk now."""
    config_file = config_dir / CONFIG_FILENAME
    return LocalConfigHashes(
        file_hash=service._file_hash(config_file) if config_file.is_file() else "",
        cfg_hash="",
        extra_hashes=current_companions(service, config_dir, component_id),
    )


def same_files(a: LocalConfigHashes, b: LocalConfigHashes) -> bool:
    """True iff *a* and *b* describe the same ``_config.yml`` and companion bytes."""
    return a.file_hash == b.file_hash and a.extra_hashes == b.extra_hashes


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def capture_local_input(
    service: SyncService, config_dir: Path, component_id: str
) -> CapturedInput | None:
    """Read ``_config.yml`` and every owned companion file ONCE (D6).

    The bytes are copied into a private directory and parsed and merged from
    there, so the hashes describe exactly what is sent, whatever an editor
    does to *config_dir* meanwhile. ``None`` when there is no usable
    ``_config.yml``.
    """
    contents: dict[str, bytes] = {}
    for name in (CONFIG_FILENAME, *owned_companions(component_id)):
        path = config_dir / name
        if path.is_file():
            contents[name] = path.read_bytes()
    if CONFIG_FILENAME not in contents:
        return None
    with tempfile.TemporaryDirectory(prefix="kbagent-push-") as tmp:
        snapshot = Path(tmp)
        for name, raw in contents.items():
            # A private copy: open() rather than Path.write_bytes, which the
            # read-only-file tests patch for the user's own files.
            with open(snapshot / name, "wb") as handle:
                handle.write(raw)
        data = service._read_config_file(snapshot)
        if data is None:
            return None
        merged = merge_code_files(component_id, copy.deepcopy(data), snapshot)
    hashes = {name: _sha256(raw) for name, raw in contents.items()}
    return CapturedInput(
        contents=contents,
        data=data,
        merged=merged,
        hashes=LocalConfigHashes(
            file_hash=hashes.pop(CONFIG_FILENAME),
            cfg_hash="",
            extra_hashes=hashes,
            disabled=file_disables(data),
        ),
    )


def local_edit_warning(
    component_id: str,
    config_id: str,
    config_dir: Path,
    *,
    secrets: bool,
    recovery: bool = False,
) -> dict[str, Any]:
    """The ``warnings[]`` entry of a config whose files were edited during the push (D7, R7).

    *recovery*: the link recovery of Phases C/D, which changed the remote but
    left the edited files and the baseline alone -- the next step is a diff,
    possibly a conflict to resolve, not simply another push (H4).
    """
    if recovery:
        message = (
            f"{config_dir} was edited while push repaired the links of {component_id}/"
            f"{config_id} in Keboola. The edited files were kept, not written over and not "
            "recorded as synced. Run `kbagent sync diff`: when it reports a conflict, run "
            "`kbagent sync pull` and review the changes first."
        )
    else:
        message = (
            f"{config_dir} was edited while push sent {component_id}/{config_id}. Push sent "
            "the version it read; the newer local content was kept, not pushed, and not "
            "written over. Push again to send it (an edit that changes only comments or "
            "formatting has nothing to send)."
        )
    if secrets:
        message += (
            " The file still holds its secret values in plain text: the encrypted values "
            "were not written back."
        )
    return {
        "code": LOCAL_EDIT_DURING_PUSH,
        "change_type": "local_edit_during_push",
        "component_id": component_id,
        "config_id": config_id,
        "path": str(config_dir),
        "message": message,
    }


def local_file_changed_error(config_dir: Path) -> KeboolaApiError:
    """The per-change error of a config or row whose files changed while push read them (D6)."""
    return KeboolaApiError(
        message=(
            f"{config_dir} was not pushed: its files changed while push read them, so push "
            "could not tell which version to send. Nothing was written for this config. "
            "Run `kbagent sync push` again."
        ),
        error_code=ErrorCode.SYNC_LOCAL_FILE_CHANGED,
        retryable=False,
    )


@dataclass(frozen=True)
class RowInput:
    """A row's ``_config.yml`` as push read it, once (#827 item 8 for rows)."""

    data: dict[str, Any]
    file_hash: str


def capture_row_input(service: SyncService, row_dir: Path) -> RowInput:
    """Read a row's ``_config.yml`` ONCE; a change during the read is not sent (D6).

    A row has no companion files: its ``_config.yml`` is the whole row.
    Raises ``FileNotFoundError`` without a usable file and
    ``SYNC_LOCAL_FILE_CHANGED`` when the file changed while it was read.
    """
    before = local_state(service, row_dir, "")
    captured = capture_local_input(service, row_dir, "")
    if captured is None:
        raise FileNotFoundError(f"Row file not found: {row_dir / CONFIG_FILENAME}")
    if not same_files(before, captured.hashes):
        raise local_file_changed_error(row_dir)
    return RowInput(data=captured.data, file_hash=captured.hashes.file_hash)


def row_file_unchanged(service: SyncService, row_dir: Path, read: RowInput) -> bool:
    """True iff the row's ``_config.yml`` still holds the bytes push read (D7)."""
    return local_state(service, row_dir, "").file_hash == read.file_hash
