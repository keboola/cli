"""Tests for Pydantic models serialization and deserialization."""

import json

import pytest
from pydantic import ValidationError

from keboola_agent_cli.models import (
    AppConfig,
    ErrorResponse,
    Feature,
    ProjectConfig,
    SuccessResponse,
    TokenVerifyResponse,
    normalize_stack_url,
)


class TestProjectConfig:
    """Tests for ProjectConfig model."""

    def test_create_with_all_fields(self) -> None:
        """ProjectConfig can be created with all fields specified."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-secret-token",
            project_name="My Project",
            project_id=1234,
        )
        assert config.stack_url == "https://connection.keboola.com"
        assert config.token == "901-secret-token"
        assert config.project_name == "My Project"
        assert config.project_id == 1234

    def test_default_values(self) -> None:
        """ProjectConfig has sensible defaults for optional fields."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-token",
        )
        assert config.project_name == ""
        assert config.project_id is None
        assert config.org_id is None
        assert config.org_name is None

    def test_org_fields_persisted(self) -> None:
        """Organization fields round-trip through JSON serialization."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-token",
            org_id=438,
            org_name="Keboola Demo",
        )
        restored = ProjectConfig.model_validate_json(config.model_dump_json())
        assert restored.org_id == 438
        assert restored.org_name == "Keboola Demo"

    def test_legacy_config_without_org_fields(self) -> None:
        """Configs persisted before org fields existed still load cleanly."""
        legacy = json.dumps(
            {
                "stack_url": "https://connection.keboola.com",
                "token": "901-token",
                "project_name": "Legacy",
                "project_id": 100,
            }
        )
        restored = ProjectConfig.model_validate_json(legacy)
        assert restored.org_id is None
        assert restored.org_name is None

    def test_json_round_trip(self) -> None:
        """ProjectConfig can be serialized to JSON and deserialized back."""
        original = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-secret-token",
            project_name="My Project",
            project_id=1234,
        )
        json_str = original.model_dump_json()
        restored = ProjectConfig.model_validate_json(json_str)
        assert restored == original

    def test_json_output_is_valid(self) -> None:
        """ProjectConfig JSON output is valid JSON."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="token",
        )
        json_str = config.model_dump_json()
        parsed = json.loads(json_str)
        assert "stack_url" in parsed
        assert "token" in parsed


class TestAppConfig:
    """Tests for AppConfig model."""

    def test_empty_config(self) -> None:
        """AppConfig can be created with defaults (no projects)."""
        config = AppConfig()
        assert config.version == 1
        assert config.default_project == ""
        assert config.projects == {}

    def test_config_with_projects(self) -> None:
        """AppConfig can hold multiple project connections."""
        config = AppConfig(
            version=1,
            default_project="prod-aws",
            projects={
                "prod-aws": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-token",
                    project_name="Production",
                    project_id=1001,
                ),
                "dev-azure": ProjectConfig(
                    stack_url="https://connection.north-europe.azure.keboola.com",
                    token="532-token",
                    project_name="Development",
                    project_id=2002,
                ),
            },
        )
        assert len(config.projects) == 2
        assert "prod-aws" in config.projects
        assert "dev-azure" in config.projects
        assert config.projects["prod-aws"].project_id == 1001

    def test_json_round_trip(self) -> None:
        """AppConfig can be serialized to JSON and deserialized back."""
        original = AppConfig(
            version=1,
            default_project="test",
            projects={
                "test": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-token",
                    project_name="Test",
                    project_id=999,
                ),
            },
        )
        json_str = original.model_dump_json()
        restored = AppConfig.model_validate_json(json_str)
        assert restored == original
        assert restored.projects["test"].project_name == "Test"

    def test_json_output_structure(self) -> None:
        """AppConfig JSON output has the expected top-level keys."""
        config = AppConfig(
            version=1,
            default_project="prod",
            projects={
                "prod": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="t",
                ),
            },
        )
        parsed = json.loads(config.model_dump_json())
        assert parsed["version"] == 1
        assert parsed["default_project"] == "prod"
        assert "prod" in parsed["projects"]
        assert parsed["projects"]["prod"]["stack_url"] == "https://connection.keboola.com"


