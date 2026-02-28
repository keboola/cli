"""Base service - shared infrastructure for multi-project parallel operations.

Provides resolve_projects(), worker pool management, and _run_parallel()
scaffold used by ConfigService, JobService, ProjectService, and LineageService.
"""

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..constants import ENV_MAX_PARALLEL_WORKERS
from ..errors import ConfigError
from ..models import ProjectConfig

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str, str], KeboolaClient]


def default_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and token."""
    return KeboolaClient(stack_url=stack_url, token=token)


class BaseService:
    """Shared base for services that operate across multiple projects.

    Provides:
    - resolve_projects(): resolve aliases to ProjectConfig instances
    - _resolve_max_workers(): env var > config.json > default (10)
    - _run_parallel(): ThreadPoolExecutor scaffold for multi-project operations

    Uses dependency injection for config_store and client_factory.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or default_client_factory

    def resolve_projects(self, aliases: list[str] | None = None) -> dict[str, ProjectConfig]:
        """Resolve project aliases to ProjectConfig instances.

        Args:
            aliases: Specific project aliases. If None or empty, returns all.

        Returns:
            Dict mapping alias to ProjectConfig.

        Raises:
            ConfigError: If any specified alias is not found.
        """
        config = self._config_store.load()

        if not aliases:
            return dict(config.projects)

        resolved: dict[str, ProjectConfig] = {}
        for alias in aliases:
            if alias not in config.projects:
                raise ConfigError(f"Project '{alias}' not found.")
            resolved[alias] = config.projects[alias]

        return resolved

    def _resolve_max_workers(self) -> int:
        """Resolve max parallel workers: env var > config.json > default (10).

        Returns:
            Positive integer for ThreadPoolExecutor max_workers.
        """
        env_val = os.environ.get(ENV_MAX_PARALLEL_WORKERS)
        if env_val is not None:
            try:
                val = int(env_val)
                if val > 0:
                    return val
            except ValueError:
                pass

        config = self._config_store.load()
        return config.max_parallel_workers

    def _run_parallel(
        self,
        projects: dict[str, ProjectConfig],
        worker_fn: Callable[[str, ProjectConfig], tuple[Any, ...]],
    ) -> tuple[list[tuple[Any, ...]], list[dict[str, str]]]:
        """Run a worker function across projects in parallel using ThreadPoolExecutor.

        Each worker_fn receives (alias, project) and returns either:
        - A 3+-tuple on success (first element is alias)
        - A 2-tuple (alias, error_dict) on failure

        The distinction is made by tuple length: len == 2 means error.

        Args:
            projects: Dict mapping alias to ProjectConfig.
            worker_fn: Callable that processes a single project.

        Returns:
            Tuple of (successes, errors) where:
            - successes: list of 3+-tuples from successful workers
            - errors: list of error dicts with project_alias, error_code, message
        """
        if not projects:
            return [], []

        successes: list[tuple[Any, ...]] = []
        errors: list[dict[str, str]] = []

        max_workers = min(len(projects), self._resolve_max_workers())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_alias = {
                executor.submit(worker_fn, alias, project): alias
                for alias, project in projects.items()
            }

            for future in as_completed(future_to_alias):
                try:
                    result = future.result()
                except Exception as exc:
                    proj_alias = future_to_alias[future]
                    logger.debug(
                        "Worker error for project '%s': %s", proj_alias, exc,
                    )
                    errors.append(
                        {
                            "project_alias": proj_alias,
                            "error_code": "UNEXPECTED_ERROR",
                            "message": str(exc),
                        }
                    )
                    continue

                if len(result) == 2:
                    _alias, error_dict = result
                    errors.append(error_dict)
                else:
                    successes.append(result)

        return successes, errors
