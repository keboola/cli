"""Component configuration endpoints (list, detail, update, search, rows, metadata, OAuth)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/configs", tags=["configs"])


class ConfigUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    configuration: dict[str, Any] | None = None
    set_paths: list[tuple[str, Any]] | None = None
    merge: bool = False
    dry_run: bool = False
    branch_id: int | None = None


class ConfigCreateRow(BaseModel):
    name: str
    description: str | None = None
    configuration: dict[str, Any] | None = None
    is_disabled: bool = False
    branch_id: int | None = None


class ConfigUpdateRow(BaseModel):
    name: str | None = None
    description: str | None = None
    configuration: dict[str, Any] | None = None
    is_disabled: bool | None = None
    branch_id: int | None = None


class ConfigCreate(BaseModel):
    name: str
    description: str | None = None
    configuration: dict[str, Any] | None = None
    branch_id: int | None = None


class SetDefaultBucket(BaseModel):
    bucket: str | None = None
    clear: bool = False
    dry_run: bool = False
    branch_id: int | None = None


class RenameConfig(BaseModel):
    name: str
    branch_id: int | None = None
    directory: str | None = None


class MetadataSet(BaseModel):
    value: str


@router.get("", summary="List component configurations")
def list_configs(
    project: str | None = Query(None, description="Project alias (None = all)"),
    component_type: str | None = None,
    component_id: str | None = None,
    branch_id: int | None = None,
    include_rows: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List component configurations across projects. Mirrors `kbagent config list`."""
    aliases = [project] if project else None
    return registry.config.list_configs(
        aliases=aliases,
        component_type=component_type,
        component_id=component_id,
        branch_id=branch_id,
        include_rows=include_rows,
    )


