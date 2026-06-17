"""Multi-project (all-projects) pull/diff/push orchestration (from sync_service.py).

Free functions taking the ``SyncService`` so the public ``pull_all`` / ``diff_all``
/ ``push_all`` methods stay thin delegators. Each fans out across the registered
projects in parallel, materialising one ``base_dir/<alias>/`` per project, and
collects per-project results without letting one project's failure abort the rest.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import (
    DEFAULT_JOBS_PER_CONFIG,
    DEFAULT_MAX_SAMPLES,
    DEFAULT_SAMPLE_LIMIT,
    KEBOOLA_DIR_NAME,
)
from ..errors import SyncConflictError

if TYPE_CHECKING:
    from .sync_service import SyncService


def pull_all(
    service: SyncService,
    base_dir: Path,
    force: bool = False,
    dry_run: bool = False,
    job_limit: int = DEFAULT_JOBS_PER_CONFIG,
    no_storage: bool = False,
    no_jobs: bool = False,
    with_samples: bool = False,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> dict[str, Any]:
    """Pull all registered projects in parallel.

    For each project, creates ``base_dir/<alias>/`` and initializes if no
    manifest exists yet, then pulls. Returns a dict with per-project results and
    a summary.
    """
    projects = service.resolve_projects(None)
    results: dict[str, Any] = {}
    success_count = 0
    failed_count = 0

    def _worker(alias: str) -> None:
        nonlocal success_count, failed_count
        project_root = base_dir / alias
        manifest_path = project_root / KEBOOLA_DIR_NAME / "manifest.json"
        try:
            if not manifest_path.exists():
                service.init_sync(alias, project_root)
            result = service.pull(
                alias,
                project_root,
                force=force,
                dry_run=dry_run,
                job_limit=job_limit,
                no_storage=no_storage,
                no_jobs=no_jobs,
                with_samples=with_samples,
                sample_limit=sample_limit,
                max_samples=max_samples,
            )
            results[alias] = result
            success_count += 1
        except SyncConflictError as exc:
            # Preserve the structured conflict so a programmatic / AI consumer of
            # `--all-projects --json` can tell a merge conflict apart from any
            # other error and read the conflicting configs -- the single-project
            # path emits the same code + conflicts.
            results[alias] = {
                "error": exc.message,
                "error_code": exc.error_code,
                "conflicts": exc.conflicts,
            }
            failed_count += 1
        except Exception as exc:
            results[alias] = {"error": str(exc)}
            failed_count += 1

    max_workers = min(len(projects), service._resolve_max_workers()) if projects else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, alias): alias for alias in projects}
        for future in as_completed(futures):
            # Exceptions are captured inside _worker; this catches truly
            # unexpected failures (e.g. threading errors).
            try:
                future.result()
            except Exception as exc:
                alias = futures[future]
                results[alias] = {"error": str(exc)}
                failed_count += 1

    total = len(projects)
    return {
        "projects": results,
        "summary": {
            "total": total,
            "success": success_count,
            "failed": failed_count,
        },
    }


def diff_all(service: SyncService, base_dir: Path) -> dict[str, Any]:
    """Diff all registered projects that have a local manifest.

    Projects without an existing manifest are skipped. Returns a dict with
    per-project diff results, a summary, and a skipped list.
    """
    projects = service.resolve_projects(None)
    results: dict[str, Any] = {}
    skipped: list[str] = []
    success_count = 0
    failed_count = 0

    actionable: list[str] = []
    for alias in projects:
        manifest_path = base_dir / alias / KEBOOLA_DIR_NAME / "manifest.json"
        if manifest_path.exists():
            actionable.append(alias)
        else:
            skipped.append(alias)

    def _worker(alias: str) -> None:
        nonlocal success_count, failed_count
        project_root = base_dir / alias
        try:
            results[alias] = service.diff(alias, project_root)
            success_count += 1
        except Exception as exc:
            results[alias] = {"error": str(exc)}
            failed_count += 1

    max_workers = min(len(actionable), service._resolve_max_workers()) if actionable else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, alias): alias for alias in actionable}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                alias = futures[future]
                results[alias] = {"error": str(exc)}
                failed_count += 1

    total = len(projects)
    return {
        "projects": results,
        "summary": {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "skipped": len(skipped),
        },
        "skipped": skipped,
    }


def push_all(
    service: SyncService,
    base_dir: Path,
    dry_run: bool = False,
    force: bool = False,
    allow_plaintext_fallback: bool = False,
) -> dict[str, Any]:
    """Push all registered projects that have a local manifest.

    Projects without an existing manifest are skipped. Returns a dict with
    per-project push results, a summary, and a skipped list.
    """
    projects = service.resolve_projects(None)
    results: dict[str, Any] = {}
    skipped: list[str] = []
    success_count = 0
    failed_count = 0

    actionable: list[str] = []
    for alias in projects:
        manifest_path = base_dir / alias / KEBOOLA_DIR_NAME / "manifest.json"
        if manifest_path.exists():
            actionable.append(alias)
        else:
            skipped.append(alias)

    def _worker(alias: str) -> None:
        nonlocal success_count, failed_count
        project_root = base_dir / alias
        try:
            result = service.push(
                alias,
                project_root,
                dry_run=dry_run,
                force=force,
                allow_plaintext_fallback=allow_plaintext_fallback,
            )
            results[alias] = result
            success_count += 1
        except Exception as exc:
            results[alias] = {"error": str(exc)}
            failed_count += 1

    max_workers = min(len(actionable), service._resolve_max_workers()) if actionable else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, alias): alias for alias in actionable}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                alias = futures[future]
                results[alias] = {"error": str(exc)}
                failed_count += 1

    total = len(projects)
    return {
        "projects": results,
        "summary": {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "skipped": len(skipped),
        },
        "skipped": skipped,
    }
