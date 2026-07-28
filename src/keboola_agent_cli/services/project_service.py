"""Project management service - business logic for add/remove/edit/list/status.

Orchestrates config persistence and API calls without knowing about CLI or HTTP details.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config_store import project_not_found_error, validate_alias_format
from ..constants import ENV_KBAGENT_PROJECT
from ..errors import ConfigError, KeboolaApiError, mask_token
from ..models import ProjectConfig, normalize_stack_url
from .base import BaseService


class ProjectService(BaseService):
    """Business logic for managing Keboola project connections.

    Uses dependency injection for config_store and client_factory.
    """

    def add_project(self, alias: str, stack_url: str, token: str) -> dict[str, Any]:
        """Add a new project connection after verifying the token.

        Calls the Keboola API to verify the token and extract project info,
        then saves the project to the config store.

        Args:
            alias: Human-friendly project name.
            stack_url: Keboola stack URL.
            token: Storage API token.

        Returns:
            Dict with project details (alias, project_name, project_id, stack_url, masked_token).

        Raises:
            KeboolaApiError: If token verification fails.
            ConfigError: If the alias already exists.
        """
        # Accept a bare host or a full project deep-link, not just a clean base
        # URL -- normalize before we build the verification client so the token
        # check hits the right host (and the stored value is the clean base).
        stack_url = normalize_stack_url(stack_url)
        client = self._client_factory(stack_url, token)
        try:
            token_info = client.verify_token()
        finally:
            client.close()

        project = ProjectConfig(
            stack_url=stack_url,
            token=token,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
            org_id=token_info.org_id,
            org_name=token_info.org_name,
        )

        self._config_store.add_project(alias, project)

        return {
            "alias": alias,
            "project_name": token_info.project_name,
            "project_id": token_info.project_id,
            "stack_url": stack_url,
            "token": mask_token(token),
            "org_id": token_info.org_id,
            "org_name": token_info.org_name,
        }

    def remove_project(self, alias: str) -> dict[str, str]:
        """Remove a project from the configuration.

        Args:
            alias: The project alias to remove.

        Returns:
            Dict confirming the removal.

        Raises:
            ConfigError: If the alias does not exist.
        """
        self._config_store.remove_project(alias)
        return {"alias": alias, "message": f"Project '{alias}' removed."}

    def bulk_remove_projects(
        self,
        aliases: list[str],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove several projects in one call, accumulating per-alias errors.

        One failing alias (e.g. it does not exist, or is an ephemeral
        ``__env__`` project that cannot be removed) does not stop the others --
        the failure is recorded under ``failed`` and the rest proceed. Like the
        single-remove path this only edits ``config.json`` locally; no remote
        API call is made.

        Args:
            aliases: Project aliases to remove. Duplicates are de-duplicated
                while preserving order.
            dry_run: When True, validate each alias and report what WOULD be
                removed without mutating ``config.json``.

        Returns:
            ``{"removed": [...], "failed": [{"alias", "error"}], "dry_run": bool}``
            where ``removed`` lists the aliases removed (or that would be
            removed in dry-run mode).
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for alias in aliases:
            if alias not in seen:
                seen.add(alias)
                ordered.append(alias)

        removed: list[str] = []
        failed: list[dict[str, str]] = []
        for alias in ordered:
            try:
                if dry_run:
                    # Apply the SAME validation as the live remove (missing
                    # alias + ephemeral `__env__` guard) without mutating, so a
                    # dry-run never reports an alias as removable that the real
                    # run would reject.
                    self._config_store.ensure_removable(alias)
                    removed.append(alias)
                else:
                    self._config_store.remove_project(alias)
                    removed.append(alias)
            except ConfigError as exc:
                failed.append({"alias": alias, "error": exc.message})

        return {"removed": removed, "failed": failed, "dry_run": dry_run}

    def edit_project(
        self,
        alias: str,
        stack_url: str | None = None,
        token: str | None = None,
        new_alias: str | None = None,
        search_root: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Edit an existing project's configuration.

        If the token is changed, re-verifies it against the API to update
        project name and ID. If ``new_alias`` is provided and differs from
        ``alias``, the rename is applied first (config + nested sync dir);
        any subsequent url/token mutations target the new alias key.

        Args:
            alias: The project alias to edit.
            stack_url: New stack URL (if changing).
            token: New token (if changing).
            new_alias: Rename target. Skipped when None or equal to ``alias``.
            search_root: Workspace root to scan for a nested sync directory
                ``<search_root>/<alias>/.keboola/manifest.json``. Defaults
                to the current working directory at call time. Tests pass
                an explicit ``tmp_path`` to avoid touching the real fs.
            dry_run: If True, validate everything (existence, format,
                collision) and compute the planned cascade WITHOUT
                mutating any state. Result dict carries ``dry_run: True``
                and a ``planned`` sub-dict describing what would happen.
                Validation errors raise the same ``ConfigError``
                exceptions as the live path. Token re-verification is
                also skipped (no API call).

        Returns:
            Dict with updated project details. When a rename happened,
            includes ``old_alias`` and ``rename`` keys describing the
            cascade outcome. In dry-run mode the dict has
            ``dry_run: True`` and a ``planned`` sub-dict; ``alias``
            reports the ORIGINAL alias (no mutation occurred).

        Raises:
            KeboolaApiError: If token re-verification fails (live mode only).
            ConfigError: If the alias does not exist, no changes provided,
                or ``new_alias`` is invalid / collides.
        """
        existing = self._config_store.get_project(alias)
        if existing is None:
            raise project_not_found_error(
                alias, self._config_store.config_path, self._config_store.source
            )

        # ``--new-alias`` matching the current alias is treated as no change
        # (rename-to-same-name idempotency). Combined with no url/token, it
        # is the same surface as "user typed the command but specified
        # nothing" -- raise the same error so the UX is consistent.
        new_alias_is_change = new_alias is not None and new_alias != alias
        if stack_url is None and token is None and not new_alias_is_change:
            raise ConfigError(
                "No changes specified. Provide --url, --token, and/or "
                "--new-alias (matching the current alias is a no-op)."
            )

        # Normalize a bare host / full project deep-link to the clean base URL
        # up front so the dry-run preview and the real edit agree on the value.
        if stack_url is not None:
            stack_url = normalize_stack_url(stack_url)

        # ----- dry-run path: validate everything, mutate nothing ----------
        if dry_run:
            planned_rename: dict[str, Any] | None = None
            if new_alias is not None and new_alias != alias:
                planned_rename = self._plan_project_alias_rename(
                    old_alias=alias,
                    new_alias=new_alias,
                    search_root=search_root if search_root is not None else Path.cwd(),
                )
            return {
                "alias": alias,
                "project_name": existing.project_name,
                "project_id": existing.project_id,
                "stack_url": stack_url if stack_url is not None else existing.stack_url,
                "token": mask_token(existing.token),
                "dry_run": True,
                "planned": {
                    "old_alias": alias,
                    "new_alias": new_alias if new_alias_is_change else None,
                    "stack_url_would_change": stack_url is not None
                    and stack_url != existing.stack_url,
                    "token_would_change": token is not None,
                    "rename": planned_rename,
                },
            }

        rename_result: dict[str, Any] | None = None
        original_alias = alias
        if new_alias is not None and new_alias != alias:
            rename_result = self._rename_project_alias(
                old_alias=alias,
                new_alias=new_alias,
                search_root=search_root if search_root is not None else Path.cwd(),
            )
            alias = new_alias  # subsequent url/token updates target the new key

        updates: dict[str, str | int] = {}

        if stack_url is not None:
            updates["stack_url"] = stack_url

        if token is not None:
            effective_url = stack_url if stack_url is not None else existing.stack_url
            client = self._client_factory(effective_url, token)
            try:
                token_info = client.verify_token()
            finally:
                client.close()
            updates["token"] = token
            updates["project_name"] = token_info.project_name
            if token_info.project_id is not None:
                updates["project_id"] = token_info.project_id

        if updates:
            self._config_store.edit_project(alias, **updates)

        updated = self._config_store.get_project(alias)
        if updated is None:
            raise ConfigError(
                f"Project '{alias}' could not be retrieved after editing. "
                "Config store may be in an inconsistent state."
            )

        result: dict[str, Any] = {
            "alias": alias,
            "project_name": updated.project_name,
            "project_id": updated.project_id,
            "stack_url": updated.stack_url,
            "token": mask_token(updated.token),
        }
        if rename_result is not None:
            result["old_alias"] = original_alias
            result["rename"] = rename_result
        return result

    def _rename_project_alias(
        self,
        *,
        old_alias: str,
        new_alias: str,
        search_root: Path,
    ) -> dict[str, Any]:
        """Rename a project alias across config.json and nested sync dirs.

        Order of operations and rollback contract (review iter 2 -- bugs
        S1 + S2):

        1. Format validation rejects path-traversal characters BEFORE any
           I/O. ``..``, ``/``, ``\\``, NUL, leading ``.`` / ``-``, and
           anything outside ``[A-Za-z0-9_.-]`` are rejected here, so the
           subsequent filesystem path computation cannot escape
           ``search_root``.
        2. ``search_root`` is resolved once via ``Path.resolve()`` to
           collapse symlinks; the disk rename uses the resolved path
           throughout, closing the door on a malicious symlink at
           ``<cwd>/<new-alias>/`` redirecting the move target.
        3. Config-side rename (`ConfigStore.rename_project`) commits the
           ``projects`` dict-key swap and the ``default_project`` cascade
           atomically (one ``save()`` call). On collision it raises
           BEFORE any disk op runs.
        4. The optional disk rename (`_rename_nested_sync_dir`) is wrapped
           in a rollback: any ``OSError`` reverses the config rename via
           a second ``rename_project(new, old)`` call so config and disk
           never end up out of sync. The original exception is re-raised
           wrapped in ``ConfigError`` so the user sees an actionable
           message.
        5. Lineage cache (`*.lineage.json` files in ``search_root``) is
           NOT rewritten -- the cache embeds ``<alias>:<table_id>`` FQNs,
           may live in a sibling git repo, may be committed to disk; a
           partial rewrite is worse than no rewrite. The scan is
           depth-capped at 2 levels to bound cost when ``search_root`` is
           a deep tree (S4).

        Project alias rename is purely local -- there is no Keboola API
        counterpart to audit, so unlike ``ConfigService.rename_config``
        this method writes no change-description string.

        Returns a dict describing the cascade:
            {
                "old_alias": str,
                "new_alias": str,
                "default_project_updated": bool,
                "sync_dir": {"old_path": str, "new_path": str, "method": str} | None,
                "lineage_cache_warning": str | None,
            }
        """
        # 1. Format validation. Rejects path-traversal characters and
        #    anything that would make a confusing dict key. Fail-fast,
        #    no I/O.
        self._validate_alias_format(new_alias)

        # 2. Resolve search_root once (collapses symlinks -- defense
        #    against a malicious cwd that aliases an unrelated tree).
        try:
            resolved_root = search_root.resolve()
        except (OSError, RuntimeError) as exc:  # RuntimeError = symlink loop
            raise ConfigError(f"Cannot resolve search_root '{search_root}': {exc}") from exc

        # 3. Capture pre-state. config_store.rename_project raises on
        #    collision before mutating, so this read is safe.
        pre_config = self._config_store.load()
        default_was_match = pre_config.default_project == old_alias

        # 4. Atomic config-side rename (collision -> ConfigError, no disk op).
        self._config_store.rename_project(old_alias, new_alias)

        # 5. Optional disk-side rename, with rollback on OS-level failure.
        try:
            sync_dir_result = self._rename_nested_sync_dir(
                old_alias=old_alias, new_alias=new_alias, search_root=resolved_root
            )
        except OSError as exc:
            # Disk move failed; roll the config rename back so config
            # and disk stay in sync. The rollback's own failure is
            # logged-and-swallowed to surface the original cause.
            import contextlib

            with contextlib.suppress(ConfigError):
                self._config_store.rename_project(new_alias, old_alias)
            raise ConfigError(
                f"Failed to rename sync directory for '{old_alias}' -> "
                f"'{new_alias}': {exc}. Config rolled back to original alias."
            ) from exc

        # 6. Lineage cache detection -> warn (no rewrite).
        lineage_warning = self._detect_lineage_cache_warning(resolved_root, old_alias)

        result: dict[str, Any] = {
            "old_alias": old_alias,
            "new_alias": new_alias,
            "default_project_updated": default_was_match,
            "sync_dir": sync_dir_result,
            "lineage_cache_warning": lineage_warning,
        }
        if lineage_warning is not None:
            sys.stderr.write(lineage_warning + "\n")
        return result

    @staticmethod
    def _validate_alias_format(new_alias: str) -> None:
        """Reject filesystem-unsafe ``--new-alias`` values.

        Delegates to the shared ``config_store.validate_alias_format`` (added
        for the ``auth register-projects`` picker, 0.77.0) so ``project edit
        --new-alias`` and the picker can never drift into accepting different
        alias character sets for the same config.json key. Error messages are
        byte-identical to the pre-delegation version (same ``field`` label).

        Stricter than the no-op check ``project add`` performs today
        (none) because rename uses ``new_alias`` as a directory name.
        Forbidden inputs:

        - empty / whitespace-only;
        - any whitespace anywhere;
        - the substring ``..`` (path-traversal in any position);
        - characters outside ``[A-Za-z0-9_.-]`` (catches ``/``, ``\\``,
          NUL, control chars, Unicode letters);
        - leading ``.`` or ``-`` (would surprise CLI parsing or hide as
          a dotfile).
        """
        validate_alias_format(new_alias, field="--new-alias")

    @staticmethod
    def _rename_nested_sync_dir(
        *,
        old_alias: str,
        new_alias: str,
        search_root: Path,
    ) -> dict[str, str] | None:
        """Move ``<search_root>/<old_alias>/`` to ``<search_root>/<new_alias>/``.

        Skips silently when the source directory does not contain a
        ``.keboola/manifest.json`` (i.e. it is not a kbagent sync
        workspace). Mirrors the collision-suffix and git-mv-with-fallback
        pattern of :meth:`ConfigService._rename_sync_directory`.

        Caller is responsible for ``search_root.resolve()``-ing first
        and for validating ``new_alias`` via
        :meth:`_validate_alias_format` -- this helper trusts both inputs.
        """
        from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME

        source_dir = search_root / old_alias
        manifest_path = source_dir / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
        if not manifest_path.exists():
            return None  # No nested sync workspace; nothing to rename on disk.

        # Collision detection: append -2, -3, ... if the target exists or
        # is a symlink (covers both real-dir and symlink-to-elsewhere cases).
        target_dir = search_root / new_alias
        if target_dir.exists() or target_dir.is_symlink():
            counter = 2
            while True:
                candidate = search_root / f"{new_alias}-{counter}"
                if not candidate.exists() and not candidate.is_symlink():
                    target_dir = candidate
                    break
                counter += 1

        method = ProjectService._move_directory(source_dir, target_dir)
        return {
            "old_path": str(source_dir),
            "new_path": str(target_dir),
            "method": method,
        }

    @staticmethod
    def _move_directory(source: Path, target: Path) -> str:
        """Move ``source`` to ``target``; prefer ``git mv`` for cleaner history.

        Returns the method string (``"git_mv"`` or ``"shutil_move"``) used.
        Mirrors :meth:`ConfigService._move_directory` -- return strings
        are kept identical so JSON consumers parsing the ``method`` key
        across both rename surfaces see the same vocabulary.
        """
        try:
            result = subprocess.run(
                ["git", "mv", str(source), str(target)],
                capture_output=True,
                text=True,
                cwd=str(source.parent),
                check=False,
            )
            if result.returncode == 0:
                return "git_mv"
        except (FileNotFoundError, OSError):
            pass
        shutil.move(str(source), str(target))
        return "shutil_move"

    @staticmethod
    def _detect_lineage_cache_warning(search_root: Path, old_alias: str) -> str | None:
        """Return a stderr-warning string if a ``*.lineage.json`` exists.

        Depth-capped at 2 levels (top + 2 subdir levels) to bound cost
        when ``search_root`` is a large tree (e.g. ``$HOME``) -- iter 2
        review S4. Lineage caches typically live at the workspace root
        or one level deep next to nested project dirs; deeper scans are
        unbounded and risk symlink loops.

        We do not parse the JSON or attempt a rewrite -- lineage caches
        embed the alias inside FQN strings, can live anywhere, and may
        be committed to a sibling git repo; partial rewrites are worse
        than no rewrite. Surfacing the manual rebuild step is enough.
        """
        try:
            if not search_root.is_dir():
                return None
            patterns = ("*.lineage.json", "*/*.lineage.json", "*/*/*.lineage.json")
            for pattern in patterns:
                for p in search_root.glob(pattern):
                    # Bail out on first hit; one warning suffices.
                    return (
                        f"Warning: lineage cache file detected at "
                        f"'{p}'. The cache embeds the old alias "
                        f"'{old_alias}' in FQN strings and is NOT auto-updated "
                        f"by this rename. Run 'kbagent lineage build --output X' "
                        f"to rebuild it against the new alias."
                    )
        except (OSError, PermissionError):
            return None
        return None

    def _plan_project_alias_rename(
        self,
        *,
        old_alias: str,
        new_alias: str,
        search_root: Path,
    ) -> dict[str, Any]:
        """Read-only dry-run for ``_rename_project_alias``.

        Mirrors the live-path validation order (format check, then
        ``search_root`` resolve, then collision check via a config load)
        so the same ``ConfigError`` exceptions surface for the same
        inputs. Skips both mutations: no ``rename_project()`` call, no
        ``_rename_nested_sync_dir()`` call. Predicts the disk-move shape
        (target dir, collision suffix, ``planned_method``) by inspecting
        the filesystem read-only. The lineage cache warning string is
        included in the result but NOT written to stderr (planning
        shouldn't emit user-facing warnings yet).
        """
        # 1. Same validators as the live path.
        self._validate_alias_format(new_alias)

        try:
            resolved_root = search_root.resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"Cannot resolve search_root '{search_root}': {exc}") from exc

        # 2. Collision check via a read-only load (no rename_project mutation).
        config = self._config_store.load()
        if old_alias not in config.projects:
            raise project_not_found_error(
                old_alias, self._config_store.config_path, self._config_store.source
            )
        if new_alias in config.projects:
            raise ConfigError(
                f"Cannot rename '{old_alias}' to '{new_alias}': "
                f"alias '{new_alias}' is already in use."
            )

        default_would_update = config.default_project == old_alias

        # 3. Sync-dir probe (read-only).
        sync_dir_planned = self._plan_nested_sync_dir(
            old_alias=old_alias, new_alias=new_alias, search_root=resolved_root
        )

        # 4. Lineage cache scan -- string returned, NOT written to stderr.
        lineage_warning = self._detect_lineage_cache_warning(resolved_root, old_alias)

        return {
            "old_alias": old_alias,
            "new_alias": new_alias,
            "default_project_would_update": default_would_update,
            "sync_dir_would_move": sync_dir_planned,
            "lineage_cache_warning": lineage_warning,
        }

    @staticmethod
    def _plan_nested_sync_dir(
        *,
        old_alias: str,
        new_alias: str,
        search_root: Path,
    ) -> dict[str, Any] | None:
        """Read-only counterpart of ``_rename_nested_sync_dir``.

        Returns the planned move shape (target path, collision suffix,
        method) without touching disk. Returns ``None`` when no nested
        sync workspace exists at ``<search_root>/<old_alias>/``.
        """
        from ..constants import KEBOOLA_DIR_NAME, MANIFEST_FILENAME

        source_dir = search_root / old_alias
        manifest_path = source_dir / KEBOOLA_DIR_NAME / MANIFEST_FILENAME
        if not manifest_path.exists():
            return None

        target_dir = search_root / new_alias
        suffix: str | None = None
        if target_dir.exists() or target_dir.is_symlink():
            counter = 2
            while True:
                candidate = search_root / f"{new_alias}-{counter}"
                if not candidate.exists() and not candidate.is_symlink():
                    target_dir = candidate
                    suffix = f"-{counter}"
                    break
                counter += 1

        # Predict whether ``git mv`` would succeed: requires (a) ``git`` on
        # PATH and (b) the source dir lives inside a git working tree.
        # Heuristic -- the live path falls back to ``shutil.move`` when
        # ``git mv`` returns nonzero, so the dry-run prediction is best-
        # effort, not a contract.
        planned_method = "shutil_move"
        if shutil.which("git"):
            ancestor: Path | None = source_dir
            while ancestor is not None and ancestor != ancestor.parent:
                if (ancestor / ".git").exists():
                    planned_method = "git_mv"
                    break
                ancestor = ancestor.parent

        return {
            "old_path": str(source_dir),
            "planned_new_path": str(target_dir),
            "planned_method": planned_method,
            "collision_suffix": suffix,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        """List all configured projects.

        Returns:
            List of dicts with project details (token masked).
        """
        config = self._config_store.load()
        result = []
        for alias, project in config.projects.items():
            result.append(
                {
                    "alias": alias,
                    "project_name": project.project_name,
                    "project_id": project.project_id,
                    "stack_url": project.stack_url,
                    "token": mask_token(project.token),
                    "is_default": alias == config.default_project,
                    "active_branch_id": project.active_branch_id,
                    "org_id": project.org_id,
                    "org_name": project.org_name,
                }
            )
        return result

    def _backfill_org_info(
        self,
        successes: list[tuple[str, dict[str, Any], bool]],
    ) -> None:
        """Persist newly-discovered org info for projects that lack it.

        Runs serially after the parallel status check so we never write to the
        config JSON from multiple worker threads. A single ``load() -> mutate
        -> save()`` pass updates every alias whose stored org info is empty
        but verify_token just returned a populated organization.

        No-op when no project needs an update -- common steady-state once the
        backfill has run once.
        """
        # Build the update set first; skip the file write entirely if empty.
        updates: dict[str, tuple[int | None, str | None]] = {}
        for _alias, status_entry, _flag in successes:
            if status_entry.get("status") != "ok":
                continue
            alias = status_entry["alias"]
            new_id = status_entry.get("org_id")
            new_name = status_entry.get("org_name")
            if new_id is None and not new_name:
                continue  # verify_token didn't return org info -- nothing to backfill
            current = self._config_store.get_project(alias)
            if current is None:
                continue
            if current.ephemeral:
                # Env-synthesized __env__ (issue #359): its org info can never
                # be persisted (save() strips it), so backfilling is futile and
                # would trigger a spurious config.json write on disk -- breaking
                # the "no config.json in headless mode" guarantee and repeating
                # on every `project status`. Skip it.
                continue
            if current.org_id is not None and current.org_name:
                continue  # already populated; skip
            updates[alias] = (new_id, new_name)

        if not updates:
            return

        # Single transactional pass: load once, mutate all in-memory, save
        # once -- under the exclusive config lock so a concurrent kbagent
        # process cannot interleave its own read-modify-write (issue #477).
        with self._config_store.transaction():
            config = self._config_store.load()
            for alias, (new_id, new_name) in updates.items():
                if alias not in config.projects:
                    continue
                project = config.projects[alias]
                if project.org_id is None and new_id is not None:
                    project.org_id = new_id
                if not project.org_name and new_name:
                    project.org_name = new_name
            self._config_store.save(config)

    def _check_project_status(
        self, alias: str, project: ProjectConfig
    ) -> tuple[str, dict[str, Any], bool] | tuple[str, dict[str, str]]:
        """Check connectivity for a single project (runs in a worker thread).

        Creates its own KeboolaClient, verifies the token, and measures response time.
        Returns (alias, status_entry) on both success AND KeboolaApiError (since an
        API error still produces a valid status entry with status="error").
        Only truly unexpected exceptions return a 2-tuple error.

        Note: For this worker, both success and KeboolaApiError return 3-tuples
        (alias, status_entry, True) to distinguish from error 2-tuples in _run_parallel.
        """
        status_entry: dict[str, Any] = {
            "alias": alias,
            "stack_url": project.stack_url,
            "token": mask_token(project.token),
            "active_branch_id": project.active_branch_id,
        }

        client = self._client_factory(project.stack_url, project.token)
        start_time = time.monotonic()
        try:
            token_info = client.verify_token()
            elapsed = time.monotonic() - start_time
            status_entry["status"] = "ok"
            status_entry["response_time_ms"] = round(elapsed * 1000)
            status_entry["project_name"] = token_info.project_name
            status_entry["project_id"] = token_info.project_id
            # Carry org info through to the aggregator so get_status() can
            # opportunistically backfill projects registered before owner.
            # organization was being captured (e.g. before #290).
            status_entry["org_id"] = token_info.org_id
            status_entry["org_name"] = token_info.org_name
            return (alias, status_entry, True)
        except KeboolaApiError as exc:
            elapsed = time.monotonic() - start_time
            status_entry["status"] = "error"
            status_entry["response_time_ms"] = round(elapsed * 1000)
            status_entry["error"] = exc.message
            status_entry["error_code"] = exc.error_code
            return (alias, status_entry, True)
        except Exception as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    def get_status(self, aliases: list[str] | None = None) -> list[dict[str, Any]]:
        """Check connectivity status for one or more projects.

        For each project, verifies the token against the API and measures
        response time in parallel using ThreadPoolExecutor.

        Args:
            aliases: Specific project aliases to check (None = all projects).

        Returns:
            List of dicts with status, response time, and project details.

        Raises:
            ConfigError: If a specified alias does not exist.
        """
        projects = self.resolve_projects(aliases)

        successes, errors = self._run_parallel(projects, self._check_project_status)

        # Extract status entries from successes (3-tuples: alias, status_entry, True)
        results: list[dict[str, Any]] = []
        for _alias, status_entry, _flag in successes:
            results.append(status_entry)

        # Opportunistic org-info backfill: projects registered before #290 have
        # org_id/org_name=None in config.json even though verify_token now
        # returns them. Apply updates serially here -- _run_parallel uses worker
        # threads and concurrent ConfigStore writes would race on the JSON file.
        self._backfill_org_info(successes)

        # Convert unexpected errors to status entries
        for error in errors:
            results.append(
                {
                    "alias": error["project_alias"],
                    "stack_url": "",
                    "token": "",
                    "status": "error",
                    "response_time_ms": 0,
                    "error": error["message"],
                    "error_code": error["error_code"],
                }
            )

        # Sort for deterministic output
        results.sort(key=lambda r: r.get("alias", ""))

        return results

    def use_project(self, alias: str) -> dict[str, Any]:
        """Pin an alias as the persistent default project.

        The pin is stored as ``config.default_project`` in config.json.
        It is overridden at runtime by the ``KBAGENT_PROJECT`` env var and by
        explicit ``--project`` flags.

        Args:
            alias: The project alias to pin.

        Returns:
            Dict with the new pin, previous pin, and source.

        Raises:
            ConfigError: If the alias does not exist.
        """
        with self._config_store.transaction():
            config = self._config_store.load()
            if alias not in config.projects:
                raise project_not_found_error(
                    alias, self._config_store.config_path, self._config_store.source
                )

            previous = config.default_project or None
            config.default_project = alias
            self._config_store.save(config)

        env_override = os.environ.get(ENV_KBAGENT_PROJECT)
        return {
            "alias": alias,
            "previous": previous,
            "source": "pin",
            "env_override": env_override or None,
        }

    def current_project(self) -> dict[str, Any]:
        """Report the effective default project and its source.

        Resolution:
        - If ``KBAGENT_PROJECT`` is set, it wins (source=env).
        - Otherwise the persisted pin wins (source=pin).
        - If neither is set, ``alias`` is ``None``.

        The env override is reported even when it points at a project that is
        not (yet) registered in config.json -- callers get the true effective
        alias plus an ``env_points_to_configured_project`` flag to reason about
        it. This avoids silently masking misconfigurations.

        Returns:
            Dict with keys: alias, source ('env' | 'pin' | 'none'), pinned,
            env_override, env_points_to_configured_project.
        """
        config = self._config_store.load()
        pinned = config.default_project or None
        # Treat KBAGENT_PROJECT="" the same as unset (Unix shell convention:
        # empty env is commonly produced by `unset` substitutes / blank
        # exports). Strict rejection would surprise CI users who export it
        # conditionally. Apply consistently in resolve_pinned_alias().
        env_value = os.environ.get(ENV_KBAGENT_PROJECT)
        env_override = env_value if env_value else None

        if env_override is not None:
            return {
                "alias": env_override,
                "source": "env",
                "pinned": pinned,
                "env_override": env_override,
                "env_points_to_configured_project": env_override in config.projects,
            }

        return {
            "alias": pinned,
            "source": "pin" if pinned else "none",
            "pinned": pinned,
            "env_override": None,
            "env_points_to_configured_project": None,
        }

    def get_info(self, alias: str) -> dict[str, Any]:
        """Return detailed project metadata for a single project.

        Calls /v2/storage/tokens/verify and formats the full response
        into a structured dict suitable for both JSON and human output.

        Args:
            alias: The project alias to query.

        Returns:
            Dict with project_id, project_name, stack_url, default_backend,
            features, limits, metrics, token_id, token_description,
            is_master_token, token_expires, and description fields.

        Raises:
            ConfigError: If the alias does not exist.
            KeboolaApiError: If the API call fails.
        """
        project = self._config_store.get_project(alias)
        if project is None:
            raise project_not_found_error(
                alias, self._config_store.config_path, self._config_store.source
            )

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw = client.get_project_info()
        finally:
            client.close()

        owner = raw.get("owner", {})
        return {
            "alias": alias,
            "project_id": owner.get("id"),
            "project_name": owner.get("name", ""),
            "stack_url": project.stack_url,
            "default_backend": owner.get("defaultBackend", "snowflake"),
            "features": owner.get("features", []),
            "limits": owner.get("limits", {}),
            "metrics": owner.get("metrics", {}),
            "token_id": str(raw.get("id", "")),
            "token_description": raw.get("description", ""),
            "is_master_token": raw.get("isMasterToken", False),
            "token_expires": raw.get("expires"),
        }

    def resolve_pinned_alias(self, explicit: str | None = None) -> tuple[str, str]:
        """Resolve the effective project alias for a single-project operation.

        Precedence (first match wins):
        1. ``explicit`` argument (typically the CLI ``--project`` flag)
        2. ``KBAGENT_PROJECT`` env var
        3. Persisted ``default_project`` pin
        4. If exactly one project is registered, fall back to it (source=sole)
        5. Fail hard with ConfigError

        This is the single-project analog of ``resolve_projects()`` (which
        fans out to all projects). Use this from write/destructive command
        paths where fan-out would be surprising or unsafe.

        Args:
            explicit: Explicit alias from a CLI flag, or None.

        Returns:
            Tuple of (alias, source).

        Raises:
            ConfigError: If the resolved alias is not registered, or if none
                can be resolved.
        """
        config = self._config_store.load()

        if explicit:
            if explicit not in config.projects:
                raise project_not_found_error(
                    explicit, self._config_store.config_path, self._config_store.source
                )
            return explicit, "explicit"

        env_value = os.environ.get(ENV_KBAGENT_PROJECT)
        if env_value:
            if env_value not in config.projects:
                raise ConfigError(
                    f"{ENV_KBAGENT_PROJECT}='{env_value}' points to a project "
                    "that is not registered. Use 'kbagent project add' or "
                    "unset the env var."
                )
            return env_value, "env"

        pinned = config.default_project
        if pinned:
            if pinned not in config.projects:
                raise ConfigError(
                    f"Pinned default project '{pinned}' is not registered. "
                    "Run 'kbagent project use <alias>' to repair."
                )
            return pinned, "pin"

        if len(config.projects) == 1:
            (sole,) = config.projects.keys()
            return sole, "sole"

        if not config.projects:
            raise ConfigError("No projects configured. Run 'kbagent project add' first.")

        raise ConfigError(
            "Multiple projects configured and no default pinned. "
            "Pass --project <alias>, set KBAGENT_PROJECT, or run "
            "'kbagent project use <alias>'."
        )