class TestErrorResponse:
    """Tests for ErrorResponse model."""

    def test_create(self) -> None:
        """ErrorResponse can be created with all fields."""
        err = ErrorResponse(
            code="INVALID_TOKEN",
            message="Token is invalid or expired",
            project="prod-aws",
            retryable=False,
        )
        assert err.code == "INVALID_TOKEN"
        assert err.message == "Token is invalid or expired"
        assert err.project == "prod-aws"
        assert err.retryable is False

    def test_defaults(self) -> None:
        """ErrorResponse has empty project, retryable=False, and error_type='unknown' by default."""
        err = ErrorResponse(code="ERR", message="Something failed")
        assert err.project == ""
        assert err.retryable is False
        assert err.error_type == "unknown"

    def test_error_type_explicit(self) -> None:
        """ErrorResponse accepts an explicit error_type value."""
        err = ErrorResponse(
            code="INVALID_TOKEN",
            error_type="authentication",
            message="Token expired",
        )
        assert err.error_type == "authentication"

    def test_json_serialization(self) -> None:
        """ErrorResponse serializes to valid JSON with expected keys including error_type."""
        err = ErrorResponse(
            code="NETWORK_ERROR",
            error_type="network",
            message="Connection timed out",
            project="dev",
            retryable=True,
        )
        parsed = json.loads(err.model_dump_json())
        assert parsed["code"] == "NETWORK_ERROR"
        assert parsed["error_type"] == "network"
        assert parsed["retryable"] is True

    def test_json_serialization_default_error_type(self) -> None:
        """ErrorResponse JSON includes error_type with default value 'unknown'."""
        err = ErrorResponse(code="ERR", message="Something failed")
        parsed = json.loads(err.model_dump_json())
        assert parsed["error_type"] == "unknown"


class TestSuccessResponse:
    """Tests for SuccessResponse model."""

    def test_with_list_data(self) -> None:
        """SuccessResponse can hold a list as data payload."""
        resp = SuccessResponse(status="ok", data=[{"name": "item1"}, {"name": "item2"}])
        assert resp.status == "ok"
        assert len(resp.data) == 2

    def test_with_empty_data(self) -> None:
        """SuccessResponse can hold None or empty data."""
        resp = SuccessResponse()
        assert resp.status == "ok"
        assert resp.data is None

    def test_json_serialization(self) -> None:
        """SuccessResponse serializes with status and data keys."""
        resp = SuccessResponse(status="ok", data={"message": "done"})
        parsed = json.loads(resp.model_dump_json())
        assert parsed["status"] == "ok"
        assert parsed["data"]["message"] == "done"

    def test_json_round_trip(self) -> None:
        """SuccessResponse can be round-tripped through JSON."""
        original = SuccessResponse(status="ok", data=["a", "b", "c"])
        json_str = original.model_dump_json()
        restored = SuccessResponse.model_validate_json(json_str)
        assert restored == original


