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


@router.get("")
def list_apps(
    project: list[str] | None = Query(None),
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.list_data_apps(aliases=project, branch_id=branch_id)


@router.get("/{project}/{app_id}")
def detail(
    project: str,
    app_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.get_data_app(alias=project, app_id=app_id, branch_id=branch_id)


@router.post("/{project}")
def create(
    project: str, body: DataAppCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
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


@router.post("/{project}/{app_id}/deploy")
def deploy(
    project: str,
    app_id: str,
    config_version: int | None = None,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.deploy_data_app(
        alias=project,
        app_id=app_id,
        config_version=config_version,
        wait=wait,
        timeout_seconds=timeout_seconds,
        branch_id=branch_id,
    )


@router.post("/{project}/{app_id}/start")
def start(
    project: str,
    app_id: str,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.start_data_app(
        alias=project, app_id=app_id, wait=wait, timeout_seconds=timeout_seconds
    )


@router.post("/{project}/{app_id}/stop")
def stop(
    project: str,
    app_id: str,
    wait: bool = False,
    timeout_seconds: float = 600.0,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.stop_data_app(
        alias=project, app_id=app_id, wait=wait, timeout_seconds=timeout_seconds
    )


@router.delete("/{project}/{app_id}")
def delete(
    project: str, app_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.data_app.delete_data_app(alias=project, app_id=app_id)


@router.get("/{project}/{app_id}/password")
def password(
    project: str, app_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.data_app.get_data_app_password(alias=project, app_id=app_id)


@router.get("/{project}/{app_id}/secrets")
def secrets_list(
    project: str,
    app_id: str,
    branch_id: int | None = None,
    show_fingerprint: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.list_data_app_secrets(
        alias=project,
        app_id=app_id,
        branch_id=branch_id,
        show_fingerprint=show_fingerprint,
    )


@router.get("/{project}/{app_id}/secrets/{key:path}")
def secrets_get(
    project: str,
    app_id: str,
    key: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.get_data_app_secret(
        alias=project, app_id=app_id, key=key, branch_id=branch_id
    )


@router.put("/{project}/{app_id}/secrets")
def secrets_set(
    project: str,
    app_id: str,
    body: SecretsSet,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.set_data_app_secrets(
        alias=project,
        app_id=app_id,
        secrets=body.secrets,
        branch_id=body.branch_id,
        allow_plaintext_on_encrypt_failure=body.allow_plaintext_on_encrypt_failure,
        dry_run=body.dry_run,
    )


@router.post("/{project}/{app_id}/secrets/remove")
def secrets_remove(
    project: str,
    app_id: str,
    body: SecretsRemove,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.data_app.remove_data_app_secrets(
        alias=project,
        app_id=app_id,
        keys=body.keys,
        branch_id=body.branch_id,
        dry_run=body.dry_run,
    )


@router.post("/validate-repo")
def validate_repo(
    body: RepoValidate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.repo_validate.validate(
        git_repo=body.git_repo,
        git_branch=body.git_branch,
        git_public=body.git_public,
        git_pat_env=body.git_pat_env,
        git_pat_file=body.git_pat_file,
        type_=body.type,
        strict=body.strict,
    )
