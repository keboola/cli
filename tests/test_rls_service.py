"""Service-layer tests for ``RlsService`` and the ``_rls_condition`` helpers.

Each test injects a ``unittest.mock.MagicMock`` as the metastore client
factory so we verify orchestration (scope resolution, validation order, call
sequencing) without touching HTTP -- mirrors ``test_semantic_layer_service.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import ANY, MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.json_utils import draft7_errors
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._rls_condition import (
    compile_condition_preview,
    validate_condition_ops,
    validate_rules_local,
)
from keboola_agent_cli.services.rls_service import RlsService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, alias: str = "prod") -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name=alias,
            project_id=5725,
        ),
    )
    return store


def _make_service(
    store: ConfigStore, *, metastore_mock: MagicMock | None = None
) -> tuple[RlsService, MagicMock]:
    mock = metastore_mock or MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    service = RlsService(
        config_store=store,
        client_factory=storage_client_factory(),
        metastore_client_factory=lambda url, token: mock,
    )
    mock.list_items.return_value = []
    return service, mock


def storage_client_factory(backend: str = "snowflake") -> Any:
    """A Storage client factory whose token belongs to a project on ``backend``."""
    storage = MagicMock()
    storage.__enter__ = MagicMock(return_value=storage)
    storage.__exit__ = MagicMock(return_value=False)
    storage.verify_token.return_value.default_backend = backend
    return lambda url, token: storage


def _policy_item(
    item_id: str = "p-1",
    table: str = "in.c-crm.invoices",
    dialect: str = "snowflake",
    rules: list[dict[str, Any]] | None = None,
    scope: str = "organization",
    source_project_id: str | None = "5725",
    target_project_ids: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "rls-policy",
        "id": item_id,
        "attributes": {
            "table": table,
            "dialect": dialect,
            "rules": rules
            if rules is not None
            else [{"principal": "a@x.com", "condition": {"true": True}}],
        },
        "meta": {
            "scope": scope,
            "sourceProjectId": source_project_id,
            "targetProjectIds": target_project_ids or [],
            "revision": 1,
        },
    }


_RULES = [{"principal": "a@x.com", "condition": {"column": "region", "op": "eq", "value": "EU"}}]

# `1 == True` and any non-True value would otherwise read as "true": only `{"true": true}` is the sentinel.
_BAD_TRUE_SENTINELS = [
    {"true": False},
    {"true": 0},
    {"true": 1},
    {"true": None},
    {"true": "yes"},
    {"true": True, "extra": 1},
]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class TestListPolicies:
    def test_returns_rows_from_list_items(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.list_items.return_value = [_policy_item()]

        result = service.list_policies("prod")

        mock.list_items.assert_called_once_with("rls-policy")
        assert result["project"] == "prod"
        assert result["policies"][0]["id"] == "p-1"
        assert result["policies"][0]["rule_count"] == 1
        assert result["policies"][0]["scope"] == "organization"


class TestGetPolicy:
    def test_returns_full_rules(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _policy_item()

        result = service.get_policy("prod", "p-1")

        mock.get_item.assert_called_once_with("rls-policy", "p-1")
        assert result["rules"] == [{"principal": "a@x.com", "condition": {"true": True}}]
        assert result["revision"] == 1


class TestFetchSchema:
    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.return_value = {"type": "object"}

        fetch = service.fetch_schema("prod")

        assert fetch.schema == {"type": "object"}
        assert fetch.reason is None

    def test_api_error_degrades(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(
            message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
        )

        fetch = service.fetch_schema("prod")

        assert fetch.schema is None
        assert fetch.reason == "not found"

    @pytest.mark.parametrize(
        ("error_code", "status"),
        [
            (ErrorCode.INVALID_TOKEN, 401),
            (ErrorCode.MISSING_MASTER_TOKEN, 403),
            (ErrorCode.ACCESS_DENIED, 403),
            (ErrorCode.PERMISSION_DENIED, 403),
            (ErrorCode.API_ERROR, 401),  # by status alone
            (ErrorCode.API_ERROR, 403),
        ],
    )
    def test_auth_and_permission_errors_are_not_reported_as_a_missing_schema(
        self, tmp_path: Path, error_code: ErrorCode, status: int
    ) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(
            message="nope", status_code=status, error_code=error_code
        )

        with pytest.raises(KeboolaApiError) as excinfo:
            service.fetch_schema("prod")

        assert excinfo.value.error_code == error_code

    @pytest.mark.parametrize("status", [404, 500, 502])
    def test_other_api_errors_still_degrade(self, tmp_path: Path, status: int) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(
            message="unavailable", status_code=status, error_code=ErrorCode.API_ERROR
        )

        fetch = service.fetch_schema("prod")

        assert fetch.schema is None
        assert fetch.reason == "unavailable"

    @pytest.mark.parametrize(
        "malformed", [{"type": 123}, {"properties": {"table": {"type": "nope"}}}]
    )
    def test_a_non_empty_but_malformed_schema_degrades_instead_of_raising(
        self, tmp_path: Path, malformed: dict
    ) -> None:
        """`Draft7Validator(schema)` would raise SchemaError mid-validation: report it as unavailable."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = malformed
        mock.post_item.return_value = _policy_item()

        fetch = service.fetch_schema("prod")
        assert fetch.schema is None
        assert "malformed" in (fetch.reason or "")

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES)
        assert any("malformed" in w for w in result["warnings"])
        mock.post_item.assert_called_once()

    def test_unexpected_error_degrades(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = RuntimeError("boom")

        fetch = service.fetch_schema("prod")

        assert fetch.schema is None
        assert "boom" in (fetch.reason or "")

    def test_empty_schema_degrades(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.return_value = {}

        fetch = service.fetch_schema("prod")

        assert fetch.schema is None
        assert fetch.reason is not None


# ---------------------------------------------------------------------------
# Write: create
# ---------------------------------------------------------------------------


class TestCreatePolicy:
    def test_default_scope_is_targeted_with_no_grants(self, tmp_path: Path) -> None:
        """The schema's own default: only the owning project is governed until grants are added."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item(scope="targeted")

        service.create_policy("prod", table="in.c-crm.invoices", rules=_RULES)

        kwargs = mock.post_item.call_args.kwargs
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_project_ids"] is None
        assert kwargs["data"]["dialect"] == "snowflake"  # defaulted from the project backend
        mock.put_target_projects.assert_not_called()

    def test_organization_scope_only_when_asked_for(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item()

        service.create_policy("prod", table="t.x", rules=_RULES, scope="organization")

        assert mock.post_item.call_args.kwargs["scope"] == "organization"

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"scope": "project"}, "scope must be one of"),
            ({"scope": "organization", "target_projects": ["7"]}, "require scope 'targeted'"),
        ],
    )
    def test_bad_scope_combinations_are_usage_errors(
        self, tmp_path: Path, kwargs: dict, fragment: str
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError, match=fragment) as excinfo:
            service.create_policy("prod", table="t.x", rules=_RULES, **kwargs)

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.post_item.assert_not_called()

    def test_target_projects_go_in_the_create_request_only(self, tmp_path: Path) -> None:
        """The POST stores the grants; a second grants call could fail after the policy exists."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item(scope="targeted", target_project_ids=[999])

        service.create_policy("prod", table="t.x", rules=_RULES, target_projects=["999", "999"])

        assert mock.post_item.call_args.kwargs["target_project_ids"] == [999]  # de-duplicated
        mock.put_target_projects.assert_not_called()

    @pytest.mark.parametrize(
        ("backend", "dialect"), [("snowflake", "bigquery"), ("bigquery", "snowflake")]
    )
    def test_a_dialect_other_than_the_backend_is_refused(
        self, tmp_path: Path, backend: str, dialect: str
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        service._client_factory = storage_client_factory(backend)

        with pytest.raises(KeboolaApiError, match="does not match the project backend") as excinfo:
            service.create_policy("prod", table="t.x", dialect=dialect, rules=_RULES)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()

    def test_a_backend_without_policy_support_is_refused(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        service._client_factory = storage_client_factory("synapse")

        with pytest.raises(KeboolaApiError, match="synapse"):
            service.create_policy("prod", table="t.x", rules=_RULES)

        mock.post_item.assert_not_called()

    def test_a_duplicate_name_gets_a_policy_remedy_not_the_semantic_one(
        self, tmp_path: Path
    ) -> None:
        """Policies are named by table: the 409 remedy names `rls update` / `rls delete`."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item()

        service.create_policy("prod", table="t.x", rules=_RULES)

        hint = mock.post_item.call_args.kwargs["conflict_hint"]
        assert "rls update --policy-id" in hint
        assert "rls delete" in hint

    @pytest.mark.parametrize(
        "bad_rules",
        [
            [{"principal": "a@x.com", "condition": {"column": "x", "op": "bogus", "value": 1}}],
            [{"principal": "a@x.com", "principals": ["b@x.com"], "condition": {"true": True}}],
        ],
    )
    def test_malformed_rules_are_rejected_before_any_write(
        self, tmp_path: Path, bad_rules: list
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError):
            service.create_policy("prod", table="t.x", rules=bad_rules)

        mock.post_item.assert_not_called()

    def test_structural_validation_runs_when_schema_available(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        # A schema that requires a `description` field the candidate body lacks.
        mock.get_schema.return_value = {
            "type": "object",
            "required": ["table", "dialect", "rules", "description"],
        }

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t.x", rules=_RULES)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()

    def test_dry_run_never_calls_post_item(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.create_policy("prod", table="t.x", rules=_RULES, dry_run=True)

        mock.post_item.assert_not_called()
        assert result["dry_run"] is True
        assert result["scope"] == "targeted"
        assert result["preview"][0]["condition"] == "\"region\" = 'EU'"


class TestSchema110:
    """Policy schema 1.1.0: groups, OR-combined rules, `default`, the false sentinel, `$identity`."""

    def test_one_identity_in_several_rules_is_accepted(self, tmp_path: Path) -> None:
        """The 1.1.0 engine ORs the conditions of every rule an identity matches."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item()
        rules = [
            {"principal": "a@x.com", "condition": {"column": "r", "op": "eq", "value": "EU"}},
            {"principals": ["A@X.com"], "condition": {"column": "r", "op": "eq", "value": "US"}},
        ]

        service.create_policy("prod", table="t.x", rules=rules)

        mock.post_item.assert_called_once()
        mock.list_items.assert_not_called()  # no cross-policy principal scan any more

    @pytest.mark.parametrize(
        "rule",
        [
            {"groups": ["sales-eu", "Sales EU"], "condition": {"true": True}},
            {"groups": ["g"], "condition": {"false": True}},
            {
                "groups": ["g"],
                "condition": {"column": "o", "op": "eq", "value": {"$identity": "email"}},
            },
            {
                "groups": ["g"],
                "condition": {"column": "t", "op": "in", "values": {"$identity": "groups"}},
            },
        ],
    )
    def test_valid_110_rules(self, rule: dict) -> None:
        assert validate_rules_local([rule]) == []

    @pytest.mark.parametrize(
        ("rule", "fragment"),
        [
            ({"groups": [], "condition": {"true": True}}, "groups must be a non-empty list"),
            ({"groups": ["a\x00"], "condition": {"true": True}}, "control characters"),
            ({"principal": "a", "groups": ["g"], "condition": {"true": True}}, "exactly one of"),
            ({"groups": ["g"], "condition": {"false": False}}, 'exactly {"false": true}'),
            (
                {
                    "groups": ["g"],
                    "condition": {"column": "c", "op": "eq", "value": {"$identity": "groups"}},
                },
                "string, number or boolean 'value'",
            ),
            (
                {
                    "groups": ["g"],
                    "condition": {"column": "c", "op": "in", "values": {"$identity": "email"}},
                },
                "non-empty list 'values'",
            ),
        ],
    )
    def test_invalid_110_rules(self, rule: dict, fragment: str) -> None:
        errors = validate_rules_local([rule])
        assert errors and fragment in errors[0]

    def test_default_is_validated_sent_and_previewed(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item()

        result = service.create_policy("prod", table="t.x", rules=_RULES, default={"false": True})

        assert mock.post_item.call_args.kwargs["data"]["default"] == {"false": True}
        assert result["default_preview"] == "FALSE"

    def test_an_invalid_default_is_refused(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError, match=r"default\.'false' condition"):
            service.create_policy("prod", table="t.x", rules=_RULES, default={"false": 1})

        mock.post_item.assert_not_called()

    def test_update_patches_the_default_and_keeps_a_missing_dialect(self, tmp_path: Path) -> None:
        """A 1.1.0 policy may omit `dialect`: the merge falls back to the backend, not to ''."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        stored = _policy_item()
        del stored["attributes"]["dialect"]
        mock.get_item.return_value = stored

        service.update_policy("prod", "p-1", default={"true": True})

        mock.patch_item.assert_called_once_with(
            "rls-policy", "p-1", name=None, data={"default": {"true": True}}, conflict_hint=ANY
        )


# ---------------------------------------------------------------------------
# Write: update
# ---------------------------------------------------------------------------


class TestUpdatePolicy:
    def test_patches_only_the_changed_keys_and_validates_the_merged_policy(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item(table="in.c-crm.invoices", dialect="snowflake")
        mock.get_schema.return_value = {"type": "object", "required": ["table", "dialect", "rules"]}

        service.update_policy("prod", "p-1", rules=_RULES)

        mock.patch_item.assert_called_once_with(
            "rls-policy", "p-1", name=None, data={"rules": _RULES}, conflict_hint=ANY
        )
        mock.put_item.assert_not_called()

    def test_a_new_table_renames_the_policy(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item()
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        service.update_policy("prod", "p-1", table="in.c-crm.new")

        mock.patch_item.assert_called_once_with(
            "rls-policy",
            "p-1",
            name="in.c-crm.new",
            data={"table": "in.c-crm.new"},
            conflict_hint=ANY,
        )

    def test_nothing_to_update_is_a_usage_error(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.update_policy("prod", "p-1")

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.get_item.assert_not_called()

    def test_the_result_is_re_read_after_the_write(self, tmp_path: Path) -> None:
        """The grants change after the PATCH response was built -- report what is stored now."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        before = _policy_item(scope="targeted", target_project_ids=[7])
        after = _policy_item(scope="targeted", target_project_ids=[8])
        mock.get_item.side_effect = [before, after]

        result = service.update_policy("prod", "p-1", target_projects=["8"])

        assert result["target_project_ids"] == [8]

    def test_grants_change_before_the_rules(self, tmp_path: Path) -> None:
        """Granting is organization-admin only: when it is refused, the rules stay unwritten."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = _policy_item(scope="targeted")
        mock.put_target_projects.side_effect = KeboolaApiError(
            message="Insufficient permissions", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )

        with pytest.raises(KeboolaApiError):
            service.update_policy("prod", "p-1", rules=_RULES, target_projects=["8"])

        mock.patch_item.assert_not_called()

    def test_an_organization_policy_cannot_be_narrowed_to_target_projects(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item(scope="organization")

        with pytest.raises(KeboolaApiError) as excinfo:
            service.update_policy("prod", "p-1", target_projects=["42"])

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.patch_item.assert_not_called()
        mock.put_target_projects.assert_not_called()

    def test_a_dialect_other_than_the_backend_is_refused(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item()

        with pytest.raises(KeboolaApiError, match="does not match the project backend"):
            service.update_policy("prod", "p-1", dialect="bigquery")

        mock.patch_item.assert_not_called()

    def test_dry_run_never_writes(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item(scope="targeted", target_project_ids=[7])
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.update_policy("prod", "p-1", rules=_RULES, dry_run=True)

        mock.patch_item.assert_not_called()
        mock.put_target_projects.assert_not_called()
        assert result["dry_run"] is True
        assert result["target_project_ids"] == [7]


class TestGrants:
    """`target_projects`: None keeps the grants, a list replaces them, [] revokes them all."""

    @pytest.mark.parametrize(
        ("targets", "expected_call"),
        [(None, None), ([], []), (["7", "8,9"], [7, 8, 9])],
    )
    def test_target_projects_semantics(
        self, tmp_path: Path, targets: list | None, expected_call: list | None
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = _policy_item(scope="targeted", target_project_ids=[7])

        service.update_policy("prod", "p-1", rules=_RULES, target_projects=targets)

        if expected_call is None:
            mock.put_target_projects.assert_not_called()
        else:
            mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", expected_call)

    def test_a_registered_alias_resolves_to_its_project_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.add_project(
            "shared",
            ProjectConfig(
                stack_url="https://connection.keboola.com", token=TEST_TOKEN, project_id=77
            ),
        )
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.create_policy(
            "prod", table="t.x", rules=_RULES, target_projects=["shared"], dry_run=True
        )

        assert result["target_project_ids"] == [77]

    @pytest.mark.parametrize("bad", [["abc"], ["0"], ["-3"], ["1", "x"]])
    def test_a_bad_target_is_rejected_before_any_network_call(
        self, tmp_path: Path, bad: list
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        for call in (
            lambda: service.create_policy(
                "prod", table="t.x", rules=_RULES, target_projects=bad, dry_run=True
            ),
            lambda: service.update_policy("prod", "p-1", target_projects=bad, dry_run=True),
        ):
            with pytest.raises(KeboolaApiError) as excinfo:
                call()
            assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.get_item.assert_not_called()
        mock.post_item.assert_not_called()


# ---------------------------------------------------------------------------
# Write: delete
# ---------------------------------------------------------------------------


class TestDeletePolicy:
    def test_calls_delete_item(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        result = service.delete_policy("prod", "p-1")

        mock.delete_item.assert_called_once_with("rls-policy", "p-1")
        assert result == {"project": "prod", "policy_id": "p-1", "deleted": True}

    def test_dry_run_shows_the_policy_and_deletes_nothing(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_item.return_value = _policy_item()

        result = service.delete_policy("prod", "p-1", dry_run=True)

        mock.delete_item.assert_not_called()
        assert result["dry_run"] is True
        assert result["policy"]["id"] == "p-1"


# ---------------------------------------------------------------------------
# Pure condition helpers
# ---------------------------------------------------------------------------


class TestCompileConditionPreview:
    def test_true_sentinel(self) -> None:
        assert compile_condition_preview({"true": True}, "snowflake") == "TRUE"

    @pytest.mark.parametrize("bad", _BAD_TRUE_SENTINELS)
    def test_non_true_sentinel_is_never_rendered_as_always_true(self, bad: dict) -> None:
        with pytest.raises(ValueError, match="exactly"):
            compile_condition_preview(bad, "snowflake")

    @pytest.mark.parametrize(
        ("condition", "dialect", "sql"),
        [
            *(
                (
                    {"column": "region", "op": op, "value": "EU"},
                    "snowflake",
                    f"\"region\" {sql} 'EU'",
                )
                for op, sql in [
                    ("eq", "="),
                    ("ne", "!="),
                    ("gt", ">"),
                    ("gte", ">="),
                    ("lt", "<"),
                    ("lte", "<="),
                ]
            ),
            # Quoted per dialect, like the enforcement (so a column-case mismatch is visible).
            ({"column": "Region", "op": "eq", "value": "EU"}, "bigquery", "`Region` = 'EU'"),
            (
                {"column": "region", "op": "in", "values": ["EU", "US"]},
                "snowflake",
                "\"region\" IN ('EU', 'US')",
            ),
            (
                {"column": "region", "op": "not_in", "values": ["EU"]},
                "snowflake",
                "\"region\" NOT IN ('EU')",
            ),
            ({"column": "d", "op": "is_null"}, "snowflake", '"d" IS NULL'),
            ({"column": "d", "op": "is_not_null"}, "snowflake", '"d" IS NOT NULL'),
            ({"column": "amount", "op": "gt", "value": 100}, "snowflake", '"amount" > 100'),
            ({"column": "active", "op": "eq", "value": True}, "snowflake", '"active" = TRUE'),
            ({"false": True}, "snowflake", "FALSE"),
            (
                {"column": "o", "op": "eq", "value": {"$identity": "email"}},
                "snowflake",
                '"o" = <identity.email>',
            ),
            (
                {"column": "t", "op": "in", "values": {"$identity": "groups"}},
                "bigquery",
                "`t` IN (<identity.groups>)",
            ),
            ({"column": "o", "op": "eq", "value": "O'Brien"}, "snowflake", "\"o\" = 'O''Brien'"),
            (
                {
                    "and": [
                        {"column": "r", "op": "eq", "value": "EU"},
                        {"column": "s", "op": "ne", "value": "x"},
                    ]
                },
                "snowflake",
                "(\"r\" = 'EU') AND (\"s\" != 'x')",
            ),
            (
                {
                    "or": [
                        {"column": "r", "op": "eq", "value": "EU"},
                        {"column": "r", "op": "eq", "value": "US"},
                    ]
                },
                "snowflake",
                "(\"r\" = 'EU') OR (\"r\" = 'US')",
            ),
        ],
    )
    def test_renders_like_the_enforcement(self, condition: dict, dialect: str, sql: str) -> None:
        assert compile_condition_preview(condition, dialect) == sql

    def test_unrecognized_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            compile_condition_preview({"nonsense": True}, "snowflake")


class TestValidateConditionOps:
    def test_known_op_passes(self) -> None:
        assert validate_condition_ops({"column": "x", "op": "eq", "value": 1}) == []

    def test_unknown_op_reported(self) -> None:
        errors = validate_condition_ops({"column": "x", "op": "bogus", "value": 1})
        assert errors and "bogus" in errors[0]

    @pytest.mark.parametrize("op", [[], {}, 1, None])
    def test_a_non_string_op_is_reported_not_a_crash(self, op: object) -> None:
        errors = validate_condition_ops({"column": "id", "op": op, "value": 1})

        assert errors and "unknown condition op" in errors[0]

    def test_true_sentinel_always_valid(self) -> None:
        assert validate_condition_ops({"true": True}) == []

    @pytest.mark.parametrize("bad", _BAD_TRUE_SENTINELS)
    def test_true_sentinel_must_be_exactly_true(self, bad: dict) -> None:
        """`{"true": false}` must not pass: it would silently author an always-true policy."""
        errors = validate_condition_ops(bad)
        assert errors and "exactly" in errors[0]

    def test_a_false_sentinel_is_rejected_before_any_write(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        rules = [{"principal": "a@x.com", "condition": {"true": False}}]

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t", dialect="snowflake", rules=rules)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()

    def test_and_requires_at_least_two(self) -> None:
        errors = validate_condition_ops({"and": [{"true": True}]})
        assert errors

    def test_nested_and_recurses(self) -> None:
        condition = {
            "and": [
                {"column": "x", "op": "bogus", "value": 1},
                {"true": True},
            ]
        }
        errors = validate_condition_ops(condition)
        assert errors and "bogus" in errors[0]


class TestValidateRulesLocal:
    def test_empty_rules_rejected(self) -> None:
        assert validate_rules_local([]) != []

    def test_non_list_rejected(self) -> None:
        assert validate_rules_local("not-a-list") != []

    def test_valid_single_principal(self) -> None:
        assert validate_rules_local([{"principal": "a@x.com", "condition": {"true": True}}]) == []

    def test_valid_principals_list(self) -> None:
        rules = [{"principals": ["a@x.com", "b@x.com"], "condition": {"true": True}}]
        assert validate_rules_local(rules) == []

    def test_neither_principal_nor_principals_rejected(self) -> None:
        errors = validate_rules_local([{"condition": {"true": True}}])
        assert errors

    def test_both_principal_and_principals_rejected(self) -> None:
        rules = [{"principal": "a@x.com", "principals": ["b@x.com"], "condition": {"true": True}}]
        assert validate_rules_local(rules)

    def test_missing_condition_rejected(self) -> None:
        errors = validate_rules_local([{"principal": "a@x.com"}])
        assert errors


class TestValidatePolicyStructural:
    def test_valid_policy_passes(self) -> None:
        schema = {
            "type": "object",
            "required": ["table", "dialect", "rules"],
            "properties": {
                "table": {"type": "string"},
                "dialect": {"enum": ["snowflake", "bigquery"]},
                "rules": {"type": "array", "minItems": 1},
            },
        }
        policy = {"table": "in.c-x.y", "dialect": "snowflake", "rules": [{"condition": {}}]}
        assert draft7_errors(policy, schema) == []

    def test_missing_required_field_reported(self) -> None:
        schema = {"type": "object", "required": ["table", "dialect", "rules"]}
        errors = draft7_errors({"table": "t"}, schema)
        assert errors


class TestValidationWarnings:
    def test_a_skipped_live_validation_is_reported_not_hidden(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        mock.post_item.return_value = _policy_item()

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES)

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]

    def test_dry_run_reports_it_too(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES, dry_run=True)

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]

    def test_no_warning_when_the_schema_was_available(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = {"type": "object"}
        mock.post_item.return_value = _policy_item()

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES)

        assert "warnings" not in result

    def test_update_reports_it_as_well(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        mock.get_item.return_value = _policy_item()

        result = service.update_policy("prod", "p-1", rules=_RULES)

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]


class TestPrimitiveShape:
    """The shape of a leaf condition is checked locally, so it fails as INVALID_RLS_POLICY even when the
    live schema is unavailable (the documented state on stacks without the object type)."""

    @pytest.mark.parametrize(
        ("condition", "fragment"),
        [
            ({"column": "", "op": "eq", "value": 1}, "non-empty string 'column'"),
            ({"op": "eq", "value": 1}, "non-empty string 'column'"),
            ({"column": 5, "op": "is_null"}, "non-empty string 'column'"),
            ({"column": "a", "op": "eq"}, "needs a 'value'"),
            ({"column": "a", "op": "gt"}, "needs a 'value'"),
            ({"column": "a", "op": "in"}, "non-empty list 'values'"),
            ({"column": "a", "op": "not_in", "values": []}, "non-empty list 'values'"),
            ({"column": "a", "op": "in", "values": "abc"}, "non-empty list 'values'"),
            # `col = NULL` / `IN (NULL)` match no row: the principal would silently see nothing.
            ({"column": "a", "op": "eq", "value": None}, "use op 'is_null'"),
            ({"column": "a", "op": "in", "values": [1, None]}, "cannot list null"),
            # Only scalar literals: an object/array must not slip through when the live schema is unavailable.
            ({"column": "a", "op": "eq", "value": {"x": 1}}, "string, number or boolean 'value'"),
            ({"column": "a", "op": "gt", "value": [1]}, "string, number or boolean 'value'"),
            ({"column": "a", "op": "in", "values": [1, [2]]}, "string, number or boolean 'values'"),
        ],
    )
    def test_malformed_primitives_are_rejected(self, condition: dict, fragment: str) -> None:
        errors = validate_condition_ops(condition)
        assert errors and fragment in errors[0]

    @pytest.mark.parametrize(
        "condition",
        [
            {"column": "a", "op": "eq", "value": 0},
            {"column": "a", "op": "in", "values": [1]},
            {"column": "a", "op": "is_null"},
            {
                "and": [
                    {"column": "a", "op": "eq", "value": 1},
                    {"column": "b", "op": "is_not_null"},
                ]
            },
        ],
    )
    def test_well_formed_primitives_pass(self, condition: dict) -> None:
        assert validate_condition_ops(condition) == []

    def test_a_malformed_rule_never_reaches_the_preview_or_the_write(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        rules = [{"principal": "a@x.com", "condition": {"column": "", "op": "eq"}}]

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t", dialect="snowflake", rules=rules, dry_run=True)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()


class TestCompositionAndKeysAreExact:
    """One composition key, and no keys beyond an op's own shape: nothing may be validated and then dropped."""

    _LEAF: ClassVar[dict[str, Any]] = {"column": "a", "op": "eq", "value": 1}

    @pytest.mark.parametrize(
        "bad",
        [
            {
                "and": [_LEAF, _LEAF],
                "or": [_LEAF, _LEAF],
            },  # the second branch used to be silently dropped
            {"or": [_LEAF, _LEAF], "and": [_LEAF, _LEAF]},
            {"and": [_LEAF, _LEAF], "column": "a"},
            {"or": [_LEAF, _LEAF], "op": "eq"},
        ],
    )
    def test_a_condition_with_several_composition_keys_is_rejected(self, bad: dict) -> None:
        errors = validate_condition_ops(bad)
        assert errors and "exactly one of those keys" in errors[0]
        with pytest.raises(ValueError, match="exactly one of those keys"):
            compile_condition_preview(bad, "snowflake")

    @pytest.mark.parametrize(
        ("condition", "extra"),
        [
            ({"column": "a", "op": "eq", "value": 1, "values": [1]}, ["values"]),
            ({"column": "a", "op": "in", "values": [1], "value": 1}, ["value"]),
            ({"column": "a", "op": "is_null", "value": 1}, ["value"]),
            ({"column": "a", "op": "eq", "value": 1, "note": "x"}, ["note"]),
        ],
    )
    def test_keys_beyond_the_ops_shape_are_rejected(self, condition: dict, extra: list) -> None:
        errors = validate_condition_ops(condition)
        assert errors and f"unexpected keys {extra}" in errors[0]

    def test_a_valid_nested_tree_still_passes_and_renders(self) -> None:
        tree = {
            "or": [
                {"and": [self._LEAF, {"column": "b", "op": "in", "values": [1, 2]}]},
                {"column": "c", "op": "is_null"},
            ]
        }
        assert validate_condition_ops(tree) == []
        assert "OR" in compile_condition_preview(tree, "snowflake")

    def test_a_multi_key_condition_never_reaches_a_write(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        rules = [
            {
                "principal": "a@x.com",
                "condition": {"and": [self._LEAF, self._LEAF], "or": [self._LEAF, self._LEAF]},
            }
        ]

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t", dialect="snowflake", rules=rules)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()


class TestPrincipalFieldsAreValidatedByValueNotTruthiness:
    _COND: ClassVar[dict[str, Any]] = {"true": True}

    @pytest.mark.parametrize(
        ("rule", "fragment"),
        [
            ({"principal": "a", "principals": [], "condition": _COND}, "exactly one of"),
            ({"principal": "", "principals": ["b"], "condition": _COND}, "exactly one of"),
            ({"principal": 1, "condition": _COND}, "principal must be a non-empty string"),
            ({"principal": "", "condition": _COND}, "principal must be a non-empty string"),
            ({"principal": None, "condition": _COND}, "principal must be a non-empty string"),
            ({"principal": ["a"], "condition": _COND}, "principal must be a non-empty string"),
            ({"principals": [], "condition": _COND}, "principals must be a non-empty list"),
            ({"principals": "a@x.com", "condition": _COND}, "principals must be a non-empty list"),
            ({"principals": ["a", ""], "condition": _COND}, "principals must be a non-empty list"),
            ({"principals": ["a", 2], "condition": _COND}, "principals must be a non-empty list"),
            ({"condition": _COND}, "exactly one of"),
        ],
    )
    def test_malformed_principal_fields_are_rejected_locally(
        self, rule: dict, fragment: str
    ) -> None:
        errors = validate_rules_local([rule])
        assert errors and fragment in errors[0]

    @pytest.mark.parametrize(
        "rule",
        [
            {"principal": "a@x.com", "condition": _COND},
            {"principals": ["a@x.com", "b@x.com"], "condition": _COND},
        ],
    )
    def test_well_formed_principal_fields_pass(self, rule: dict) -> None:
        assert validate_rules_local([rule]) == []

    def test_a_malformed_principal_never_reaches_a_write_when_the_schema_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy(
                "prod",
                table="t",
                dialect="snowflake",
                rules=[{"principal": 1, "condition": self._COND}],
            )

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()


class TestUnresolvedVersionListing:
    @pytest.mark.parametrize("listing", [{"versions": []}, {"versions": [{"isDefault": True}]}])
    def test_a_version_listing_that_cannot_be_resolved_is_unavailable_not_a_schema(
        self, tmp_path: Path, listing: dict
    ) -> None:
        """It is a valid, constraint-free JSON Schema, so validating against it would silently skip every
        structural check without the documented warning."""
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = listing
        mock.post_item.return_value = _policy_item()

        fetch = service.fetch_schema("prod")
        assert fetch.schema is None
        assert "version listing" in (fetch.reason or "")

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES)
        assert any("version listing" in w for w in result["warnings"])
