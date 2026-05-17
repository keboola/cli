"""Data App endpoints (lifecycle, secrets, password)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/data-apps", tags=["data-apps"])


class DataAppCreate(BaseModel):
    name: str
    description: str = ""
    slug: str
    git_repo: str
    git_branch: str = "main"
    git_public: bool = False
    git_username: str | None = None
    git_pat_plaintext: str | None = None
    git_pat_encrypted: str | None = None
    auth: str = "password"
    size: str = "tiny"
    auto_suspend_after_seconds: int = 900
    type: str = "python-js"
    branch_id: int | None = None
    deploy: bool = True
    wait: bool = False
    timeout_seconds: float = 600.0
    keep_on_failure: bool = False
    dry_run: bool = False


class SecretsSet(BaseModel):
    secrets: dict[str, str]
    branch_id: int | None = None
    allow_plaintext_on_encrypt_failure: bool = False
    dry_run: bool = False


class SecretsRemove(BaseModel):
    keys: list[str]
    branch_id: int | None = None
    dry_run: bool = False


class RepoValidate(BaseModel):
    git_repo: str
    git_branch: str = "main"
    git_public: bool = True
    git_pat_env: str | None = None
    git_pat_file: str | None = None
    type: str = "python-js"
    strict: bool = False


@router.get("", summary="List data apps across projects")
def list_apps(
    project: list[str] | None = Query(None),
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List data apps in one or more projects. Mirrors `kbagent data-app list`."""
    return registry.data_app.list_data_apps(aliases=project, branch_id=branch_id)


@router.get("/{project}/{app_id}", summary="Get data app detail")
def detail(
    project: str,
    app_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch detail for a single data app. Mirrors `kbagent data-app detail`."""
    return registry.data_app.get_data_app(alias=project, app_id=app_id, branch_id=branch_id)


@router.post("/{project}", summary="Create a data app")
def create(
    project: str, body: DataAppCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new data app, optionally deploy and wait. Mirrors `kbagent data-app create`."""
    return registry.data_app.create_data_app(
        alias=project,
        name=body.name,
        description=body.description,
        slug=body.slug,
        git_repo=body.git_repo,
        git_branch=body.git_branch,
        git_public=body.git_public,
        git_username=body.git_username,
        git_pat_plaintext=body.git_pat_plaintext,
        git_pat_encrypted=body.git_pat_encrypted,
        auth=body.auth,
        size=body.size,
        auto_suspend_after_seconds=body.auto_suspend_after_seconds,
        type_=body.type,
        branch_id=body.branch_id,
        deploy=body.deploy,
        wait=body.wait,
        timeout_seconds=body.timeout_seconds,
        keep_on_failure=body.keep_on_failure,
        dry_run=body.dry_run,
    )


@router.post("/{project}/{app_id}/deploy", summary="Deploy a data app version")
def deploy(
    project: str,
    app_id: str,
    config_version: int | None = None,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Deploy the configured version of a data app. Mirrors `kbagent data-app deploy`."""
    return registry.data_app.deploy_data_app(
        alias=project,
        app_id=app_id,
        config_version=config_version,
        wait=wait,
        timeout_seconds=timeout_seconds,
        branch_id=branch_id,
    )


@router.post("/{project}/{app_id}/start", summary="Start a data app")
def start(
    project: str,
    app_id: str,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Start a deployed data app. Mirrors `kbagent data-app start`."""
    return registry.data_app.start_data_app(
        alias=project, app_id=app_id, wait=wait, timeout_seconds=timeout_seconds
    )


@router.post("/{project}/{app_id}/stop", summary="Stop a data app")
def stop(
    project: str,
    app_id: str,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Stop a running data app. Mirrors `kbagent data-app stop`."""
    return registry.data_app.stop_data_app(
        alias=project, app_id=app_id, wait=wait, timeout_seconds=timeout_seconds
    )


@router.delete("/{project}/{app_id}", summary="Delete a data app")
def delete(
    project: str, app_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Delete a data app and its configuration. Mirrors `kbagent data-app delete`."""
    return registry.data_app.delete_data_app(alias=project, app_id=app_id)


@router.get("/{project}/{app_id}/password", summary="Get data app access password")
def password(
    project: str, app_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Fetch the password for a password-protected data app. Mirrors `kbagent data-app password`."""
    return registry.data_app.get_data_app_password(alias=project, app_id=app_id)


@router.get("/{project}/{app_id}/secrets", summary="List data app secrets")
def secrets_list(
    project: str,
    app_id: str,
    branch_id: int | None = None,
    show_fingerprint: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List secret keys configured on a data app. Mirrors `kbagent data-app secrets-list`."""
    return registry.data_app.list_data_app_secrets(
        alias=project,
        app_id=app_id,
        branch_id=branch_id,
        show_fingerprint=show_fingerprint,
    )


@router.get("/{project}/{app_id}/secrets/{key:path}", summary="Get a single data app secret")
def secrets_get(
    project: str,
    app_id: str,
    key: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Read a single secret value on a data app. Mirrors `kbagent data-app secrets-get`."""
    return registry.data_app.get_data_app_secret(
        alias=project, app_id=app_id, key=key, branch_id=branch_id
    )


@router.put("/{project}/{app_id}/secrets", summary="Set data app secrets")
def secrets_set(
    project: str,
    app_id: str,
    body: SecretsSet,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Set or update encrypted secrets on a data app. Mirrors `kbagent data-app secrets-set`."""
    return registry.data_app.set_data_app_secrets(
        alias=project,
        app_id=app_id,
        secrets=body.secrets,
        branch_id=body.branch_id,
        allow_plaintext_on_encrypt_failure=body.allow_plaintext_on_encrypt_failure,
        dry_run=body.dry_run,
    )


@router.post("/{project}/{app_id}/secrets/remove", summary="Remove data app secrets")
def secrets_remove(
    project: str,
    app_id: str,
    body: SecretsRemove,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Remove one or more secrets from a data app. Mirrors `kbagent data-app secrets-remove`."""
    return registry.data_app.remove_data_app_secrets(
        alias=project,
        app_id=app_id,
        keys=body.keys,
        branch_id=body.branch_id,
        dry_run=body.dry_run,
    )


@router.post("/validate-repo", summary="Validate a data app git repo")
def validate_repo(
    body: RepoValidate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Validate that a git repo is a deployable data app. Mirrors `kbagent data-app validate-repo`."""
    return registry.repo_validate.validate(
        git_repo=body.git_repo,
        git_branch=body.git_branch,
        git_public=body.git_public,
        git_pat_env=body.git_pat_env,
        git_pat_file=body.git_pat_file,
        type_=body.type,
        strict=body.strict,
    )
