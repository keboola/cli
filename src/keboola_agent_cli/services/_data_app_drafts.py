"""Draft lifecycle shared by the data-app CLI and REST API."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..data_science_client import DataAppDraft, DataScienceClient
from ..errors import ErrorCode, KeboolaApiError, request_outcome_unknown
from ._data_app_bodies import DATA_APP_COMPONENT_ID, _coerce_config_dict
from ._data_app_password import app_branch_id
from .base import BaseService

logger = logging.getLogger(__name__)
_URL_WAIT_SECONDS = 30
_URL_POLL_SECONDS = 2
_UNAVAILABLE = "Drafts are not available on this Keboola stack."


@dataclass(frozen=True)
class _DraftTarget:
    ds: DataScienceClient
    app_id: str
    app_type: str
    is_legacy: bool
    parent_id: str | None
    git_branch: str | None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validate_draft_id(draft_id: str | None) -> None:
    if draft_id is None:
        return
    try:
        if str(UUID(draft_id)) == draft_id.lower():
            return
    except ValueError:
        pass
    raise KeboolaApiError("Draft ID must be a UUID.", 400, ErrorCode.VALIDATION_ERROR)


def _wait_for_url(ds: DataScienceClient, app_id: str, draft: DataAppDraft) -> DataAppDraft:
    deadline = time.monotonic() + _URL_WAIT_SECONDS
    while not draft.url:
        remaining = deadline - time.monotonic()
        if remaining <= _URL_POLL_SECONDS:
            break
        time.sleep(_URL_POLL_SECONDS)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            draft = ds.get_draft(app_id, draft.draft_id, timeout=remaining)
        except (KeboolaApiError, ValueError):
            logger.debug("Could not read draft %s while waiting for its URL", draft.draft_id)
            break
    return draft


def _conversion_hint(target: _DraftTarget) -> str:
    if target.parent_id and target.git_branch:
        return (
            f"Legacy draft '{target.app_id}' has no preview on the current platform. "
            f"Find the app ID for parent config '{target.parent_id}' with data-app list, "
            "then create a draft on Git branch "
            f"'{target.git_branch}', then delete this legacy draft."
        )
    return (
        f"Legacy draft '{target.app_id}' cannot be converted: "
        "its parent configuration or Git branch is unknown."
    )


def _preview_error(
    exc: KeboolaApiError, target: _DraftTarget, draft_id: str | None
) -> KeboolaApiError:
    status, message, code = exc.status_code, exc.message, exc.error_code
    if status == 403 or (status == 400 and "is not authorized to manage app" in message):
        message = f"The token cannot manage data app '{target.app_id}' to mint a preview link."
        code = ErrorCode.ACCESS_DENIED
    elif status == 404 and "No route found" in message:
        message = (
            _UNAVAILABLE if draft_id else "Preview links are not available on this Keboola stack."
        )
    elif status == 404 and draft_id:
        message = f"Data app '{target.app_id}' has no draft '{draft_id}'."
    elif status == 400 and "has no URL yet" in message and draft_id:
        message = (
            f"Draft '{draft_id}' is still starting and has no URL. Try again in a few seconds."
        )
    elif status == 400 and draft_id is None and "is not in dev mode" in message:
        message = (
            _conversion_hint(target)
            if target.is_legacy
            else "This app is not in dev mode. Preview a draft instead; do not switch its mode."
        )
    elif status == 400 and draft_id is None and "has no URL yet" in message:
        message = (
            _conversion_hint(target)
            if target.is_legacy
            else "The app has no URL yet. Wait until it is running before minting a preview link."
        )
    elif status == 503 and draft_id is None and "not configured" in message:
        message = "Preview links are not configured on this Keboola stack."
    return KeboolaApiError(message, status, code, retryable=exc.retryable, details=exc.details)


class DataAppDraftService(BaseService):
    _ds_client_factory: Callable[[str, str], DataScienceClient]

    @contextmanager
    def _draft_target(self, alias: str, app_id: str) -> Iterator[_DraftTarget]:
        project = self.resolve_projects([alias])[alias]
        with self._ds_client_factory(project.stack_url, project.token) as ds:
            app = ds.get_app(app_id)
            if app.get("componentId", app.get("component_id")) != DATA_APP_COMPONENT_ID:
                raise KeboolaApiError(
                    f"Data app '{app_id}' does not belong to the {DATA_APP_COMPONENT_ID} component.",
                    400,
                    ErrorCode.VALIDATION_ERROR,
                )
            config_id = str(app.get("configId") or "")
            if not config_id:
                raise KeboolaApiError(
                    f"Data app {app_id} has no associated configId", 500, ErrorCode.API_ERROR
                )
            with self._client_factory(project.stack_url, project.token) as storage:
                config = storage.get_config_detail(
                    DATA_APP_COMPONENT_ID, config_id, branch_id=app_branch_id(app)
                )
            parameters = _mapping(
                _coerce_config_dict(config.get("configuration")).get("parameters")
            )
            data_app = _mapping(parameters.get("dataApp"))
            git_branch = _mapping(data_app.get("git")).get("branch")
            parent = data_app.get("parentConfigurationId")
            yield _DraftTarget(
                ds,
                app_id,
                str(app.get("type", "")),
                data_app.get("isDraft") is True,
                parent if isinstance(parent, str) and parent else None,
                git_branch if isinstance(git_branch, str) and git_branch else None,
            )

    def list_data_app_drafts(
        self, alias: str, app_id: str, *, limit: int | None = None
    ) -> dict[str, Any]:
        with self._draft_target(alias, app_id) as target:
            drafts = target.ds.list_drafts(target.app_id)
        items = [draft.model_dump() for draft in drafts[:limit]]
        return {
            "project_alias": alias,
            "app_id": app_id,
            "drafts": items,
            "count": len(items),
            "limit": limit,
            "has_more": len(items) < len(drafts),
        }

    def create_data_app_draft(self, alias: str, app_id: str, git_branch: str) -> dict[str, Any]:
        branch = git_branch.strip()
        if not branch or any(char.isspace() for char in branch):
            raise KeboolaApiError("Git branch name is invalid.", 400, ErrorCode.VALIDATION_ERROR)
        with self._draft_target(alias, app_id) as target:
            ds = target.ds
            if target.app_type != "python-js":
                raise KeboolaApiError(
                    "Only python-js data apps have drafts.", 400, ErrorCode.VALIDATION_ERROR
                )
            if target.is_legacy:
                message = (
                    f"Find the app ID for parent config '{target.parent_id}' with data-app list, "
                    f"and pass it instead of legacy draft '{app_id}'."
                    if target.parent_id
                    else _conversion_hint(target)
                )
                raise KeboolaApiError(message, 400, ErrorCode.VALIDATION_ERROR)
            try:
                drafts = ds.list_drafts(target.app_id)
            except (KeboolaApiError, ValueError, TypeError):
                logger.debug(
                    "Could not list drafts of data app %s before creating one", target.app_id
                )
                drafts = []
            for existing in drafts:
                if existing.git_branch == branch:
                    raise KeboolaApiError(
                        f"Draft '{existing.draft_id}' already serves branch '{branch}' "
                        f"(run state: {existing.run_state}). Continue that draft, or delete it first.",
                        409,
                        ErrorCode.ALREADY_EXISTS,
                        details={"draft": existing.model_dump()},
                    )
            try:
                draft = ds.create_draft(target.app_id, branch)
            except KeboolaApiError as exc:
                if exc.status_code == 400 and "does not exist" in exc.message:
                    raise KeboolaApiError(
                        f"{exc.message} Push the Git branch first, then try again.",
                        400,
                        ErrorCode.VALIDATION_ERROR,
                    ) from exc
                if exc.status_code == 404 and "No route found" in exc.message:
                    raise KeboolaApiError(_UNAVAILABLE, 404, ErrorCode.NOT_FOUND) from exc
                if not request_outcome_unknown(exc):
                    raise
                raise self._unknown_draft_outcome(branch, exc) from exc
            except ValueError as exc:
                raise self._unknown_draft_outcome(branch) from exc
            draft = _wait_for_url(ds, target.app_id, draft)
        return {
            "project_alias": alias,
            "app_id": app_id,
            "status": "created",
            "draft": draft.model_dump(),
        }

    def _unknown_draft_outcome(
        self, branch: str, exc: KeboolaApiError | None = None
    ) -> KeboolaApiError:
        message = (
            f"A draft may already exist on Git branch '{branch}'. "
            "Check with data-app draft-list before retrying."
        )
        return KeboolaApiError(
            message,
            exc.status_code if exc else 0,
            exc.error_code if exc else ErrorCode.API_ERROR,
            details=exc.details if exc else None,
        )

    def delete_data_app_draft(
        self, alias: str, app_id: str, draft_id: str | None = None, *, dry_run: bool = False
    ) -> dict[str, Any]:
        _validate_draft_id(draft_id)
        with self._draft_target(alias, app_id) as target:
            ds = target.ds
            if draft_id is None and (target.app_type != "python-js" or not target.is_legacy):
                raise KeboolaApiError(
                    f"Data app '{app_id}' is not a legacy python-js draft. "
                    "To delete a draft of a production app, pass --draft-id.",
                    400,
                    ErrorCode.VALIDATION_ERROR,
                )
            if dry_run:
                if draft_id is not None:
                    ds.get_draft(target.app_id, draft_id, timeout=_URL_WAIT_SECONDS)
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "app_id": app_id,
                    "draft_id": draft_id,
                }
            if draft_id is None:
                ds.delete_app(target.app_id)
            else:
                try:
                    ds.delete_draft(target.app_id, draft_id)
                except KeboolaApiError as exc:
                    if exc.status_code == 404:
                        raise KeboolaApiError(
                            f"Data app '{app_id}' has no draft '{draft_id}'; it may already be deleted.",
                            404,
                            ErrorCode.NOT_FOUND,
                        ) from exc
                    raise
        return {
            "project_alias": alias,
            "app_id": app_id,
            "deleted": True,
            "draft_id": draft_id,
        }

    def get_data_app_preview_link(
        self, alias: str, app_id: str, draft_id: str | None = None
    ) -> dict[str, str]:
        _validate_draft_id(draft_id)
        try:
            with self._draft_target(alias, app_id) as target:
                try:
                    link = target.ds.create_preview_link(target.app_id, draft_id)
                    return {"project_alias": alias, "app_id": app_id, **link.model_dump()}
                except KeboolaApiError as exc:
                    raise _preview_error(exc, target, draft_id) from exc
        except KeboolaApiError as exc:
            if exc.status_code in (400, 403) and "is not authorized to manage app" in exc.message:
                raise KeboolaApiError(
                    f"The token cannot manage data app '{app_id}' to mint a preview link.",
                    exc.status_code,
                    ErrorCode.ACCESS_DENIED,
                ) from exc
            raise
