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
        assert "keboola-cli" in request.headers["User-Agent"]
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


class TestCreateProject:
    """Tests for create_project()."""

    def test_success(self, httpx_mock) -> None:
        """Creates a project and returns the response dict with the new id."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"id": 12345, "name": "Agent Sandbox"},
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.create_project(organization_id=42, name="Agent Sandbox")

        assert result["id"] == 12345
        assert result["name"] == "Agent Sandbox"
        client.close()

    def test_name_is_sole_payload_key_without_extras(self, httpx_mock) -> None:
        """With no extra_params, name is the only body field sent."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"id": 1, "name": "Agent Sandbox"},
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.create_project(organization_id=42, name="Agent Sandbox")

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body == {"name": "Agent Sandbox"}
        client.close()

    def test_extra_params_merged_into_payload(self, httpx_mock) -> None:
        """extra_params are passed through verbatim alongside name."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"id": 1, "name": "PoC"},
            status_code=201,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        client.create_project(
            organization_id=42,
            name="PoC",
            extra_params={"type": "poc6months", "defaultBackend": "snowflake"},
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["name"] == "PoC"
        assert body["type"] == "poc6months"
        assert body["defaultBackend"] == "snowflake"
        client.close()

    def test_403_access_denied(self, httpx_mock) -> None:
        """Raises KeboolaApiError with ACCESS_DENIED on 403 (non-admin token)."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/42/projects",
            json={"error": "You don't have access to this organization"},
            status_code=403,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.create_project(organization_id=42, name="Nope")

        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403
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
            can_read_all_project_events=False,
            can_manage_dev_branches=False,
            can_manage_tokens=False,
        )

        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["description"] == "custom-desc"
        assert body["canManageBuckets"] is False
        assert body["canReadAllFileUploads"] is False
        assert body["canReadAllProjectEvents"] is False
        assert body["canManageDevBranches"] is False
        assert body["canManageTokens"] is False
        client.close()

    def test_can_manage_tokens_default_true(self, httpx_mock) -> None:
        """By default canManageTokens is True (needed for Scheduler/Orchestrator)."""
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
        assert body["canManageTokens"] is True
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


class TestGetOrganization:
    """Tests for get_organization()."""

    def test_returns_organization_details(self, httpx_mock) -> None:
        """get_organization returns id + name (used by org setup to populate org_name)."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/organizations/438",
            json={"id": 438, "name": "Keboola Demo"},
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.get_organization(438)

        assert result["id"] == 438
        assert result["name"] == "Keboola Demo"
        client.close()


class TestGetProject:
    """Tests for get_project()."""

    def test_success(self, httpx_mock) -> None:
        """Returns project dict for accessible project."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/901",
            json={"id": 901, "name": "Padak", "organization": {"id": 438}},
            status_code=200,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        result = client.get_project(901)

        assert result["id"] == 901
        assert result["name"] == "Padak"
        assert result["organization"]["id"] == 438
        client.close()

    def test_403_not_member(self, httpx_mock) -> None:
        """Raises KeboolaApiError when user is not a project member."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/999",
            json={"error": "Access denied to project 999"},
            status_code=403,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.get_project(999)

        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403
        client.close()

    def test_404_not_found(self, httpx_mock) -> None:
        """Raises KeboolaApiError when project does not exist."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/999999",
            json={"error": "Project not found"},
            status_code=404,
        )

        client = ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            client.get_project(999999)

        assert exc_info.value.error_code == "NOT_FOUND"
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


# ──────────────────────────────────────────────────────────────────────
# Project members & invitations (since v0.26.1)
# ──────────────────────────────────────────────────────────────────────


_INVITATION_RESPONSE = {
    "id": 1741,
    "created": "2026-05-01T19:04:35+0200",
    "expires": None,
    "reason": "v0.26.1 verification",
    "role": "guest",
    "user": {"id": 1325, "email": "ottomansky.max@gmail.com", "name": ""},
    "creator": {"id": 216, "email": "max.ottomansky@keboola.com", "name": "Max"},
}

_MEMBER_LIST_RESPONSE = [
    {
        "id": 216,
        "name": "Max",
        "email": "max.ottomansky@keboola.com",
        "role": "admin",
        "status": "active",
        "mfaEnabled": True,
        "features": ["power-user"],
        "canAccessLogs": False,
    },
    {
        "id": 4241,
        "name": "Marcel",
        "email": "mfiser@cuestapartners.com",
        "role": "guest",
        "status": "active",
        "mfaEnabled": True,
        "features": [],
        "canAccessLogs": False,
    },
]


