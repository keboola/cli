"""Service-layer tests for ``RlsService`` and the ``_rls_condition`` helpers.

Each test injects a ``unittest.mock.MagicMock`` as the metastore client
factory so we verify orchestration (scope resolution, validation order, call
sequencing) without touching HTTP -- mirrors ``test_semantic_layer_service.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._rls_condition import (
    compile_condition_preview,
    validate_condition_ops,
    validate_policy_structural,
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
    service = RlsService(config_store=store, metastore_client_factory=lambda url, token: mock)
    return service, mock


def _policy_item(
    item_id: str = "p-1",
    table: str = "in.c-crm.invoices",
    dialect: str = "snowflake",
    rules: list[dict[str, Any]] | None = None,
    scope: str = "organization",
    source_project_id: str | None = "5725",
    target_project_ids: list[str] | None = None,
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

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES
        )
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
    def test_default_scope_is_organization_never_project(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item()

        service.create_policy("prod", table="in.c-crm.invoices", dialect="snowflake", rules=_RULES)

        _, kwargs = mock.post_item.call_args
        assert kwargs["scope"] == "organization"
        assert kwargs["scope"] != "project"
        mock.put_target_projects.assert_not_called()

    def test_target_projects_use_targeted_scope_and_manage_grants(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.post_item.return_value = _policy_item(scope="targeted", target_project_ids=["999"])

        service.create_policy(
            "prod",
            table="in.c-crm.invoices",
            dialect="snowflake",
            rules=_RULES,
            target_project_ids=["999"],
        )

        _, kwargs = mock.post_item.call_args
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_project_ids"] == ["999"]
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", ["999"])

    def test_invalid_dialect_rejected_before_any_write(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t", dialect="postgres", rules=_RULES)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()
        mock.get_schema.assert_not_called()

    def test_unknown_condition_op_rejected_before_any_write(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        bad_rules = [
            {"principal": "a@x.com", "condition": {"column": "x", "op": "bogus", "value": 1}}
        ]

        with pytest.raises(KeboolaApiError):
            service.create_policy("prod", table="t", dialect="snowflake", rules=bad_rules)

        mock.post_item.assert_not_called()

    def test_rule_with_both_principal_and_principals_rejected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        bad_rules = [
            {"principal": "a@x.com", "principals": ["b@x.com"], "condition": {"true": True}}
        ]

        with pytest.raises(KeboolaApiError):
            service.create_policy("prod", table="t", dialect="snowflake", rules=bad_rules)

        mock.post_item.assert_not_called()

    def test_structural_validation_runs_when_schema_available(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        # A schema that requires a `description` field the candidate body lacks.
        mock.get_schema.return_value = {
            "type": "object",
            "required": ["table", "dialect", "rules", "description"],
        }

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy("prod", table="t", dialect="snowflake", rules=_RULES)

        assert excinfo.value.error_code == ErrorCode.INVALID_RLS_POLICY
        mock.post_item.assert_not_called()

    def test_dry_run_never_calls_post_item(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.create_policy(
            "prod", table="t", dialect="snowflake", rules=_RULES, dry_run=True
        )

        mock.post_item.assert_not_called()
        assert result["dry_run"] is True
        assert result["preview"][0]["condition"] == "region = 'EU'"


# ---------------------------------------------------------------------------
# Write: update
# ---------------------------------------------------------------------------


class TestUpdatePolicy:
    def test_partial_override_preserves_untouched_fields(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _policy_item(table="in.c-crm.invoices", dialect="snowflake")
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.put_item.return_value = _policy_item()

        service.update_policy("prod", "p-1", rules=_RULES)

        _, kwargs = mock.put_item.call_args
        assert kwargs["data"]["table"] == "in.c-crm.invoices"  # unchanged, not wiped
        assert kwargs["data"]["dialect"] == "snowflake"  # unchanged, not wiped
        assert kwargs["data"]["rules"] == _RULES  # the one field we changed

    def test_targeted_scope_derived_from_merged_targets(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _policy_item(scope="organization", target_project_ids=[])
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.put_item.return_value = _policy_item(scope="targeted", target_project_ids=["42"])

        service.update_policy("prod", "p-1", target_project_ids=["42"])

        _, kwargs = mock.put_item.call_args
        assert kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", ["42"])

    def test_invalid_override_rejected_before_write(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _policy_item()

        with pytest.raises(KeboolaApiError):
            service.update_policy("prod", "p-1", dialect="postgres")

        mock.put_item.assert_not_called()

    def test_dry_run_never_calls_put_item(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)
        mock.get_item.return_value = _policy_item()
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.update_policy("prod", "p-1", rules=_RULES, dry_run=True)

        mock.put_item.assert_not_called()
        assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# Write: delete
# ---------------------------------------------------------------------------


class TestDeletePolicy:
    def test_calls_delete_item(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service, mock = _make_service(store)

        result = service.delete_policy("prod", "p-1")

        mock.delete_item.assert_called_once_with("rls-policy", "p-1")
        assert result == {"project": "prod", "policy_id": "p-1", "deleted": True}


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
        "op,sql",
        [
            ("eq", "region = 'EU'"),
            ("ne", "region != 'EU'"),
            ("gt", "region > 'EU'"),
            ("gte", "region >= 'EU'"),
            ("lt", "region < 'EU'"),
            ("lte", "region <= 'EU'"),
        ],
    )
    def test_comparison_ops(self, op: str, sql: str) -> None:
        condition = {"column": "region", "op": op, "value": "EU"}
        assert compile_condition_preview(condition, "snowflake") == sql

    def test_in_op(self) -> None:
        condition = {"column": "region", "op": "in", "values": ["EU", "US"]}
        assert compile_condition_preview(condition, "snowflake") == "region IN ('EU', 'US')"

    def test_not_in_op(self) -> None:
        condition = {"column": "region", "op": "not_in", "values": ["EU"]}
        assert compile_condition_preview(condition, "snowflake") == "region NOT IN ('EU')"

    def test_is_null(self) -> None:
        condition = {"column": "deleted_at", "op": "is_null"}
        assert compile_condition_preview(condition, "snowflake") == "deleted_at IS NULL"

    def test_is_not_null(self) -> None:
        condition = {"column": "deleted_at", "op": "is_not_null"}
        assert compile_condition_preview(condition, "snowflake") == "deleted_at IS NOT NULL"

    def test_and_nesting(self) -> None:
        condition = {
            "and": [
                {"column": "region", "op": "eq", "value": "EU"},
                {"column": "status", "op": "ne", "value": "draft"},
            ]
        }
        assert compile_condition_preview(condition, "snowflake") == (
            "(region = 'EU') AND (status != 'draft')"
        )

    def test_or_nesting(self) -> None:
        condition = {
            "or": [
                {"column": "region", "op": "eq", "value": "EU"},
                {"column": "region", "op": "eq", "value": "US"},
            ]
        }
        assert compile_condition_preview(condition, "snowflake") == (
            "(region = 'EU') OR (region = 'US')"
        )

    def test_numeric_and_null_literals_not_quoted(self) -> None:
        condition = {"column": "amount", "op": "gt", "value": 100}
        assert compile_condition_preview(condition, "snowflake") == "amount > 100"

    def test_unrecognized_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            compile_condition_preview({"nonsense": True}, "snowflake")


class TestValidateConditionOps:
    def test_known_op_passes(self) -> None:
        assert validate_condition_ops({"column": "x", "op": "eq", "value": 1}) == []

    def test_unknown_op_reported(self) -> None:
        errors = validate_condition_ops({"column": "x", "op": "bogus", "value": 1})
        assert errors and "bogus" in errors[0]

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
        assert validate_policy_structural(policy, schema) == []

    def test_missing_required_field_reported(self) -> None:
        schema = {"type": "object", "required": ["table", "dialect", "rules"]}
        errors = validate_policy_structural({"table": "t"}, schema)
        assert errors


class TestValidationWarnings:
    def test_a_skipped_live_validation_is_reported_not_hidden(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        mock.post_item.return_value = _policy_item()

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES
        )

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]

    def test_dry_run_reports_it_too(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES, dry_run=True
        )

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]

    def test_no_warning_when_the_schema_was_available(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.return_value = {"type": "object"}
        mock.post_item.return_value = _policy_item()

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES
        )

        assert "warnings" not in result

    def test_update_reports_it_as_well(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="no schema", status_code=404)
        mock.get_item.return_value = _policy_item()
        mock.put_item.return_value = _policy_item()

        result = service.update_policy("prod", "p-1", dialect="bigquery")

        assert result["warnings"] == ["Live schema validation was skipped: no schema"]


class TestClearingGrants:
    """`target_project_ids`: None keeps the grants, a list replaces them, [] revokes them all."""

    def _targeted(self) -> dict[str, Any]:
        return _policy_item(scope="targeted", target_project_ids=["7", "8"])

    def test_empty_list_revokes_every_grant_and_keeps_the_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = self._targeted()
        mock.put_item.return_value = self._targeted()

        result = service.update_policy("prod", "p-1", target_project_ids=[])

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        assert mock.put_item.call_args.kwargs["target_project_ids"] == []
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", [])
        assert result["id"] == "p-1"

    def test_none_keeps_the_existing_grants(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = self._targeted()
        mock.put_item.return_value = self._targeted()

        service.update_policy("prod", "p-1", dialect="bigquery")

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", ["7", "8"])

    def test_clearing_a_policy_that_has_no_grants_makes_no_grant_call(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = _policy_item()  # organization scope, no targets
        mock.put_item.return_value = _policy_item()

        service.update_policy("prod", "p-1", target_project_ids=[])

        assert mock.put_item.call_args.kwargs["scope"] == "organization"
        mock.put_target_projects.assert_not_called()

    def test_dry_run_previews_the_cleared_state_without_writing(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = self._targeted()

        result = service.update_policy("prod", "p-1", target_project_ids=[], dry_run=True)

        assert result["target_project_ids"] == []
        mock.put_item.assert_not_called()
        mock.put_target_projects.assert_not_called()


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
        ],
    )
    def test_malformed_primitives_are_rejected(self, condition: dict, fragment: str) -> None:
        errors = validate_condition_ops(condition)
        assert errors and fragment in errors[0]

    @pytest.mark.parametrize(
        "condition",
        [
            {"column": "a", "op": "eq", "value": 0},
            {"column": "a", "op": "eq", "value": None},  # an explicit null value is still a value
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


class TestScopeIsKeptWhenTargetsAreOmitted:
    def _targeted_without_grants(self) -> dict[str, Any]:
        """What the metastore returns after every grant was revoked: still `targeted`, empty list."""
        return _policy_item(scope="targeted", target_project_ids=[])

    def test_an_unrelated_update_does_not_turn_a_cleared_targeted_policy_into_organization(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = self._targeted_without_grants()
        mock.put_item.return_value = self._targeted_without_grants()

        service.update_policy("prod", "p-1", dialect="bigquery")  # no target option at all

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_not_called()

    def test_an_explicit_target_list_makes_the_policy_targeted(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = _policy_item()  # organization scope
        mock.put_item.return_value = _policy_item(scope="targeted", target_project_ids=["7"])

        service.update_policy("prod", "p-1", target_project_ids=["7"])

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", ["7"])

    def test_clearing_keeps_the_current_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)
        mock.get_item.return_value = _policy_item(scope="targeted", target_project_ids=["7"])
        mock.put_item.return_value = self._targeted_without_grants()

        service.update_policy("prod", "p-1", target_project_ids=[])

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("rls-policy", "p-1", [])


class TestDryRunValidatesTargetIds:
    """A preview must run the same validation as the write -- including the target project ids."""

    @pytest.mark.parametrize("bad", [["abc"], ["0"], ["-3"], ["1", "x"]])
    def test_create_dry_run_rejects_a_bad_target_before_any_network_call(
        self, tmp_path: Path, bad: list
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_policy(
                "prod",
                table="t",
                dialect="snowflake",
                rules=_RULES,
                target_project_ids=bad,
                dry_run=True,
            )

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.get_schema.assert_not_called()
        mock.post_item.assert_not_called()

    def test_update_dry_run_rejects_a_bad_target_before_any_network_call(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(_make_store(tmp_path))

        with pytest.raises(KeboolaApiError) as excinfo:
            service.update_policy("prod", "p-1", target_project_ids=["abc"], dry_run=True)

        assert excinfo.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock.get_item.assert_not_called()
        mock.put_item.assert_not_called()

    def test_valid_targets_still_preview(self, tmp_path: Path) -> None:
        service, mock = _make_service(_make_store(tmp_path))
        mock.get_schema.side_effect = KeboolaApiError(message="n/a", status_code=404)

        result = service.create_policy(
            "prod",
            table="t",
            dialect="snowflake",
            rules=_RULES,
            target_project_ids=["7", "8"],
            dry_run=True,
        )

        assert result["scope"] == "targeted"
        assert result["target_project_ids"] == ["7", "8"]


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

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES
        )
        assert any("version listing" in w for w in result["warnings"])
