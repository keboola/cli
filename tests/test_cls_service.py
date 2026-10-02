"""Service-layer tests for ``ClsService`` (column-level security).

Same approach as ``test_rls_service.py``: a ``MagicMock`` metastore client
factory, so we verify orchestration (item type, scope resolution, validation
order, fetch-then-merge) without HTTP. The shared plumbing is inherited from
``RlsService`` and covered there; these tests pin what ``ClsService`` changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.cls_service import ClsService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
_RULES = [{"principal": "a@x.com", "visible_columns": ["id", "region"]}]


def _make_service(tmp_path: Path) -> tuple[ClsService, MagicMock]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=5725,
        ),
    )
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.get_schema.side_effect = KeboolaApiError(
        message="no schema", status_code=404, error_code=ErrorCode.NOT_FOUND
    )
    return ClsService(config_store=store, metastore_client_factory=lambda url, token: mock), mock


def _item(rules: list[dict[str, Any]] | None = None, **meta: Any) -> dict[str, Any]:
    return {
        "type": "cls-policy",
        "id": "p-1",
        "attributes": {
            "table": "in.c-crm.invoices",
            "dialect": "snowflake",
            "rules": _RULES if rules is None else rules,
        },
        "meta": {"scope": "organization", "sourceProjectId": "5725", "revision": 2, **meta},
    }


class TestLocalRuleValidation:
    @pytest.mark.parametrize(
        ("rules", "fragment"),
        [
            (None, "non-empty array"),
            ([], "non-empty array"),
            (["x"], "must be an object"),
            ([{"visible_columns": ["id"]}], "exactly one of 'principal'/'principals'"),
            (
                [{"principal": "a@x.com", "principals": ["b@x.com"], "visible_columns": ["id"]}],
                "exactly one of 'principal'/'principals'",
            ),
            ([{"principal": "a@x.com"}], "non-empty 'visible_columns'"),
            ([{"principal": "a@x.com", "visible_columns": []}], "non-empty 'visible_columns'"),
            ([{"principal": "a@x.com", "visible_columns": "id"}], "non-empty 'visible_columns'"),
            (
                [{"principal": "a@x.com", "visible_columns": ["id; DROP"]}],
                "not a valid column name",
            ),
            ([{"principal": "a@x.com", "visible_columns": [1]}], "not a valid column name"),
        ],
    )
    def test_rejects(self, tmp_path: Path, rules: Any, fragment: str) -> None:
        service, mock = _make_service(tmp_path)

        with pytest.raises(KeboolaApiError) as exc:
            service.create_policy("prod", table="in.c-crm.t", dialect="snowflake", rules=rules)

        assert exc.value.error_code == ErrorCode.INVALID_CLS_POLICY
        assert fragment in exc.value.message
        assert exc.value.message.startswith("CLS policy is invalid")
        mock.post_item.assert_not_called()

    def test_accepts_principals_list(self, tmp_path: Path) -> None:
        service, _ = _make_service(tmp_path)
        rules = [{"principals": ["a@x.com", "b@x.com"], "visible_columns": ["id"]}]

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=rules, dry_run=True
        )

        assert result["preview"] == [
            {"principal": ["a@x.com", "b@x.com"], "visible_columns": ["id"]}
        ]

    def test_bad_dialect_is_rejected_before_any_write(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)

        with pytest.raises(KeboolaApiError, match="dialect must be one of"):
            service.create_policy("prod", table="in.c-crm.t", dialect="oracle", rules=_RULES)

        mock.post_item.assert_not_called()


class TestStructuralValidation:
    def test_live_schema_violation_is_reported_as_cls_error(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_schema.side_effect = None
        mock.get_schema.return_value = {
            "type": "object",
            "properties": {"table": {"type": "string", "pattern": "^[a-z.]+$"}},
        }

        with pytest.raises(KeboolaApiError) as exc:
            service.create_policy("prod", table="BAD TABLE", dialect="snowflake", rules=_RULES)

        mock.get_schema.assert_called_with("cls-policy")
        assert exc.value.error_code == ErrorCode.INVALID_CLS_POLICY
        assert "Schema error at table" in exc.value.message

    def test_missing_schema_degrades_instead_of_blocking(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.post_item.return_value = _item()

        service.create_policy("prod", table="in.c-crm.invoices", dialect="snowflake", rules=_RULES)

        mock.post_item.assert_called_once()

    def test_fetch_schema_uses_cls_item_type(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_schema.side_effect = None
        mock.get_schema.return_value = {"type": "object"}

        fetch = service.fetch_schema("prod")

        mock.get_schema.assert_called_once_with("cls-policy")
        assert fetch.schema == {"type": "object"}


class TestReads:
    def test_list_uses_cls_item_type(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.list_items.return_value = [_item()]

        result = service.list_policies("prod")

        mock.list_items.assert_called_once_with("cls-policy")
        assert result["policies"][0]["rule_count"] == 1

    def test_detail_returns_visible_columns(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()

        result = service.get_policy("prod", "p-1")

        mock.get_item.assert_called_once_with("cls-policy", "p-1")
        assert result["rules"] == _RULES
        assert result["revision"] == 2


class TestCreate:
    def test_writes_cls_policy_at_organization_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.post_item.return_value = _item()

        result = service.create_policy(
            "prod", table="in.c-crm.invoices", dialect="snowflake", rules=_RULES
        )

        mock.post_item.assert_called_once_with(
            "cls-policy",
            name="in.c-crm.invoices",
            data={"table": "in.c-crm.invoices", "dialect": "snowflake", "rules": _RULES},
            scope="organization",
            target_project_ids=None,
        )
        mock.put_target_projects.assert_not_called()
        assert result["preview"] == [{"principal": "a@x.com", "visible_columns": ["id", "region"]}]

    def test_target_projects_make_it_targeted_and_grant(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.post_item.return_value = _item(scope="targeted")

        service.create_policy(
            "prod",
            table="in.c-crm.invoices",
            dialect="bigquery",
            rules=_RULES,
            target_project_ids=["7", "8"],
        )

        assert mock.post_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("cls-policy", "p-1", ["7", "8"])

    def test_dry_run_never_writes(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)

        result = service.create_policy(
            "prod", table="in.c-crm.t", dialect="snowflake", rules=_RULES, dry_run=True
        )

        assert result["dry_run"] is True
        assert result["scope"] == "organization"
        mock.post_item.assert_not_called()


class TestUpdate:
    def test_merges_only_the_given_fields(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()
        mock.put_item.return_value = _item()
        new_rules = [{"principal": "b@x.com", "visible_columns": ["id"]}]

        service.update_policy("prod", "p-1", rules=new_rules)

        mock.put_item.assert_called_once_with(
            "cls-policy",
            "p-1",
            name="in.c-crm.invoices",
            data={"table": "in.c-crm.invoices", "dialect": "snowflake", "rules": new_rules},
            scope="organization",
            target_project_ids=None,
        )

    def test_keeps_existing_targets_and_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item(scope="targeted", targetProjectIds=["7"])
        mock.put_item.return_value = _item(scope="targeted")

        service.update_policy("prod", "p-1", table="in.c-crm.other")

        assert mock.put_item.call_args.kwargs["scope"] == "targeted"
        mock.put_target_projects.assert_called_once_with("cls-policy", "p-1", ["7"])

    def test_dry_run_never_writes(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()

        result = service.update_policy("prod", "p-1", dialect="bigquery", dry_run=True)

        assert result["dry_run"] is True
        assert result["dialect"] == "bigquery"
        mock.put_item.assert_not_called()

    def test_invalid_merged_rules_are_rejected(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()

        with pytest.raises(KeboolaApiError) as exc:
            service.update_policy("prod", "p-1", rules=[{"principal": "a@x.com"}])

        assert exc.value.error_code == ErrorCode.INVALID_CLS_POLICY
        mock.put_item.assert_not_called()


def test_delete_uses_cls_item_type(tmp_path: Path) -> None:
    service, mock = _make_service(tmp_path)

    result = service.delete_policy("prod", "p-1")

    mock.delete_item.assert_called_once_with("cls-policy", "p-1")
    assert result == {"project": "prod", "policy_id": "p-1", "deleted": True}
