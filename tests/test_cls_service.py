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

from .test_rls_service import storage_client_factory

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
    mock.list_items.return_value = []
    service = ClsService(
        config_store=store,
        client_factory=storage_client_factory(),
        metastore_client_factory=lambda url, token: mock,
    )
    return service, mock


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

    def test_a_dialect_other_than_the_backend_is_rejected_before_any_write(
        self, tmp_path: Path
    ) -> None:
        service, mock = _make_service(tmp_path)

        with pytest.raises(KeboolaApiError, match="does not match the project backend"):
            service.create_policy("prod", table="in.c-crm.t", dialect="bigquery", rules=_RULES)

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

    def test_version_listing_is_resolved_to_the_real_schema_before_validating(
        self, tmp_path: Path
    ) -> None:
        """The live metastore's bare endpoint returns only `{"versions": [...]}`; validating
        against that listing would pass anything, so the default version must be fetched."""
        service, mock = _make_service(tmp_path)
        real_schema = {
            "type": "object",
            "properties": {"table": {"type": "string", "pattern": "^[a-z.]+$"}},
        }
        mock.get_schema.side_effect = [
            {"versions": [{"version": "0.9.0"}, {"version": "1.0.0", "isDefault": True}]},
            real_schema,
        ]

        with pytest.raises(KeboolaApiError) as exc:
            service.create_policy("prod", table="BAD TABLE", dialect="snowflake", rules=_RULES)

        assert [c.args + tuple(c.kwargs.items()) for c in mock.get_schema.call_args_list] == [
            ("cls-policy",),
            ("cls-policy", ("version", "1.0.0")),
        ]
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
    def test_writes_cls_policy_at_targeted_scope_by_default(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.post_item.return_value = _item(scope="targeted")

        result = service.create_policy("prod", table="in.c-crm.invoices", rules=_RULES)

        mock.post_item.assert_called_once_with(
            "cls-policy",
            name="in.c-crm.invoices",
            data={"table": "in.c-crm.invoices", "dialect": "snowflake", "rules": _RULES},
            scope="targeted",
            target_project_ids=None,
        )
        assert result["preview"] == [{"principal": "a@x.com", "visible_columns": ["id", "region"]}]

    def test_target_projects_are_granted_in_the_create_request(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.post_item.return_value = _item(scope="targeted")

        service.create_policy(
            "prod", table="in.c-crm.invoices", rules=_RULES, target_projects=["7,8"]
        )

        assert mock.post_item.call_args.kwargs["target_project_ids"] == [7, 8]
        mock.put_target_projects.assert_not_called()

    def test_duplicate_principals_are_rejected(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        rules = [*_RULES, {"principal": "A@x.com", "visible_columns": ["id"]}]

        with pytest.raises(KeboolaApiError, match="already has a rule"):
            service.create_policy("prod", table="in.c-crm.t", rules=rules)

        mock.post_item.assert_not_called()

    def test_dry_run_never_writes(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)

        result = service.create_policy("prod", table="in.c-crm.t", rules=_RULES, dry_run=True)

        assert result["dry_run"] is True
        assert result["scope"] == "targeted"
        mock.post_item.assert_not_called()


class TestUpdate:
    def test_patches_only_the_given_fields(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()
        new_rules = [{"principal": "b@x.com", "visible_columns": ["id"]}]

        service.update_policy("prod", "p-1", rules=new_rules)

        mock.patch_item.assert_called_once_with(
            "cls-policy", "p-1", name=None, data={"rules": new_rules}
        )

    def test_keeps_existing_targets_and_scope(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item(scope="targeted", targetProjectIds=["7"])

        service.update_policy("prod", "p-1", table="in.c-crm.other")

        mock.put_target_projects.assert_not_called()  # the omitted option re-sends no grants

    def test_dry_run_never_writes(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()

        result = service.update_policy("prod", "p-1", table="in.c-crm.other", dry_run=True)

        assert result["dry_run"] is True
        assert result["table"] == "in.c-crm.other"
        mock.patch_item.assert_not_called()

    def test_invalid_merged_rules_are_rejected(self, tmp_path: Path) -> None:
        service, mock = _make_service(tmp_path)
        mock.get_item.return_value = _item()

        with pytest.raises(KeboolaApiError) as exc:
            service.update_policy("prod", "p-1", rules=[{"principal": "a@x.com"}])

        assert exc.value.error_code == ErrorCode.INVALID_CLS_POLICY
        mock.patch_item.assert_not_called()


def test_delete_uses_cls_item_type(tmp_path: Path) -> None:
    service, mock = _make_service(tmp_path)

    result = service.delete_policy("prod", "p-1")

    mock.delete_item.assert_called_once_with("cls-policy", "p-1")
    assert result == {"project": "prod", "policy_id": "p-1", "deleted": True}
