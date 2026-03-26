"""Tests for ManageClient - list org projects, create tokens, retries, error handling."""

import pytest

from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.manage_client import ManageClient

STACK_URL = "https://connection.keboola.com"
MANAGE_TOKEN = "manage-12345-abcdefghijklmnopqrstuvwxyz"

PROJECTS_RESPONSE = [
    {"id": 100, "name": "Project Alpha"},
    {"id": 200, "name": "Project Beta"},
    {"id": 300, "name": "Project Gamma"},
]

TOKEN_RESPONSE = {
    "id": "tok-999",
    "token": "901-99999-newStorageTokenValue1234",
    "description": "kbagent-cli (Project Alpha)",
}


class TestManageClientHeaders:
    """Verify that ManageClient sends the correct auth header."""

    def test_sends_manage_api_token_header(self, httpx_mock) -> None:
        """Requests include X-KBC-ManageApiToken header."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/1/projects",
            json=[],
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.list_organization_projects(1)

        request = httpx_mock.get_request()
        assert request.headers["X-KBC-ManageApiToken"] == MANAGE_TOKEN
        assert "keboola-agent-cli" in request.headers["User-Agent"]
        client.close()

    def test_does_not_send_storage_token_header(self, httpx_mock) -> None:
        """Requests do NOT include X-StorageApi-Token header."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/1/projects",
            json=[],
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.list_organization_projects(1)

        request = httpx_mock.get_request()
        assert "X-StorageApi-Token" not in request.headers
        client.close()


class TestListOrganizationProjects:
    """Tests for list_organization_projects()."""

    def test_success(self, httpx_mock) -> None:
        """Returns list of project dicts on success."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json=PROJECTS_RESPONSE,
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.list_organization_projects(42)

        assert len(result) == 3
        assert result[0]["id"] == 100
        assert result[0]["name"] == "Project Alpha"
        assert result[2]["id"] == 300
        client.close()

    def test_401_invalid_token(self, httpx_mock) -> None:
        """Raises KeboolaApiError with INVALID_TOKEN on 401."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"error": "Invalid manage token"},
            status_code=401,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.list_organization_projects(42)

        assert exc_info.value.error_code == "INVALID_TOKEN"
        assert exc_info.value.status_code == 401
        assert exc_info.value.retryable is False
        client.close()

    def test_404_org_not_found(self, httpx_mock) -> None:
        """Raises KeboolaApiError with NOT_FOUND on 404."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/999/projects",
            json={"error": "Organization not found"},
            status_code=404,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.list_organization_projects(999)

        assert exc_info.value.error_code == "NOT_FOUND"
        assert exc_info.value.status_code == 404
        client.close()

    def test_retry_on_503(self, httpx_mock) -> None:
        """Retries on 503 and succeeds on subsequent attempt."""
        # First call: 503
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"error": "Service unavailable"},
            status_code=503,
        )
        # Second call: 200
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json=PROJECTS_RESPONSE,
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.list_organization_projects(42)

        assert len(result) == 3
        assert len(httpx_mock.get_requests()) == 2
        client.close()

    def test_empty_org(self, httpx_mock) -> None:
        """Returns empty list for organization with no projects."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json=[],
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.list_organization_projects(42)

        assert result == []
        client.close()


class TestCreateProjectToken:
    """Tests for create_project_token()."""

    def test_success(self, httpx_mock) -> None:
        """Creates token and returns response dict with token field."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json=TOKEN_RESPONSE,
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.create_project_token(
            project_id=100,
            description="kbagent-cli (Project Alpha)",
        )

        assert result["token"] == "901-99999-newStorageTokenValue1234"
        assert result["description"] == "kbagent-cli (Project Alpha)"
        client.close()

    def test_success_with_200(self, httpx_mock) -> None:
        """Creates token when API returns 200 instead of 201."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json=TOKEN_RESPONSE,
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.create_project_token(
            project_id=100,
            description="kbagent-cli (Project Alpha)",
        )

        assert result["token"] == "901-99999-newStorageTokenValue1234"
        client.close()

    def test_403_access_denied(self, httpx_mock) -> None:
        """Raises KeboolaApiError with ACCESS_DENIED on 403."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json={"error": "You don't have access to this project"},
            status_code=403,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.create_project_token(project_id=100, description="test")

        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403
        client.close()

    def test_custom_description_in_payload(self, httpx_mock) -> None:
        """Sends correct description and capability flags in request body."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json=TOKEN_RESPONSE,
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.create_project_token(
            project_id=100,
            description="custom-desc",
            can_manage_buckets=False,
            can_read_all_file_uploads=False,
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["description"] == "custom-desc"
        assert body["canManageBuckets"] is False
        assert body["canReadAllFileUploads"] is False
        client.close()


    def test_expires_in_included_in_payload(self, httpx_mock) -> None:
        """When expires_in is set, expiresIn is sent in the request payload."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json=TOKEN_RESPONSE,
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.create_project_token(
            project_id=100,
            description="kbagent-cli",
            expires_in=3600,
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["expiresIn"] == 3600
        client.close()

    def test_expires_in_none_excluded_from_payload(self, httpx_mock) -> None:
        """When expires_in is None, expiresIn key is absent from the request payload."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/100/tokens",
            json=TOKEN_RESPONSE,
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.create_project_token(
            project_id=100,
            description="kbagent-cli",
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert "expiresIn" not in body
        client.close()


class TestManageClientContextManager:
    """Test context manager protocol."""

    def test_context_manager(self, httpx_mock) -> None:
        """ManageClient works as a context manager."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/1/projects",
            json=[],
            status_code=200,
        )

        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.list_organization_projects(1)
            assert result == []