class TestStackUrlValidation:
    """Tests for S2: URL validation on ProjectConfig.stack_url."""

    def test_project_add_rejects_http_url(self) -> None:
        """http:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="http://connection.keboola.com",
                token="901-token",
            )

    def test_project_add_rejects_file_url(self) -> None:
        """file:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="file:///etc/passwd",
                token="901-token",
            )

    def test_project_add_rejects_ftp_url(self) -> None:
        """ftp:// URL is rejected with a ValidationError."""
        with pytest.raises(ValidationError, match="https://"):
            ProjectConfig(
                stack_url="ftp://connection.keboola.com",
                token="901-token",
            )

    def test_bare_host_is_normalized_to_https(self) -> None:
        """A bare host (no scheme) gets https:// prepended instead of rejected."""
        config = ProjectConfig(
            stack_url="connection.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_full_project_link_reduced_to_base(self) -> None:
        """A full project deep-link is reduced to scheme+host."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com/admin/projects/10105/dashboard",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_trailing_slash_stripped(self) -> None:
        """A trailing slash is dropped from the normalized base URL."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com/",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_bare_host_with_path_reduced_to_base(self) -> None:
        """A bare host + path (no scheme) normalizes to https://<host>."""
        config = ProjectConfig(
            stack_url="connection.north-europe.azure.keboola.com/admin/projects/7",
            token="901-token",
        )
        assert config.stack_url == "https://connection.north-europe.azure.keboola.com"

    def test_surrounding_whitespace_trimmed(self) -> None:
        """Leading/trailing whitespace (paste artifact) is trimmed."""
        config = ProjectConfig(
            stack_url="  https://connection.keboola.com  ",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_empty_url_rejected(self) -> None:
        """An empty / whitespace-only URL is rejected."""
        with pytest.raises(ValidationError, match="empty"):
            ProjectConfig(stack_url="   ", token="901-token")

    def test_project_add_accepts_https_url(self) -> None:
        """https:// URL is accepted without error."""
        config = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.keboola.com"

    def test_project_add_accepts_https_azure(self) -> None:
        """https:// Azure stack URL is accepted."""
        config = ProjectConfig(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.north-europe.azure.keboola.com"

    def test_project_add_accepts_https_gcp(self) -> None:
        """https:// GCP stack URL is accepted."""
        config = ProjectConfig(
            stack_url="https://connection.europe-west3.gcp.keboola.com",
            token="901-token",
        )
        assert config.stack_url == "https://connection.europe-west3.gcp.keboola.com"


class TestStackUrlCanonicalization:
    """The returned string is used as a dict key, so one stack must yield one key.

    `auth.json`'s `sessions` map and the session-token provider registry are both
    keyed on this value directly, so a host spelled two ways would otherwise
    become two sessions: a login as `Connection.Keboola.Com` invisible to a later
    `connection.keboola.com` lookup.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            "Connection.Keboola.Com",
            "CONNECTION.KEBOOLA.COM",
            "https://Connection.Keboola.Com",
            "https://CONNECTION.keboola.com/admin/projects/10105/dashboard",
            "  https://Connection.KEBOOLA.com/  ",
        ],
    )
    def test_every_spelling_of_one_host_collapses_to_one_key(self, spelling: str) -> None:
        assert normalize_stack_url(spelling) == "https://connection.keboola.com"

    def test_explicit_port_is_preserved(self) -> None:
        """A non-default port distinguishes two real endpoints; only the host folds."""
        assert (
            normalize_stack_url("https://Connection.Keboola.Com:8443/admin")
            == "https://connection.keboola.com:8443"
        )

    def test_credentials_in_the_url_are_dropped(self) -> None:
        """`project list` echoes the stored stack URL to the terminal, so a
        `user:password@` must never survive into the persisted value. Basic auth
        was never how this API authenticates -- the token is a header."""
        assert (
            normalize_stack_url("https://User:Pass@Connection.Keboola.Com")
            == "https://connection.keboola.com"
        )

    def test_ipv6_literal_keeps_its_brackets(self) -> None:
        """`urlparse().hostname` strips the brackets that URL syntax requires."""
        assert normalize_stack_url("https://[FE80::1]:8443/x") == "https://[fe80::1]:8443"

    def test_unparseable_port_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid port"):
            normalize_stack_url("https://connection.keboola.com:notaport")

    def test_lowercasing_survives_a_config_round_trip(self) -> None:
        """The field validator runs on load too, so an existing config.json with a
        mixed-case host normalizes itself without a migration."""
        loaded = ProjectConfig.model_validate(
            {"stack_url": "https://Connection.Keboola.Com", "token": "901-token"}
        )
        assert loaded.stack_url == "https://connection.keboola.com"


class TestTokenVerifyResponseValidation:
    """Tests for Phase 6: TokenVerifyResponse required fields and project_id default."""

    def test_token_verify_response_rejects_missing_fields(self) -> None:
        """TokenVerifyResponse with missing required fields raises ValidationError."""
        with pytest.raises(ValidationError):
            TokenVerifyResponse(  # ty: ignore[missing-argument]
                token_id="123",
                token_description="My Token",
                # project_name missing
                # owner_name missing
            )

    def test_token_verify_response_rejects_missing_owner_name(self) -> None:
        """TokenVerifyResponse with missing owner_name raises ValidationError."""
        with pytest.raises(ValidationError, match="owner_name"):
            TokenVerifyResponse(  # ty: ignore[missing-argument]
                token_id="123",
                token_description="My Token",
                project_name="Test Project",
                # owner_name missing
            )

    def test_token_verify_response_rejects_missing_token_id(self) -> None:
        """TokenVerifyResponse with missing token_id raises ValidationError."""
        with pytest.raises(ValidationError, match="token_id"):
            TokenVerifyResponse(  # ty: ignore[missing-argument]
                token_description="My Token",
                project_name="Test Project",
                owner_name="Test Owner",
            )

    def test_token_verify_response_rejects_missing_token_description(self) -> None:
        """TokenVerifyResponse with missing token_description raises ValidationError."""
        with pytest.raises(ValidationError, match="token_description"):
            TokenVerifyResponse(  # ty: ignore[missing-argument]
                token_id="123",
                project_name="Test Project",
                owner_name="Test Owner",
            )

    def test_token_verify_response_rejects_missing_project_name(self) -> None:
        """TokenVerifyResponse with missing project_name raises ValidationError."""
        with pytest.raises(ValidationError, match="project_name"):
            TokenVerifyResponse(  # ty: ignore[missing-argument]
                token_id="123",
                token_description="My Token",
                owner_name="Test Owner",
            )

    def test_project_id_default_none(self) -> None:
        """TokenVerifyResponse project_id defaults to None, not 0."""
        response = TokenVerifyResponse(
            token_id="123",
            token_description="My Token",
            project_name="Test Project",
            owner_name="Test Owner",
        )
        assert response.project_id is None

    def test_token_verify_response_with_all_fields(self) -> None:
        """TokenVerifyResponse with all fields specified works correctly."""
        response = TokenVerifyResponse(
            token_id="123",
            token_description="My Token",
            project_id=4567,
            project_name="Test Project",
            owner_name="Test Owner",
        )
        assert response.token_id == "123"
        assert response.token_description == "My Token"
        assert response.project_id == 4567
        assert response.project_name == "Test Project"
        assert response.owner_name == "Test Owner"


class TestMaxParallelWorkersValidation:
    """Tests for max_parallel_workers upper bound validation."""

    def test_max_workers_upper_bound(self) -> None:
        """max_parallel_workers > 100 raises ValidationError."""
        with pytest.raises(ValidationError, match="less than or equal to 100"):
            AppConfig(max_parallel_workers=200)

    def test_max_workers_at_100_is_valid(self) -> None:
        """max_parallel_workers = 100 is accepted."""
        config = AppConfig(max_parallel_workers=100)
        assert config.max_parallel_workers == 100

    def test_max_workers_default_is_valid(self) -> None:
        """Default max_parallel_workers (10) is accepted."""
        config = AppConfig()
        assert config.max_parallel_workers == 10

    def test_max_workers_at_1_is_valid(self) -> None:
        """max_parallel_workers = 1 is accepted."""
        config = AppConfig(max_parallel_workers=1)
        assert config.max_parallel_workers == 1

    def test_max_workers_zero_rejected(self) -> None:
        """max_parallel_workers = 0 raises ValidationError (issue #269 sec-11).

        Pre-fix this passed Pydantic validation, then ThreadPoolExecutor
        crashed with ``ValueError: max_workers must be greater than 0`` on
        every multi-project operation."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            AppConfig(max_parallel_workers=0)

    def test_max_workers_negative_rejected(self) -> None:
        """max_parallel_workers < 0 raises ValidationError."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            AppConfig(max_parallel_workers=-5)


class TestFeature:
    """Tests for the Feature model (Keboola feature flag)."""

    def test_full_object_with_extras_passes_through(self) -> None:
        """A feature dict with known fields plus unknown extras validates and
        keeps the extras (model_config extra='allow')."""
        feature = Feature.model_validate(
            {
                "name": "queuev2",
                "title": "Queue v2",
                "description": "New job queue",
                "type": "project",
                "canBeManagedViaApi": True,
                "id": 1,
            }
        )
        assert feature.name == "queuev2"
        assert feature.title == "Queue v2"
        assert feature.description == "New job queue"
        assert feature.type == "project"
        dumped = feature.model_dump()
        # Extra keys survive serialization untouched.
        assert dumped["canBeManagedViaApi"] is True
        assert dumped["id"] == 1

    def test_defaults_when_empty_dict(self) -> None:
        """An empty dict yields safe empty-string defaults for every field."""
        feature = Feature.model_validate({})
        assert feature.name == ""
        assert feature.title == ""
        assert feature.description == ""
        assert feature.type == ""

    def test_minimal_name_only(self) -> None:
        """The common normalized shape {'name': <string>} validates and the
        other fields fall back to their defaults."""
        feature = Feature.model_validate({"name": "data-apps"})
        assert feature.name == "data-apps"
        assert feature.title == ""
        assert feature.type == ""

    def test_model_dump_includes_declared_and_extra_fields(self) -> None:
        """model_dump emits the declared fields plus any extras."""
        feature = Feature.model_validate({"name": "x", "title": "X", "adminFeature": False})
        dumped = feature.model_dump()
        assert dumped["name"] == "x"
        assert dumped["title"] == "X"
        assert dumped["description"] == ""
        assert dumped["type"] == ""
        assert dumped["adminFeature"] is False

    def test_json_round_trip_preserves_extras(self) -> None:
        """Feature survives a JSON round-trip including extra keys."""
        original = Feature.model_validate(
            {"name": "queuev2", "title": "Queue v2", "projectFeature": True}
        )
        restored = Feature.model_validate_json(original.model_dump_json())
        assert restored.name == "queuev2"
        assert restored.model_dump()["projectFeature"] is True


class TestProjectConfigBackwardCompat:
    """Tests for backward compatibility of ProjectConfig with active_branch_id."""

    def test_project_config_without_active_branch_id(self) -> None:
        """ProjectConfig created from dict without active_branch_id defaults to None."""
        data = {
            "stack_url": "https://connection.keboola.com",
            "token": "901-secret-token",
            "project_name": "My Project",
            "project_id": 1234,
        }
        config = ProjectConfig.model_validate(data)
        assert config.active_branch_id is None

    def test_project_config_with_active_branch_id(self) -> None:
        """ProjectConfig created with active_branch_id preserves the value."""
        data = {
            "stack_url": "https://connection.keboola.com",
            "token": "901-secret-token",
            "project_name": "My Project",
            "project_id": 1234,
            "active_branch_id": 123,
        }
        config = ProjectConfig.model_validate(data)
        assert config.active_branch_id == 123


class TestDeveloperPortalIdentity:
    """Tests for DeveloperPortalIdentity model."""

    def test_minimal_construction(self) -> None:
        """DeveloperPortalIdentity can be created with minimal required fields."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(username="service.keboola.x", password="p")
        assert ident.username == "service.keboola.x"
        assert ident.password == "p"
        assert ident.role_hint == "vendor"
        assert ident.vendor is None
        assert ident.portal_url == "https://apps-api.keboola.com"

    def test_rejects_non_https_portal_url(self) -> None:
        """DeveloperPortalIdentity rejects non-https portal_url."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        with pytest.raises(ValidationError, match="https"):
            DeveloperPortalIdentity(
                username="u",
                password="p",
                portal_url="http://apps-api.keboola.com",
            )

    def test_accepts_staging_https_portal_url(self) -> None:
        """DeveloperPortalIdentity accepts https staging URL."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(
            username="u",
            password="p",
            portal_url="https://apps-api.staging.keboola.dev",
        )
        assert ident.portal_url == "https://apps-api.staging.keboola.dev"

    def test_role_hint_accepts_admin(self) -> None:
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(username="u", password="p", role_hint="admin")
        assert ident.role_hint == "admin"

    def test_role_hint_normalises_case(self) -> None:
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(username="u", password="p", role_hint="ADMIN")
        assert ident.role_hint == "admin"

    def test_role_hint_typo_downgrades_to_vendor_with_warning(self, capsys) -> None:
        """Typos do NOT raise: pre-0.51.1 configs had free-text values, so we
        normalise unknown strings to 'vendor' with a stderr warning to keep
        ConfigStore.load() from crashing the CLI on upgrade."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(username="u", password="p", role_hint="vendr")
        assert ident.role_hint == "vendor"
        captured = capsys.readouterr()
        assert "role_hint" in captured.err
        assert "downgrading" in captured.err

    def test_legacy_freetext_role_hint_loads_cleanly(self, capsys) -> None:
        """Backwards compat: a config.json carrying any free-text role_hint
        (allowed pre-0.51.1) must round-trip through Pydantic without raising.
        Empty strings, hand-edited values, even non-string types get normalised."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        for legacy in ("keboola-admin", "", "  ADMIN  ", 42):
            ident = DeveloperPortalIdentity.model_validate(
                {"username": "u", "password": "p", "role_hint": legacy}
            )
            assert ident.role_hint in ("vendor", "admin")


class TestAppConfigDevPortalFields:
    """Tests for AppConfig dev_portal_identities and default_dev_portal_identity fields."""

    def test_defaults_empty(self) -> None:
        """AppConfig dev_portal_identities and default_dev_portal_identity default to empty."""
        cfg = AppConfig()
        assert cfg.dev_portal_identities == {}
        assert cfg.default_dev_portal_identity == ""

    def test_round_trip(self) -> None:
        """AppConfig with dev portal identities round-trips through JSON."""
        from keboola_agent_cli.models import DeveloperPortalIdentity

        ident = DeveloperPortalIdentity(username="u", password="p", vendor="keboola")
        cfg = AppConfig(
            dev_portal_identities={"vendor-keboola": ident},
            default_dev_portal_identity="vendor-keboola",
        )
        round_trip = AppConfig.model_validate(cfg.model_dump(mode="json"))
        assert round_trip.dev_portal_identities["vendor-keboola"].vendor == "keboola"
        assert round_trip.default_dev_portal_identity == "vendor-keboola"
