"""Configuration listing service - business logic for listing and detailing configs.

Orchestrates multi-project configuration retrieval, filtering, and aggregation
without knowing about CLI or HTTP details.
"""

from collections.abc import Callable
from typing import Any

from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..errors import ConfigError, KeboolaApiError
from ..models import ProjectConfig

ClientFactory = Callable[[str, str], KeboolaClient]


def default_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and token."""
    return KeboolaClient(stack_url=stack_url, token=token)


class ConfigService:
    """Business logic for listing and inspecting Keboola configurations.

    Supports multi-project aggregation: queries multiple projects in sequence,
    collects results, and reports per-project errors without stopping others.

    Uses dependency injection for config_store and client_factory to enable
    easy testing with mocks.
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
            aliases: Specific project aliases to resolve. If None or empty,
                     returns all configured projects.

        Returns:
            Dict mapping alias to ProjectConfig for the resolved projects.

        Raises:
            ConfigError: If any specified alias is not found in the config.
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

    def list_configs(
        self,
        aliases: list[str] | None = None,
        component_type: str | None = None,
        component_id: str | None = None,
    ) -> dict[str, Any]:
        """List configurations across one or multiple projects.

        Queries each resolved project for components and their configurations,
        flattens them into a unified list. Per-project errors are collected
        but do not stop other projects from being queried.

        Args:
            aliases: Project aliases to query. None means all projects.
            component_type: Optional filter by component type
                (extractor, writer, transformation, application).
            component_id: Optional filter by specific component ID
                (e.g. keboola.ex-db-snowflake).

        Returns:
            Dict with keys:
                - "configs": list of config dicts with project_alias,
                  component_id, component_name, component_type,
                  config_id, config_name, config_description
                - "errors": list of error dicts with project_alias,
                  error_code, message

        Raises:
            ConfigError: If a specified alias is not found (before querying).
        """
        projects = self.resolve_projects(aliases)

        all_configs: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for alias, project in projects.items():
            client = self._client_factory(project.stack_url, project.token)
            try:
                components = client.list_components(component_type=component_type)
                for component in components:
                    comp_id = component.get("id", "")
                    comp_name = component.get("name", "")
                    comp_type = component.get("type", "")

                    # Apply component_id filter if specified
                    if component_id and comp_id != component_id:
                        continue

                    configurations = component.get("configurations", [])
                    for cfg in configurations:
                        all_configs.append(
                            {
                                "project_alias": alias,
                                "component_id": comp_id,
                                "component_name": comp_name,
                                "component_type": comp_type,
                                "config_id": str(cfg.get("id", "")),
                                "config_name": cfg.get("name", ""),
                                "config_description": cfg.get("description", ""),
                            }
                        )
            except KeboolaApiError as exc:
                errors.append(
                    {
                        "project_alias": alias,
                        "error_code": exc.error_code,
                        "message": exc.message,
                    }
                )
            finally:
                client.close()

        return {"configs": all_configs, "errors": errors}

    def get_config_detail(
        self,
        alias: str,
        component_id: str,
        config_id: str,
    ) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            alias: Project alias to query.
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.

        Returns:
            Dict with the full configuration detail from the API,
            plus a "project_alias" key.

        Raises:
            ConfigError: If the alias is not found.
            KeboolaApiError: If the API call fails.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            detail = client.get_config_detail(component_id, config_id)
        finally:
            client.close()

        detail["project_alias"] = alias
        return detail
