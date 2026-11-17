"""Push of a ``modified`` config: one read of the local files, one conditional PUT (#827).

``sync push`` updates a config the diff calls ``modified``. Three windows of
that update used to lose work silently:

- **Remote (item 4).** The PUT carried no precondition, so an edit made in
  the Keboola UI between the diff and the PUT was overwritten. The diff now
  records the version it read (``remote_version``) and the PUT sends it as
  ``If-Match`` (:func:`update_if_unchanged`). The Storage API answers 412
  when the config, or one of its rows, changed since. Push then reads the
  config: when it holds exactly what push sent -- configuration, name,
  description and the enabled state push sent -- the requested state is
  verified and the update counts as done (a PUT whose response was lost and
  that the transport repeated lands here, R4). Otherwise it is a
  ``SYNC_CONFLICT``: the local files and the manifest entry stay as they
  were, and no later phase of the same push writes that config (D4). The
  equality proves the requested state, never that this push wrote it.
- **Local, before the PUT (item 8a).** The local files are read once
  (:func:`capture_local_input`): the bytes that are hashed are the bytes that
  are parsed and sent. A file that changed between the first hash and the
  read is not pushed (``SYNC_LOCAL_FILE_CHANGED``, run push again).
- **Local, after the PUT (items 8a, 8b).** Right before the writeback the
  files are hashed again (:func:`settle_local_files`). A file edited during
  the push is not overwritten: the writeback is skipped, a
  ``LOCAL_EDIT_DURING_PUSH`` warning names it, and the manifest records what
  was sent, so the edit stays a local change for the next diff. Otherwise
  the stamp is the hash of the bytes the writeback WROTE, never a fresh read
  -- an edit saved after the write still differs from it. The hashes are the
  push receipt the later phases stamp from (the promote rebind, D9).

Limits (documented, not fixed): an editor save that lands between the last
check and the writeback is overwritten (no editor locking); a comment-only
edit diffs clean because diff compares normalized content; row PUTs, the
link backfill PUTs and data-app writes send no precondition; creates keep
their own identity checkpoint (``pending_creates``).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import tempfile
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..errors import ErrorCode, KeboolaApiError
from ..sync.branch_scope import branch_tree_path, find_branch_entry
from ..sync.code_extraction import PYPROJECT_FILENAME, merge_code_files, owned_companions
from ..sync.config_format import local_config_to_api
from ..sync.manifest import Manifest, ManifestConfiguration
from ._encryption import SecretCache, collect_secrets, encrypt_secrets_in_config
from ._sync_baseline import raise_on_legacy_boundary
from ._sync_companions import current_companions, require_decided_packages
from ._sync_disabled import baseline_entry, file_disables, pushed_disabled
from ._sync_models import LocalConfigHashes
from ._sync_push_ops import guard_script_shape
from ._sync_workspace import SANDBOXES_COMPONENT_ID, warn_on_backend_size_change
from ._sync_writeback import stamp_updated_config, writeback_after_push
from .data_app_service import DATA_APP_COMPONENT_ID

if TYPE_CHECKING:
    from ._sync_promote_remap import PromoteRemap
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

LOCAL_EDIT_DURING_PUSH = "LOCAL_EDIT_DURING_PUSH"


@dataclass(frozen=True)
class CapturedInput:
    """A config's local files as push read them, once.

    Attributes:
        data: ``_config.yml`` parsed -- what the writeback starts from.
        merged: *data* with the companion files merged in -- what is sent.
        hashes: SHA-256 of exactly the bytes *data* and *merged* came from,
            and whether that ``_config.yml`` disables the config.
    """

    data: dict[str, Any]
    merged: dict[str, Any]
    hashes: LocalConfigHashes


@dataclass(frozen=True)
class SentUpdate:
    """The fields one config PUT sends; a 412 is verified against exactly these."""

    name: str
    description: str
    configuration: dict[str, Any]
    is_disabled: bool | None


@dataclass(frozen=True)
class UpdateOutcome:
    """A pushed update: the API's view of the config and the hashes to stamp.

    ``hashes`` is the push receipt: the bytes push wrote back (or, when the
    files changed during the push, the bytes it read and sent).
    """

    response: dict[str, Any]
    hashes: LocalConfigHashes


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
            (snapshot / name).write_bytes(raw)
        data = service._read_config_file(snapshot)
        if data is None:
            return None
        merged = merge_code_files(component_id, copy.deepcopy(data), snapshot)
    hashes = {name: _sha256(raw) for name, raw in contents.items()}
    return CapturedInput(
        data=data,
        merged=merged,
        hashes=LocalConfigHashes(
            file_hash=hashes.pop(CONFIG_FILENAME),
            cfg_hash="",
            extra_hashes=hashes,
            disabled=file_disables(data),
        ),
    )


def local_file_changed_error(config_dir: Path) -> KeboolaApiError:
    """The per-change error of a config whose files changed while push read them (D6)."""
    return KeboolaApiError(
        message=(
            f"{config_dir} was not pushed: its files changed while push read them, so push "
            "could not tell which version to send. Nothing was written for this config. "
            "Run `kbagent sync push` again."
        ),
        error_code=ErrorCode.SYNC_LOCAL_FILE_CHANGED,
        retryable=False,
    )


def _canonical(value: Any) -> Any:
    """*value* with an empty object and an empty list made equal (the API returns ``[]`` for ``{}``)."""
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()} if value else []
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def holds_sent(remote: dict[str, Any] | None, sent: SentUpdate) -> bool:
    """True iff *remote* holds every field *sent* sets (R1, R2).

    The exact outgoing configuration -- encrypted values compared as the
    ciphertext sent, never through ``config_hash``'s placeholder -- plus name,
    description, and the enabled state when the PUT sent one.
    """
    if not isinstance(remote, dict):
        return False
    if _canonical(remote.get("configuration") or {}) != _canonical(sent.configuration or {}):
        return False
    if (remote.get("name") or "") != sent.name:
        return False
    if (remote.get("description") or "") != sent.description:
        return False
    return sent.is_disabled is None or bool(remote.get("isDisabled")) == sent.is_disabled


def update_if_unchanged(
    client: Any,
    *,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    expected_version: int | None,
    sent: SentUpdate,
) -> dict[str, Any]:
    """PUT *sent* only while the config is at *expected_version* (#827, D2, D3).

    A 412 means the config, or one of its rows, changed after the diff read
    it -- or the transport repeated a PUT that had landed. The config is read
    back: when it holds exactly *sent*, the requested state is verified and
    that read is returned as the response. Otherwise a ``SYNC_CONFLICT`` is
    raised and nothing more is written. Without *expected_version* (the
    listing carried none) the PUT has no precondition, as before.
    """
    precondition = {} if expected_version is None else {"expected_version": expected_version}
    try:
        result = client.update_config(
            component_id=component_id,
            config_id=config_id,
            name=sent.name,
            configuration=sent.configuration,
            description=sent.description,
            change_description="Updated via kbagent sync push",
            is_disabled=sent.is_disabled,
            branch_id=branch_id,
            **precondition,
        )
    except KeboolaApiError as exc:
        if exc.status_code != HTTPStatus.PRECONDITION_FAILED:
            raise
        remote = client.get_config_detail(
            component_id=component_id, config_id=config_id, branch_id=branch_id
        )
        if holds_sent(remote, sent):
            logger.info(
                "Update of %s/%s answered 412, and the config holds what push sent: verified",
                component_id,
                config_id,
            )
            return remote
        raise KeboolaApiError(
            message=(
                f"{component_id}/{config_id} was not updated: it changed in Keboola after this "
                f"push read it at version {expected_version} (the config or one of its rows was "
                "edited). The local files and the manifest were left as they are. Run "
                "`kbagent sync pull` for this branch, review the changes, then push again."
            ),
            status_code=exc.status_code,
            error_code=ErrorCode.SYNC_CONFLICT,
            retryable=False,
        ) from exc
    return result if isinstance(result, dict) else {}


def local_edit_warning(
    component_id: str, config_id: str, config_dir: Path, *, secrets: bool
) -> dict[str, Any]:
    """The ``warnings[]`` entry of a config whose files were edited during the push (D7, R7)."""
    message = (
        f"{config_dir} was edited while push sent {component_id}/{config_id}. Push sent the "
        "version it read; the newer local content was kept, not pushed, and not written over. "
        "`kbagent sync diff` shows it as a local change; push it again."
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


def settle_local_files(
    service: SyncService,
    captured: CapturedInput,
    config_dir: Path,
    *,
    component_id: str,
    config_id: str,
    file_config_id: str,
    configuration: dict[str, Any],
    warnings: list[dict[str, Any]] | None,
) -> LocalConfigHashes:
    """Write the pushed state back unless the files changed; return the hashes to stamp (D7, D8).

    The returned hashes describe bytes push knows: the ones it wrote, else
    the ones it read and sent -- never a fresh read of the directory.
    """
    sink = warnings if warnings is not None else []
    found: dict[str, str] = {}
    collect_secrets(captured.data, "", found)
    if not same_files(local_state(service, config_dir, component_id), captured.hashes):
        sink.append(local_edit_warning(component_id, config_id, config_dir, secrets=bool(found)))
        return captured.hashes
    written = writeback_after_push(
        service,
        copy.deepcopy(captured.data),
        config_dir,
        file_config_id,
        configuration,
        component_id=component_id,
        warnings=warnings,
        expected_pyproject_hash=captured.hashes.extra_hashes.get(PYPROJECT_FILENAME),
    )
    extra = dict(captured.hashes.extra_hashes)
    pyproject = written.pyproject
    if pyproject is not None and pyproject.edited:
        sink.append(local_edit_warning(component_id, config_id, config_dir, secrets=False))
    elif pyproject is not None and pyproject.file_hash and PYPROJECT_FILENAME in extra:
        extra[PYPROJECT_FILENAME] = pyproject.file_hash
    return LocalConfigHashes(
        file_hash=written.file_hash,
        cfg_hash="",
        extra_hashes=extra,
        disabled=captured.hashes.disabled,
    )


def push_update(
    service: SyncService,
    client: Any,
    component_id: str,
    config_id: str,
    config_path_str: str,
    project_root: Path,
    manifest: Manifest,
    branch_id: int | None,
    *,
    allow_plaintext_fallback: bool = False,
    warnings: list[dict[str, Any]] | None = None,
    known_secrets: SecretCache | None = None,
    remap: PromoteRemap | None = None,
    remote_version: int | None = None,
) -> UpdateOutcome:
    """Update an existing config from its local files.

    On the promote path (*remap* given) the payload's references are remapped
    to the target's ids (D24) and the local file keeps its own id (D23).
    *remote_version* is the target's version the diff read: the PUT's
    ``If-Match`` (#827).

    Returns the API response (the baseline is stamped from the remote's own
    view of the config, issue #686) and the receipt hashes. ``warnings``
    accumulates the ``script[]`` normalization records of
    :func:`guard_script_shape`, a workspace's ``parameters.backendSize``
    change (CLI-25) and ``LOCAL_EDIT_DURING_PUSH``.
    """
    branch_path = service._resolve_source_branch_path(manifest, project_root, branch_id)
    config_dir = project_root / branch_path / config_path_str
    before_read = local_state(service, config_dir, component_id)
    captured = capture_local_input(service, config_dir, component_id)
    if captured is None:
        raise FileNotFoundError(f"Config file not found: {config_dir / CONFIG_FILENAME}")
    if not same_files(before_read, captured.hashes):
        raise local_file_changed_error(config_dir)
    local_data = captured.merged
    # Packages a silent pyproject.toml left undecided: the target's own (T1).
    require_decided_packages(
        client,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        local_data=local_data,
        config_path=config_path_str,
    )

    name, description, configuration = local_config_to_api(local_data)
    baseline = baseline_entry(
        manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        source_tree=branch_path,
        path=config_path_str,
    )

    # Runtime-safety backstop on the assembled API body (issues #245/#274).
    configuration = guard_script_shape(
        component_id, configuration, warnings, config_id=config_id, config_path=config_path_str
    )
    project_id = manifest.project.id if manifest.project else None
    original = encrypt_secrets_in_config(
        client,
        project_id,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
        known=known_secrets,
    )
    configuration = original
    if remap is not None:
        # The remap starts from the source-id payload, once (D24).
        configuration = remap.apply(component_id, original)
        if component_id == DATA_APP_COMPONENT_ID:
            configuration = copy.deepcopy(configuration)
            _keep_target_app_id(client, config_id, branch_id, configuration)
        # rebind_pending is on disk before the PUT that may need it.
        remap.before_update(component_id, config_id, original)

    if component_id == SANDBOXES_COMPONENT_ID and warnings is not None:
        warn_on_backend_size_change(
            client,
            config_id=config_id,
            configuration=configuration,
            branch_id=branch_id,
            warnings=warnings,
        )

    result = update_if_unchanged(
        client,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        expected_version=remote_version,
        sent=SentUpdate(
            name=name,
            description=description,
            configuration=configuration,
            # Explicit key in the local YAML pushes the state; an absent key
            # leaves the remote state untouched (issue #467) unless the
            # baseline file the push's diff compared against had the line --
            # then deleting it re-enables (``_sync_disabled``).
            is_disabled=pushed_disabled(local_data, baseline.metadata if baseline else None),
        ),
    )
    logger.info("Updated config %s/%s", component_id, config_id)
    if remap is not None:
        remap.after_update(component_id, config_id, original)
        remap.record(
            component_id,
            config_id,
            config_dir=config_dir,
            config_path=config_path_str,
            original=original,
            configuration=configuration,
        )

    # Never the target id on the promote path (D23): main/ keeps the source
    # branch's id.
    promote = branch_path != branch_tree_path(manifest, branch_id)
    hashes = settle_local_files(
        service,
        captured,
        config_dir,
        component_id=component_id,
        config_id=config_id,
        file_config_id="" if promote else config_id,
        configuration=configuration,
        warnings=warnings,
    )
    if remap is not None:
        remap.keep_receipt(component_id, config_id, hashes)
    return UpdateOutcome(response=result, hashes=hashes)


def _keep_target_app_id(
    client: Any, config_id: str, branch_id: int | None, configuration: dict[str, Any]
) -> None:
    """Send the target branch's data-app id, never the one main/ names.

    A data app's ``parameters.id`` points at its Data Science app. On the
    promote path main/ keeps the production app id (D23), so an update read
    from main/ would re-point the dev branch's config at the production app.
    The id is read from the config on the target branch itself; a failed read
    fails this update rather than send the wrong id.
    """
    remote = client.get_config_detail(
        component_id=DATA_APP_COMPONENT_ID, config_id=config_id, branch_id=branch_id
    )
    target_params = ((remote or {}).get("configuration") or {}).get("parameters") or {}
    params = configuration.get("parameters")
    if target_params.get("id") and isinstance(params, dict):
        params["id"] = target_params["id"]


@dataclass(frozen=True)
class _EntryBefore:
    """The target entry of an update as it was before the update began (D3)."""

    entry: ManifestConfiguration | None
    metadata: dict[str, Any] | None

    def restore(self, manifest: Manifest, key: tuple[str, str], branch_id: int | None) -> None:
        """Undo what the update changed in the manifest (a checkpointed ``rebind_pending``)."""
        now = find_branch_entry(manifest, key[0], key[1], branch_id)
        if self.entry is None:
            if now is not None:
                manifest.configurations = [c for c in manifest.configurations if c is not now]
            return
        self.entry.metadata = copy.deepcopy(self.metadata or {})


def push_modified(
    service: SyncService,
    client: Any,
    change: dict[str, Any],
    *,
    project_root: Path,
    branch_path: str,
    manifest: Manifest,
    branch_id: int | None,
    held: set[tuple[str, str]],
    allow_plaintext_fallback: bool,
    warnings: list[dict[str, Any]],
    known_secrets: SecretCache,
    remap: PromoteRemap | None,
) -> None:
    """Push one ``modified`` config and stamp its manifest entry (Phase A).

    A failed update puts the config into *held*: no later phase of this push
    writes it (D4). A conflict also puts its manifest entry back as it was
    before the update -- the ``rebind_pending`` the promote path checkpoints
    before the PUT included: that PUT never happened.
    """
    component_id, config_id = change["component_id"], change["config_id"]
    config_path_str = change.get("path", "")
    key = (component_id, config_id)
    entry = find_branch_entry(manifest, component_id, config_id, branch_id)
    before = _EntryBefore(entry, copy.deepcopy(entry.metadata) if entry is not None else None)
    try:
        raise_on_legacy_boundary(
            service,
            client,
            component_id=component_id,
            config_id=config_id,
            config_dir=project_root / branch_path / config_path_str,
            manifest=manifest,
            branch_id=branch_id,
        )
        outcome = push_update(
            service,
            client,
            component_id,
            config_id,
            config_path_str,
            project_root,
            manifest,
            branch_id,
            allow_plaintext_fallback=allow_plaintext_fallback,
            warnings=warnings,
            known_secrets=known_secrets,
            remap=remap,
            remote_version=change.get("remote_version"),
        )
    except Exception as exc:
        held.add(key)
        if isinstance(exc, KeboolaApiError) and exc.error_code == ErrorCode.SYNC_CONFLICT:
            before.restore(manifest, key, branch_id)
        raise
    stamp_updated_config(
        client,
        manifest=manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        config_path_str=config_path_str,
        hashes=outcome.hashes,
        response=outcome.response,
        warnings=warnings,
        source_tree=branch_path,
        config_dir=project_root / branch_path / config_path_str,
    )