@router.get("/search", summary="Search configurations by pattern")
def search_configs(
    query: str,
    project: str | None = None,
    component_type: str | None = None,
    ignore_case: bool = False,
    regex: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Search component configurations by substring or regex. Mirrors `kbagent config search`."""
    aliases = [project] if project else None
    return registry.config.search_configs(
        query=query,
        aliases=aliases,
        component_type=component_type,
        ignore_case=ignore_case,
        regex=regex,
        branch_id=branch_id,
    )


@router.get("/{project}/{component_id}/{config_id}", summary="Get configuration detail")
def config_detail(
    project: str,
    component_id: str,
    config_id: str,
    branch_id: int | None = None,
    with_state: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch a single configuration. Mirrors `kbagent config detail`."""
    return registry.config.get_config_detail(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
        with_state=with_state,
    )


@router.patch("/{project}/{component_id}/{config_id}", summary="Update a configuration")
def config_update(
    project: str,
    component_id: str,
    config_id: str,
    body: ConfigUpdate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Update a configuration name, description, or content. Mirrors `kbagent config update`."""
    return registry.config.update_config(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        name=body.name,
        description=body.description,
        configuration=body.configuration,
        set_paths=body.set_paths,
        merge=body.merge,
        dry_run=body.dry_run,
        branch_id=body.branch_id,
    )


@router.delete("/{project}/{component_id}/{config_id}", summary="Delete a configuration")
def config_delete(
    project: str,
    component_id: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a component configuration."""
    return registry.config.delete_config(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
    )


@router.post("/{project}/{component_id}", summary="Create a configuration")
def config_create(
    project: str,
    component_id: str,
    body: ConfigCreate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create a new configuration for a component. Mirrors `kbagent config new`."""
    return registry.config.create_config(
        alias=project,
        component_id=component_id,
        name=body.name,
        description=body.description,
        configuration=body.configuration,
        branch_id=body.branch_id,
    )


@router.post(
    "/{project}/{component_id}/{config_id}/set-default-bucket",
    summary="Set or clear default bucket",
)
def config_set_default_bucket(
    project: str,
    component_id: str,
    config_id: str,
    body: SetDefaultBucket,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set or clear a configuration's default bucket. Mirrors `kbagent config set-default-bucket`."""
    return registry.config.set_default_bucket(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        bucket=body.bucket,
        clear=body.clear,
        dry_run=body.dry_run,
        branch_id=body.branch_id,
    )


@router.post("/{project}/{component_id}/{config_id}/rename", summary="Rename a configuration")
def config_rename(
    project: str,
    component_id: str,
    config_id: str,
    body: RenameConfig,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Rename a configuration. Mirrors `kbagent config rename`."""
    return registry.config.rename_config(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        new_name=body.name,
        branch_id=body.branch_id,
        directory=body.directory,
    )


@router.get("/{project}/{component_id}/{config_id}/metadata", summary="List configuration metadata")
def metadata_list(
    project: str,
    component_id: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List metadata entries on a configuration. Mirrors `kbagent config metadata-list`."""
    return registry.config.list_config_metadata(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
    )


@router.get(
    "/{project}/{component_id}/{config_id}/metadata/{key}",
    summary="Get a metadata value",
)
def metadata_get(
    project: str,
    component_id: str,
    config_id: str,
    key: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Read a single metadata value by key. Mirrors `kbagent config get-metadata`."""
    return registry.config.get_config_metadata_value(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        key=key,
        branch_id=branch_id,
    )


@router.put(
    "/{project}/{component_id}/{config_id}/metadata/{key}",
    summary="Set a metadata value",
)
def metadata_set(
    project: str,
    component_id: str,
    config_id: str,
    key: str,
    body: MetadataSet,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set a metadata value on a configuration. Mirrors `kbagent config set-metadata`."""
    return registry.config.set_config_metadata(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        key=key,
        value=body.value,
        branch_id=branch_id,
    )


@router.delete(
    "/{project}/{component_id}/{config_id}/metadata/{metadata_id}",
    summary="Delete a metadata entry",
)
def metadata_delete(
    project: str,
    component_id: str,
    config_id: str,
    metadata_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a metadata entry by id. Mirrors `kbagent config delete-metadata`."""
    return registry.config.delete_config_metadata(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        metadata_id=metadata_id,
        branch_id=branch_id,
    )


@router.post(
    "/{project}/{component_id}/{config_id}/folder", summary="Move configuration to a folder"
)
def set_folder(
    project: str,
    component_id: str,
    config_id: str,
    folder: str = Body(..., embed=True),
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Move a configuration into a folder. Mirrors `kbagent config set-folder`."""
    return registry.config.set_config_folder(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        folder=folder,
        branch_id=branch_id,
    )


@router.post("/{project}/{component_id}/{config_id}/rows", summary="Create a configuration row")
def row_create(
    project: str,
    component_id: str,
    config_id: str,
    body: ConfigCreateRow,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create a new row on a configuration. Mirrors `kbagent config row-create`."""
    return registry.config.create_config_row(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        name=body.name,
        description=body.description,
        configuration=body.configuration,
        is_disabled=body.is_disabled,
        branch_id=body.branch_id,
    )


@router.patch(
    "/{project}/{component_id}/{config_id}/rows/{row_id}",
    summary="Update a configuration row",
)
def row_update(
    project: str,
    component_id: str,
    config_id: str,
    row_id: str,
    body: ConfigUpdateRow,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Update a configuration row. Mirrors `kbagent config row-update`."""
    return registry.config.update_config_row(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        row_id=row_id,
        name=body.name,
        description=body.description,
        configuration=body.configuration,
        is_disabled=body.is_disabled,
        branch_id=body.branch_id,
    )


@router.delete(
    "/{project}/{component_id}/{config_id}/rows/{row_id}",
    summary="Delete a configuration row",
)
def row_delete(
    project: str,
    component_id: str,
    config_id: str,
    row_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a configuration row. Mirrors `kbagent config row-delete`."""
    return registry.config.delete_config_row(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        row_id=row_id,
        branch_id=branch_id,
    )


@router.get(
    "/{project}/{component_id}/{config_id}/oauth-url",
    summary="Get OAuth authorization URL",
)
def oauth_url(
    project: str,
    component_id: str,
    config_id: str,
    redirect_url: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Get an OAuth authorization URL for a configuration. Mirrors `kbagent config oauth-url`."""
    return registry.config.get_oauth_url(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        redirect_url=redirect_url,
    )


# ---- Variables (delegated to VariablesService) ----


class VariablesSet(BaseModel):
    variables: dict[str, str]
    replace: bool = False
    variables_id: str | None = None
    values_id: str | None = None
    branch_id: int | None = None
    dry_run: bool = False


@router.get(
    "/{project}/{component_id}/{config_id}/variables",
    summary="Get configuration variables",
)
def variables_get(
    project: str,
    component_id: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Read variables attached to a configuration. Mirrors `kbagent config variables-get`."""
    return registry.variables.get_variables(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
    )


@router.put(
    "/{project}/{component_id}/{config_id}/variables",
    summary="Set configuration variables",
)
def variables_set(
    project: str,
    component_id: str,
    config_id: str,
    body: VariablesSet,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set or merge configuration variables. Mirrors `kbagent config variables-set`."""
    return registry.variables.set_variables(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        variables=body.variables,
        replace=body.replace,
        variables_id=body.variables_id,
        values_id=body.values_id,
        branch_id=body.branch_id,
        dry_run=body.dry_run,
    )


@router.delete(
    "/{project}/{component_id}/{config_id}/variables",
    summary="Clear configuration variables",
)
def variables_clear(
    project: str,
    component_id: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Remove all variables from a configuration. Mirrors `kbagent config variables-clear`."""
    return registry.variables.clear_variables(
        alias=project,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch_id,
    )
