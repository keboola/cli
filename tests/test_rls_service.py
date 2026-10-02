"""Service-layer tests for ``RlsService`` and the ``_rls_condition`` helpers.

Each test injects a ``unittest.mock.MagicMock`` as the metastore client
factory so we verify orchestration (scope resolution, validation order, call
sequencing) without touching HTTP -- mirrors ``test_semantic_layer_service.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
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