class TestCreateProjectInvitation:
    def test_success(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations",
            method="POST",
            json=_INVITATION_RESPONSE,
            status_code=201,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.create_project_invitation(
                project_id=5725,
                email="ottomansky.max@gmail.com",
                role="guest",
                reason="v0.26.1 verification",
            )
        assert result["id"] == 1741
        assert result["role"] == "guest"
        assert result["user"]["email"] == "ottomansky.max@gmail.com"

    def test_payload_contains_email_role_reason(self, httpx_mock) -> None:
        import json as _json

        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations",
            method="POST",
            json=_INVITATION_RESPONSE,
            status_code=201,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            client.create_project_invitation(
                project_id=5725,
                email="ottomansky.max@gmail.com",
                role="guest",
                reason="v0.26.1 verification",
            )
        body = _json.loads(httpx_mock.get_request().read())
        assert body == {
            "email": "ottomansky.max@gmail.com",
            "role": "guest",
            "reason": "v0.26.1 verification",
        }

    def test_omits_reason_when_none(self, httpx_mock) -> None:
        import json as _json

        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations",
            method="POST",
            json=_INVITATION_RESPONSE,
            status_code=201,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            client.create_project_invitation(project_id=5725, email="x@y.com", role="admin")
        body = _json.loads(httpx_mock.get_request().read())
        assert body == {"email": "x@y.com", "role": "admin"}

    def test_400_already_invited_surfaces_message(self, httpx_mock) -> None:
        """The 'already invited' 400 must round-trip the API's error text so
        the service layer can match its substring marker."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations",
            method="POST",
            json={"error": "This user has already been invited to this project."},
            status_code=400,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.create_project_invitation(project_id=5725, email="x@y.com", role="admin")
        assert exc_info.value.status_code == 400
        assert "already been invited" in exc_info.value.message


class TestListProjectInvitations:
    def test_returns_plain_list(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations",
            json=[_INVITATION_RESPONSE],
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.list_project_invitations(5725)
        assert isinstance(result, list)
        assert result[0]["user"]["email"] == "ottomansky.max@gmail.com"


class TestCancelProjectInvitation:
    def test_returns_none_on_204(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations/1741",
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.cancel_project_invitation(5725, 1741) is None

    def test_404_after_already_deleted(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/invitations/1741",
            method="DELETE",
            json={"error": "Invitation not found"},
            status_code=404,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.cancel_project_invitation(5725, 1741)
        assert exc_info.value.error_code == "NOT_FOUND"


class TestListProjectMembers:
    def test_returns_top_level_user_dicts(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/users",
            json=_MEMBER_LIST_RESPONSE,
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.list_project_members(5725)
        assert len(result) == 2
        assert result[0]["email"] == "max.ottomansky@keboola.com"
        # Role lives at the top level (not nested under a "user" key).
        assert result[0]["role"] == "admin"
        assert result[1]["role"] == "guest"


class TestRemoveProjectMember:
    def test_returns_none_on_204(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/users/216",
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.remove_project_member(5725, 216) is None

    def test_400_administrator_not_found(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/users/999",
            method="DELETE",
            json={"error": "Administrator not found"},
            status_code=400,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.remove_project_member(5725, 999)
        assert exc_info.value.status_code == 400


class TestUpdateProjectMemberRole:
    def test_uses_PATCH_not_PUT(self, httpx_mock) -> None:
        """Regression: PUT returns 404 even on real members; the client must
        emit PATCH."""
        import json as _json

        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/users/216",
            method="PATCH",
            json={"id": 216, "email": "max.ottomansky@keboola.com", "role": "guest"},
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.update_project_member_role(5725, 216, "guest")
        request = httpx_mock.get_request()
        assert request.method == "PATCH"
        assert _json.loads(request.read()) == {"role": "guest"}
        assert result["role"] == "guest"


# ──────────────────────────────────────────────────────────────────────
# Feature flags (super-admin manage token required)
# ──────────────────────────────────────────────────────────────────────


_FEATURES_RESPONSE = [
    {
        "id": 1,
        "name": "queuev2",
        "title": "Queue v2",
        "description": "New job queue",
        "type": "project",
        "canBeManagedViaApi": True,
    },
    {
        "id": 2,
        "name": "data-apps",
        "title": "Data Apps",
        "type": "admin",
    },
]


class TestListFeatures:
    def test_returns_catalogue_list(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/features",
            method="GET",
            json=_FEATURES_RESPONSE,
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.list_features()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "queuev2"
        # Unknown/extra keys round-trip untouched.
        assert result[0]["canBeManagedViaApi"] is True

    def test_403_without_super_admin(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/features",
            method="GET",
            json={"error": "Super admin required"},
            status_code=403,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.list_features()
        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403


class TestAddProjectFeature:
    def test_success_returns_body(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/features",
            method="POST",
            json={"feature": "queuev2", "added": True},
            status_code=201,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.add_project_feature(5725, "queuev2")
        assert result["feature"] == "queuev2"

    def test_payload_is_feature_object(self, httpx_mock) -> None:
        import json as _json

        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/features",
            method="POST",
            json={},
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            client.add_project_feature(5725, "queuev2")
        request = httpx_mock.get_request()
        assert request.method == "POST"
        assert _json.loads(request.read()) == {"feature": "queuev2"}

    def test_404_unknown_project(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/999/features",
            method="POST",
            json={"error": "Project not found"},
            status_code=404,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.add_project_feature(999, "queuev2")
        assert exc_info.value.error_code == "NOT_FOUND"


class TestRemoveProjectFeature:
    def test_returns_none_on_204(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/features/queuev2",
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.remove_project_feature(5725, "queuev2") is None

    def test_url_encodes_feature_name(self, httpx_mock) -> None:
        """A feature name with reserved characters is fully percent-encoded
        (quote(..., safe='')), so '/' and ' ' become %2F and %20."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/projects/5725/features/vendor%2Ffeat%20flag",
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.remove_project_feature(5725, "vendor/feat flag") is None
        assert "vendor%2Ffeat%20flag" in str(httpx_mock.get_request().url)


class TestGetUser:
    def test_success(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/jane@example.com",
            method="GET",
            json={
                "id": 42,
                "email": "jane@example.com",
                "features": ["queuev2", "data-apps"],
            },
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.get_user("jane@example.com")
        assert result["id"] == 42
        assert result["email"] == "jane@example.com"
        assert result["features"] == ["queuev2", "data-apps"]

    def test_url_keeps_at_and_dot_but_encodes_plus(self, httpx_mock) -> None:
        """Email is quote(email, safe='@'): '@' and '.' stay literal, but
        sub-address '+' is percent-encoded to %2B."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/jane%2Btag@example.com",
            method="GET",
            json={"id": 7, "email": "jane+tag@example.com", "features": []},
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.get_user("jane+tag@example.com")
        url = str(httpx_mock.get_request().url)
        assert "jane%2Btag@example.com" in url
        assert result["id"] == 7

    def test_404_unknown_user(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/nobody@example.com",
            method="GET",
            json={"error": "User not found"},
            status_code=404,
        )
        with (
            ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.get_user("nobody@example.com")
        assert exc_info.value.error_code == "NOT_FOUND"


class TestAddUserFeature:
    def test_success_returns_body(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/jane@example.com/features",
            method="POST",
            json={"feature": "queuev2", "added": True},
            status_code=201,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            result = client.add_user_feature("jane@example.com", "queuev2")
        assert result["feature"] == "queuev2"

    def test_payload_and_encoded_url(self, httpx_mock) -> None:
        import json as _json

        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/jane%2Btag@example.com/features",
            method="POST",
            json={},
            status_code=200,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            client.add_user_feature("jane+tag@example.com", "queuev2")
        request = httpx_mock.get_request()
        assert request.method == "POST"
        assert _json.loads(request.read()) == {"feature": "queuev2"}
        assert "jane%2Btag@example.com" in str(request.url)


class TestRemoveUserFeature:
    def test_returns_none_on_204(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/users/jane@example.com/features/queuev2",
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.remove_user_feature("jane@example.com", "queuev2") is None

    def test_encodes_both_email_and_feature(self, httpx_mock) -> None:
        """Email keeps '@'/'.' (safe='@') while the feature is fully encoded
        (safe='')."""
        httpx_mock.add_response(
            url=(f"{STACK_URL}/manage/users/jane%2Btag@example.com/features/vendor%2Fflag"),
            method="DELETE",
            status_code=204,
        )
        with ManageClient(stack_url=STACK_URL, manage_token=MANAGE_TOKEN) as client:
            assert client.remove_user_feature("jane+tag@example.com", "vendor/flag") is None
        url = str(httpx_mock.get_request().url)
        assert "jane%2Btag@example.com" in url
        assert "vendor%2Fflag" in url
