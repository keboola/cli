"""Draft lifecycle wire contract through the native service and HTTP client."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.data_science_client import DataScienceClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.data_app_service import DataAppService

_APP_ID = "42"
_APP = "/v2/apps/42"
_DRAFT = "01a1c00c-07e3-7c46-99ec-6a814a0baa19"
_DRAFTS = f"{_APP}/drafts"
_DRAFT_PATH = f"{_DRAFTS}/{_DRAFT}"
_STORAGE = "/v2/storage/components/keboola.data-apps/configs/cfg1"
_STACK = "https://connection.keboola.com"
_PREVIEW = {"url": "https://example.test/#t=fake", "linkExpiresAt": "2026-11-20T01:00:00Z"}
_DRAFT_PAYLOAD = {
    "id": _DRAFT,
    "gitBranch": "feature/test",
    "url": None,
    "runState": "pending",
    "createdAt": "2026-11-20T00:00:00Z",
}


class Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.app_type = "python-js"
        self.legacy = False
        self.branch_id: int | None = None
        self.replies: dict[tuple[str, str], tuple[int, Any]] = {}

    def request(self, request: httpx.Request) -> httpx.Response:
        key = request.method, request.url.path
        self.calls.append(key)
        storage = (
            _STORAGE.replace("/storage/", f"/storage/branch/{self.branch_id}/")
            if self.branch_id
            else _STORAGE
        )
        if key == ("GET", storage):
            return httpx.Response(
                200,
                json={
                    "name": "Test app",
                    "configuration": {
                        "parameters": {
                            "dataApp": {
                                "isDraft": self.legacy,
                                "parentConfigurationId": "parent" if self.legacy else None,
                            },
                        },
                    },
                },
            )
        if key == ("GET", _APP):
            return httpx.Response(
                200,
                json={
                    "componentId": "keboola.data-apps",
                    "type": self.app_type,
                    "configId": "cfg1",
                    "branchId": self.branch_id,
                },
            )
        if key == ("POST", _DRAFTS):
            assert json.loads(request.content) == {"gitBranch": "feature/test"}
        if request.url.path.endswith("/preview-link"):
            assert json.loads(request.content) == {}
        status, body = self.replies[key]
        return httpx.Response(status, json=body)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Backend:
    mock = Backend()
    original = httpx.Client.__init__

    def init(client: httpx.Client, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(mock.request)
        original(client, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    return mock


@pytest.fixture
def service(tmp_path: Path) -> DataAppService:
    store = ConfigStore(config_dir=tmp_path)
    store.add_project(
        "prod",
        ProjectConfig(stack_url=_STACK, token="fake-token", project_id=123, active_branch_id=456),
    )
    return DataAppService(config_store=store)


def test_create_polls_for_url(backend: Backend, service: DataAppService) -> None:
    draft_url = "https://draft.example.test/"
    backend.replies = {
        ("GET", _DRAFTS): (200, []),
        ("POST", _DRAFTS): (201, _DRAFT_PAYLOAD),
        ("GET", _DRAFT_PATH): (200, _DRAFT_PAYLOAD | {"url": draft_url}),
    }
    with patch("keboola_agent_cli.services._data_app_drafts.time.sleep"):
        result = service.create_data_app_draft("prod", _APP_ID, " feature/test ")
    assert result["draft"] == {
        "draft_id": _DRAFT,
        "git_branch": "feature/test",
        "url": draft_url,
        "run_state": "pending",
        "created_at": _DRAFT_PAYLOAD["createdAt"],
    }
    assert result == {
        "project_alias": "prod",
        "app_id": _APP_ID,
        "status": "created",
        "draft": result["draft"],
    }
    assert backend.calls == [
        ("GET", _APP),
        ("GET", _STORAGE),
        ("GET", _DRAFTS),
        ("POST", _DRAFTS),
        ("GET", _DRAFT_PATH),
    ]


def test_create_refuses_duplicate(backend: Backend, service: DataAppService) -> None:
    backend.replies = {("GET", _DRAFTS): (200, [_DRAFT_PAYLOAD])}
    with pytest.raises(KeboolaApiError, match="already serves branch") as error:
        service.create_data_app_draft("prod", _APP_ID, "feature/test")
    assert error.value.status_code == 409
    assert error.value.error_code == ErrorCode.ALREADY_EXISTS
    assert error.value.details["draft"]["draft_id"] == _DRAFT
    assert ("POST", _DRAFTS) not in backend.calls


def test_list_drafts_uses_apps_storage_branch(backend: Backend, service: DataAppService) -> None:
    backend.branch_id = 789
    backend.replies = {("GET", _DRAFTS): (200, [_DRAFT_PAYLOAD, _DRAFT_PAYLOAD | {"id": "second"}])}
    assert service.list_data_app_drafts("prod", _APP_ID, limit=1) == {
        "project_alias": "prod",
        "app_id": _APP_ID,
        "count": 1,
        "limit": 1,
        "has_more": True,
        "drafts": [
            {
                "draft_id": _DRAFT,
                "git_branch": "feature/test",
                "url": None,
                "run_state": "pending",
                "created_at": _DRAFT_PAYLOAD["createdAt"],
            }
        ],
    }
    assert backend.calls == [
        ("GET", _APP),
        ("GET", _STORAGE.replace("/storage/", "/storage/branch/789/")),
        ("GET", _DRAFTS),
    ]


def test_create_does_not_retry_unknown_post(backend: Backend, service: DataAppService) -> None:
    backend.replies = {("GET", _DRAFTS): (200, []), ("POST", _DRAFTS): (500, {"error": "error"})}
    with pytest.raises(KeboolaApiError, match="may already exist") as error:
        service.create_data_app_draft("prod", _APP_ID, "feature/test")
    assert not error.value.retryable
    assert backend.calls.count(("POST", _DRAFTS)) == 1


@pytest.mark.parametrize("app_type,legacy", [("streamlit", True), ("python-js", False)])
def test_delete_rejects_nonlegacy(
    backend: Backend, service: DataAppService, app_type: str, legacy: bool
) -> None:
    backend.app_type, backend.legacy = app_type, legacy
    with pytest.raises(KeboolaApiError, match="not a legacy python-js draft"):
        service.delete_data_app_draft("prod", _APP_ID)
    assert backend.calls == [("GET", _APP), ("GET", _STORAGE)]


@pytest.mark.parametrize("legacy", [False, True])
def test_delete_calls_only_draft_or_legacy_app_endpoint(
    backend: Backend, service: DataAppService, legacy: bool
) -> None:
    backend.legacy = legacy
    draft_id = None if legacy else _DRAFT
    path = _APP if legacy else _DRAFT_PATH
    backend.replies = {("GET", _DRAFT_PATH): (200, _DRAFT_PAYLOAD), ("DELETE", path): (204, None)}
    assert service.delete_data_app_draft("prod", _APP_ID, draft_id, dry_run=True) == {
        "project_alias": "prod",
        "app_id": _APP_ID,
        "draft_id": draft_id,
        "dry_run": True,
    }
    assert not any(method == "DELETE" for method, _ in backend.calls)
    result = service.delete_data_app_draft("prod", _APP_ID, draft_id)
    assert result == {
        "project_alias": "prod",
        "app_id": _APP_ID,
        "draft_id": draft_id,
        "deleted": True,
    }
    assert [call for call in backend.calls if call[0] == "DELETE"] == [("DELETE", path)]


@pytest.mark.parametrize("legacy", [False, True])
def test_preview_mints_correct_link(
    backend: Backend, service: DataAppService, legacy: bool
) -> None:
    backend.legacy = legacy
    endpoint = "/apps/42" if legacy else _DRAFT_PATH
    backend.replies = {("POST", f"{endpoint}/preview-link"): (200, _PREVIEW)}
    assert service.get_data_app_preview_link("prod", _APP_ID, None if legacy else _DRAFT) == {
        "project_alias": "prod",
        "app_id": _APP_ID,
        "url": _PREVIEW["url"],
        "link_expires_at": _PREVIEW["linkExpiresAt"],
    }


def test_invalid_preview_response_does_not_disclose_secret() -> None:
    with (
        DataScienceClient(_STACK, "fake-token") as client,
        patch.object(
            client, "_do_request", return_value=httpx.Response(200, json={"url": _PREVIEW["url"]})
        ),
        pytest.raises(KeboolaApiError, match="invalid preview-link response") as error,
    ):
        client.create_preview_link("42", _DRAFT)
    assert _PREVIEW["url"] not in str(error.value)


def test_preview_rejects_non_uuid(backend: Backend, service: DataAppService) -> None:
    with pytest.raises(KeboolaApiError, match="Draft ID must be a UUID"):
        service.get_data_app_preview_link("prod", _APP_ID, "../deploy")
    assert not backend.calls
