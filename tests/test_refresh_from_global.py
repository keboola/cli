"""`project refresh` takes a working token from the global config (#955).

A local `.kbagent/` created by `init --from-global` keeps a one-time copy of
the global tokens. After a rotation in the global config, `project refresh`
in the local directory first takes the global token of the same project and
mints through the Manage API only what it cannot take.

The global config here is a real ConfigStore in the tmp directory that
conftest points ``platformdirs.user_config_dir`` at; the Storage and Manage
clients are mocks.
"""

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._project_refresh import (
    LOCAL_COPIES_HINT,
    global_store_for,
    is_global_config,
)
from keboola_agent_cli.config_store import ConfigStore, global_config_dir
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services import _refresh_from_global
from keboola_agent_cli.services._refresh_from_global import pick_global_candidate
from keboola_agent_cli.services.org_service import OrgService

runner = CliRunner()

STACK = "https://connection.keboola.com"
OTHER_STACK = "https://connection.eu-central-1.keboola.com"
DEAD_LOCAL = "100-11-deadLocalTokenValue000000000"
GLOBAL_TOKEN = "100-22-rotatedGlobalTokenValue00000"
OTHER_GLOBAL = "100-33-anotherGlobalTokenValue00000"
DEAD_LOCAL_B = "200-11-deadLocalTokenValueB0000000"
MINTED = "200-99-mintedTokenValue00000000000"
MANAGE = "manage-token-123456789012345678"


def _invalid() -> KeboolaApiError:
    return KeboolaApiError(
        message="Invalid token", status_code=401, error_code=ErrorCode.INVALID_TOKEN
    )


def _verified(project_id: int, name: str = "Renamed") -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="t", token_description="d", project_id=project_id, project_name=name, owner_name=""
    )


class Storage:
    """Storage client factory: each token maps to a verify result or an exception."""

    def __init__(self, behaviour: dict[str, Any]) -> None:
        self.behaviour = behaviour
        self.seen: list[str] = []

    def __call__(self, url: str, token: str) -> MagicMock:
        self.seen.append(token)
        client = MagicMock()
        client.__enter__.return_value = client  # like KeboolaClient.__enter__
        outcome = self.behaviour[token]
        if isinstance(outcome, Exception):
            client.verify_token.side_effect = outcome
        else:
            client.verify_token.return_value = outcome
        return client


def _manage() -> MagicMock:
    manage = MagicMock()
    manage.verify_token.return_value = {"user": {"email": "admin@test.com"}}
    manage.create_project_token.return_value = {"id": "x", "token": MINTED}
    return manage


def _store(path: Path, projects: dict[str, ProjectConfig]) -> ConfigStore:
    path.mkdir(parents=True, exist_ok=True)
    store = ConfigStore(config_dir=path)
    store.save(AppConfig())  # config.json exists even with no project
    for alias, project in projects.items():
        store.add_project(alias, project)
    return store


def _project(token: str, project_id: int = 100, stack: str = STACK) -> ProjectConfig:
    return ProjectConfig(
        stack_url=stack, token=token, project_id=project_id, project_name=f"P{project_id}"
    )


def _global(projects: dict[str, ProjectConfig]) -> ConfigStore:
    return _store(global_config_dir(), projects)


def _service(local: ConfigStore, storage: Storage, manage: MagicMock) -> OrgService:
    return OrgService(
        config_store=local,
        manage_client_factory=lambda url, token: manage,
        storage_client_factory=storage,
    )


def _token(store: ConfigStore, alias: str) -> str:
    project = store.load().projects[alias]
    return project.token


# ── Service ─────────────────────────────────────────────────────────────


class TestServiceAdoption:
    def test_adopts_the_global_token_without_a_manage_token(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})
        manage = _manage()
        provider = MagicMock(return_value=MANAGE)

        result = _service(local, storage, manage).refresh_tokens(
            global_store=global_store, manage_token_provider=provider
        )

        assert result["projects_adopted"] == [
            {
                "alias": "prod",
                "project_id": 100,
                "project_name": "Renamed",
                "global_alias": "g-prod",
                "action": "adopted_from_global",
            }
        ]
        assert result["projects_refreshed"] == []
        assert result["projects_checked"] == 1
        assert _token(local, "prod") == GLOBAL_TOKEN
        assert local.load().projects["prod"].project_name == "Renamed"
        provider.assert_not_called()
        manage.verify_token.assert_not_called()
        manage.create_project_token.assert_not_called()
        # The global config is never written.
        assert _token(global_store, "g-prod") == GLOBAL_TOKEN

    def test_adopted_entry_never_carries_a_token(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})

        for dry_run in (True, False):
            result = _service(local, storage, _manage()).refresh_tokens(
                global_store=global_store, dry_run=dry_run, manage_token=MANAGE
            )
            dumped = json.dumps(result)
            assert GLOBAL_TOKEN not in dumped
            assert GLOBAL_TOKEN[:8] not in dumped
            assert all("token" not in entry for entry in result["projects_adopted"])

    def test_transient_local_check_error_never_adopts(self, tmp_path: Path) -> None:
        """R1: a 503 says nothing about the token -- mint as before, never adopt."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        transient = KeboolaApiError(
            message="Service unavailable",
            status_code=503,
            error_code=ErrorCode.API_ERROR,
            retryable=True,
        )
        storage = Storage(
            {DEAD_LOCAL: transient, GLOBAL_TOKEN: _verified(100), MINTED: _verified(100)}
        )
        provider = MagicMock(return_value=MANAGE)

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, manage_token_provider=provider
        )

        assert result["projects_adopted"] == []
        assert [p["alias"] for p in result["projects_refreshed"]] == ["prod"]
        assert GLOBAL_TOKEN not in storage.seen
        assert _token(local, "prod") == MINTED
        provider.assert_called_once()

    def test_timeout_on_local_check_never_adopts(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        timeout = KeboolaApiError(
            message="t", status_code=0, error_code=ErrorCode.TIMEOUT, retryable=True
        )
        storage = Storage({DEAD_LOCAL: timeout, GLOBAL_TOKEN: _verified(100)})

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, dry_run=True
        )

        assert result["projects_adopted"] == []
        assert [p["action"] for p in result["projects_refreshed"]] == ["would_refresh"]
        assert GLOBAL_TOKEN not in storage.seen

    def test_valid_local_token_is_never_touched(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _verified(100), GLOBAL_TOKEN: _verified(100)})
        provider = MagicMock(return_value=MANAGE)

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, manage_token_provider=provider
        )

        assert [p["alias"] for p in result["projects_valid"]] == ["prod"]
        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in storage.seen
        assert _token(local, "prod") == DEAD_LOCAL
        provider.assert_not_called()

    def test_force_never_adopts(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100), MINTED: _verified(100)}
        )
        provider = MagicMock(return_value=MANAGE)

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, force=True, manage_token_provider=provider
        )

        assert result["projects_adopted"] == []
        assert [p["alias"] for p in result["projects_refreshed"]] == ["prod"]
        assert GLOBAL_TOKEN not in storage.seen
        assert _token(local, "prod") == MINTED

    def test_dry_run_reports_the_adoption_and_never_asks_for_the_manage_token(
        self, tmp_path: Path
    ) -> None:
        """R2c."""
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), DEAD_LOCAL_B: _invalid(), GLOBAL_TOKEN: _verified(100)}
        )
        manage = _manage()
        provider = MagicMock(side_effect=AssertionError("dry run asked for the Manage token"))

        result = _service(local, storage, manage).refresh_tokens(
            global_store=global_store, dry_run=True, manage_token_provider=provider
        )

        assert [p["action"] for p in result["projects_adopted"]] == ["would_adopt_from_global"]
        assert [p["alias"] for p in result["projects_refreshed"]] == ["b"]
        provider.assert_not_called()
        manage.verify_token.assert_not_called()
        assert _token(local, "prod") == DEAD_LOCAL

    def test_provider_failure_writes_nothing(self, tmp_path: Path) -> None:
        """R2a at the service: the provider's exception propagates, no adoption written."""
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), DEAD_LOCAL_B: _invalid(), GLOBAL_TOKEN: _verified(100)}
        )

        class NoToken(Exception):
            pass

        with pytest.raises(NoToken):
            _service(local, storage, _manage()).refresh_tokens(
                global_store=global_store, manage_token_provider=MagicMock(side_effect=NoToken)
            )

        assert _token(local, "prod") == DEAD_LOCAL
        assert _token(local, "b") == DEAD_LOCAL_B

    def test_minting_without_token_or_provider_is_a_programming_error(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"b": _project(DEAD_LOCAL_B, 200)})
        storage = Storage({DEAD_LOCAL_B: _invalid()})
        with pytest.raises(ValueError, match="manage_token"):
            _service(local, storage, _manage()).refresh_tokens()

    def test_mixed_run_adopts_and_mints_with_one_provider_call(self, tmp_path: Path) -> None:
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {
                DEAD_LOCAL: _invalid(),
                DEAD_LOCAL_B: _invalid(),
                GLOBAL_TOKEN: _verified(100),
                MINTED: _verified(200),
            }
        )
        provider = MagicMock(return_value=MANAGE)

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, manage_token_provider=provider
        )

        provider.assert_called_once()
        assert [p["alias"] for p in result["projects_adopted"]] == ["prod"]
        assert [p["alias"] for p in result["projects_refreshed"]] == ["b"]
        assert _token(local, "prod") == GLOBAL_TOKEN
        assert _token(local, "b") == MINTED

    def test_config_changed_between_plan_and_write_is_not_overwritten(self, tmp_path: Path) -> None:
        """R4: another process rewrote the alias after the check -- report, never overwrite."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        concurrent = "100-44-writtenByAnotherProcess0000"
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})
        real_write = _refresh_from_global.write_adoption

        def write_after_concurrent_edit(store: ConfigStore, plan: Any) -> None:
            store.edit_project("prod", token=concurrent)
            real_write(store, plan)

        with patch(
            "keboola_agent_cli.services.org_service.write_adoption", write_after_concurrent_edit
        ):
            result = _service(local, storage, _manage()).refresh_tokens(global_store=global_store)

        assert result["projects_adopted"] == []
        assert [p["alias"] for p in result["projects_failed"]] == ["prod"]
        assert "changed concurrently" in result["projects_failed"][0]["message"]
        assert result["projects_failed"][0]["error_code"] == ErrorCode.CONFIG_ERROR
        assert _token(local, "prod") == concurrent

    def test_edit_during_manage_prompt_mints_nothing_and_keeps_the_edit(
        self, tmp_path: Path
    ) -> None:
        """An alias edited while the Manage-token prompt waited is never overwritten.

        The lazy prompt runs after the plan; minting from that stale snapshot
        would undo the concurrent edit. Nothing may be minted either, or the
        new token would be left orphaned on the server.
        """
        local = _store(tmp_path / "local", {"b": _project(DEAD_LOCAL_B, 200)})
        concurrent = "200-44-writtenByAnotherProcess0000"
        storage = Storage({DEAD_LOCAL_B: _invalid(), MINTED: _verified(200)})
        manage = _manage()

        def prompt_while_another_process_edits() -> str:
            local.edit_project("b", token=concurrent)
            return MANAGE

        result = _service(local, storage, manage).refresh_tokens(
            manage_token_provider=prompt_while_another_process_edits
        )

        assert result["projects_refreshed"] == []
        assert [p["alias"] for p in result["projects_failed"]] == ["b"]
        assert "changed concurrently" in result["projects_failed"][0]["message"]
        manage.create_project_token.assert_not_called()
        assert _token(local, "b") == concurrent

    def test_edit_after_mint_is_kept_and_names_the_unsaved_token(self, tmp_path: Path) -> None:
        """An edit that lands while the token is being created is not overwritten either."""
        local = _store(tmp_path / "local", {"b": _project(DEAD_LOCAL_B, 200)})
        concurrent = "200-44-writtenByAnotherProcess0000"
        storage = Storage({DEAD_LOCAL_B: _invalid(), MINTED: _verified(200)})
        manage = _manage()
        real_create = manage.create_project_token.side_effect

        def create_while_another_process_edits(**kwargs: Any) -> Any:
            local.edit_project("b", token=concurrent)
            return real_create(**kwargs) if real_create else {"token": MINTED}

        manage.create_project_token.side_effect = create_while_another_process_edits

        result = _service(local, storage, manage).refresh_tokens(manage_token=MANAGE)

        assert result["projects_refreshed"] == []
        message = result["projects_failed"][0]["message"]
        assert "changed concurrently" in message and "token delete" in message
        assert MINTED not in message
        assert _token(local, "b") == concurrent

    def test_removed_alias_between_plan_and_write_is_reported(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})
        real_write = _refresh_from_global.write_adoption

        def write_after_removal(store: ConfigStore, plan: Any) -> None:
            store.remove_project("prod")
            real_write(store, plan)

        with patch("keboola_agent_cli.services.org_service.write_adoption", write_after_removal):
            result = _service(local, storage, _manage()).refresh_tokens(global_store=global_store)

        assert [p["alias"] for p in result["projects_failed"]] == ["prod"]
        assert "prod" not in local.load().projects


class TestOwnerLookupBeforeWrites:
    def test_failing_owner_lookup_still_reports_every_adoption(self, tmp_path: Path) -> None:
        """Review fix 3: a Manage client factory that raises must not hide written adoptions."""
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), DEAD_LOCAL_B: _invalid(), GLOBAL_TOKEN: _verified(100)}
        )

        def broken_manage_factory(url: str, token: str) -> MagicMock:
            raise RuntimeError("manage client unavailable")

        service = OrgService(
            config_store=local,
            manage_client_factory=broken_manage_factory,
            storage_client_factory=storage,
        )
        result = service.refresh_tokens(global_store=global_store, manage_token=MANAGE)

        assert [p["alias"] for p in result["projects_adopted"]] == ["prod"]
        assert [p["alias"] for p in result["projects_failed"]] == ["b"]
        assert _token(local, "prod") == GLOBAL_TOKEN
        assert _token(local, "b") == DEAD_LOCAL_B


class TestServiceNoAdoption:
    """Every case where the global config offers nothing: mint as before."""

    def _run(self, tmp_path: Path, global_projects: dict[str, ProjectConfig], **verify: Any) -> Any:
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global(global_projects)
        behaviour: dict[str, Any] = {DEAD_LOCAL: _invalid(), MINTED: _verified(100)}
        behaviour.update(verify)
        storage = Storage(behaviour)
        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=global_store, manage_token=MANAGE
        )
        return result, local, storage

    def test_project_on_another_stack_is_not_adopted(self, tmp_path: Path) -> None:
        """R6."""
        result, local, storage = self._run(
            tmp_path, {"g": _project(GLOBAL_TOKEN, stack=OTHER_STACK)}
        )
        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in storage.seen
        assert _token(local, "prod") == MINTED

    def test_stack_spelling_differences_still_match(self, tmp_path: Path) -> None:
        result, local, _ = self._run(
            tmp_path,
            {"g": _project(GLOBAL_TOKEN, stack="https://Connection.Keboola.com/")},
            **{GLOBAL_TOKEN: _verified(100)},
        )
        assert [p["alias"] for p in result["projects_adopted"]] == ["prod"]
        assert _token(local, "prod") == GLOBAL_TOKEN

    def test_another_port_is_another_stack(self, tmp_path: Path) -> None:
        result, _, storage = self._run(
            tmp_path, {"g": _project(GLOBAL_TOKEN, stack="https://connection.keboola.com:8443")}
        )
        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in storage.seen

    def test_several_distinct_global_tokens_are_ambiguous(self, tmp_path: Path) -> None:
        """R13."""
        result, local, storage = self._run(
            tmp_path,
            {"g1": _project(GLOBAL_TOKEN), "g2": _project(OTHER_GLOBAL)},
            **{GLOBAL_TOKEN: _verified(100), OTHER_GLOBAL: _verified(100)},
        )
        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in storage.seen and OTHER_GLOBAL not in storage.seen
        assert _token(local, "prod") == MINTED

    def test_two_aliases_with_the_same_token_are_one_candidate(self, tmp_path: Path) -> None:
        result, local, _ = self._run(
            tmp_path,
            {"g1": _project(GLOBAL_TOKEN), "g2": _project(GLOBAL_TOKEN)},
            **{GLOBAL_TOKEN: _verified(100)},
        )
        assert [p["global_alias"] for p in result["projects_adopted"]] == ["g1"]
        assert _token(local, "prod") == GLOBAL_TOKEN

    def test_global_token_that_fails_verification_is_not_adopted(self, tmp_path: Path) -> None:
        result, local, _ = self._run(
            tmp_path, {"g": _project(GLOBAL_TOKEN)}, **{GLOBAL_TOKEN: _invalid()}
        )
        assert result["projects_adopted"] == []
        assert _token(local, "prod") == MINTED

    def test_global_token_of_another_project_is_not_adopted(self, tmp_path: Path) -> None:
        result, local, _ = self._run(
            tmp_path, {"g": _project(GLOBAL_TOKEN)}, **{GLOBAL_TOKEN: _verified(999)}
        )
        assert result["projects_adopted"] == []
        assert _token(local, "prod") == MINTED

    def test_global_token_equal_to_the_dead_local_one_is_ignored(self, tmp_path: Path) -> None:
        result, _, _ = self._run(tmp_path, {"g": _project(DEAD_LOCAL)})
        assert result["projects_adopted"] == []
        assert [p["alias"] for p in result["projects_refreshed"]] == ["prod"]

    def test_corrupt_global_config_falls_back_to_minting(self, tmp_path: Path) -> None:
        """R10."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_dir = global_config_dir()
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "config.json").write_text("{not json", encoding="utf-8")
        storage = Storage({DEAD_LOCAL: _invalid(), MINTED: _verified(100)})

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=ConfigStore(config_dir=global_dir), manage_token=MANAGE
        )

        assert result["projects_adopted"] == []
        assert result["projects_failed"] == []
        assert _token(local, "prod") == MINTED

    def test_missing_global_config_falls_back_to_minting(self, tmp_path: Path) -> None:
        """R10."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        storage = Storage({DEAD_LOCAL: _invalid(), MINTED: _verified(100)})

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=ConfigStore(config_dir=tmp_path / "nowhere"), manage_token=MANAGE
        )

        assert result["projects_adopted"] == []
        assert _token(local, "prod") == MINTED
        assert not (tmp_path / "nowhere").exists()

    def test_env_synthesized_global_project_is_not_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R9: the `__env__` project the global load injects is never a candidate."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_store = _global({})
        monkeypatch.setenv("KBAGENT_PROJECT_FROM_ENV", "1")
        monkeypatch.setenv("KBC_TOKEN", GLOBAL_TOKEN)
        monkeypatch.setenv("KBC_STORAGE_API_URL", STACK)
        storage = Storage(
            {DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100), MINTED: _verified(100)}
        )

        result = _service(local, storage, _manage()).refresh_tokens(
            aliases=["prod"], global_store=global_store, manage_token=MANAGE
        )

        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in storage.seen
        assert _token(local, "prod") == MINTED

    def test_global_config_failing_validation_in_any_way_falls_back_to_minting(
        self, tmp_path: Path
    ) -> None:
        """Review fix 1: a non-integer version raised TypeError out of refresh."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_dir = global_config_dir()
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "config.json").write_text(
            '{"version": "broken", "projects": {}}', encoding="utf-8"
        )
        storage = Storage({DEAD_LOCAL: _invalid(), MINTED: _verified(100)})

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=ConfigStore(config_dir=global_dir), manage_token=MANAGE
        )

        assert result["projects_adopted"] == []
        assert result["projects_failed"] == []
        assert _token(local, "prod") == MINTED

    def test_malformed_global_token_never_reaches_the_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Review fix 2: a pydantic error quoting the token must not be logged."""
        secret = "FAKE-SECRET-GLOBAL-TOKEN-VALUE"
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        global_dir = global_config_dir()
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "config.json").write_text(
            json.dumps({"version": 1, "projects": {"g": {"stack_url": STACK, "token": [secret]}}}),
            encoding="utf-8",
        )
        storage = Storage({DEAD_LOCAL: _invalid(), MINTED: _verified(100)})
        caplog.set_level(logging.DEBUG)

        result = _service(local, storage, _manage()).refresh_tokens(
            global_store=ConfigStore(config_dir=global_dir), manage_token=MANAGE
        )

        assert result["projects_adopted"] == []
        assert secret not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)

    def test_failed_global_verify_never_logs_the_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Review fix 2: the verify fallback logs the exception class only."""
        leaky = KeboolaApiError(
            message=f"rejected {GLOBAL_TOKEN}", status_code=401, error_code=ErrorCode.INVALID_TOKEN
        )
        caplog.set_level(logging.DEBUG)
        result, _, _ = self._run(tmp_path, {"g": _project(GLOBAL_TOKEN)}, **{GLOBAL_TOKEN: leaky})
        assert result["projects_adopted"] == []
        assert GLOBAL_TOKEN not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)

    def test_local_ephemeral_project_is_never_offered_an_adoption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix 5: the local `__env__` project falls through to today's path."""
        local = _store(tmp_path / "local", {})
        global_store = _global({"g-prod": _project(GLOBAL_TOKEN)})
        monkeypatch.setenv("KBAGENT_PROJECT_FROM_ENV", "1")
        monkeypatch.setenv("KBC_TOKEN", DEAD_LOCAL)
        monkeypatch.setenv("KBC_STORAGE_API_URL", STACK)
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})

        result = _service(local, storage, _manage()).refresh_tokens(
            aliases=["__env__"], global_store=global_store, dry_run=True
        )

        assert result["projects_adopted"] == []
        assert [p["action"] for p in result["projects_refreshed"]] == ["would_refresh"]
        assert GLOBAL_TOKEN not in storage.seen

    def test_unreadable_global_dir_means_no_candidates(self, tmp_path: Path) -> None:
        """Review fix 4: an OSError while checking the global config is no error."""
        from keboola_agent_cli.services._refresh_from_global import load_global_projects

        _global({"g-prod": _project(GLOBAL_TOKEN)})
        with patch.object(Path, "is_file", side_effect=PermissionError(13, "denied")):
            assert load_global_projects(ConfigStore(config_dir=global_config_dir())) == {}

    def test_pick_skips_ephemeral_and_session_entries(self) -> None:
        from keboola_agent_cli.auth.sentinel import make_session_token

        local = _project(DEAD_LOCAL)
        ephemeral = _project(GLOBAL_TOKEN).model_copy(update={"ephemeral": True})
        session = _project(make_session_token(100))
        assert pick_global_candidate({"__env__": ephemeral, "s": session}, local) is None


# ── Command ─────────────────────────────────────────────────────────────


def _invoke(
    local_dir: Path, storage: Storage, args: list[str], resolver: MagicMock | None = None
) -> Any:
    manage = _manage()

    def build(config_store: ConfigStore) -> OrgService:
        return _service(config_store, storage, manage)

    with ExitStack() as stack:
        stack.enter_context(patch("keboola_agent_cli.cli.OrgService", build))
        if resolver is not None:
            stack.enter_context(
                patch("keboola_agent_cli.commands.project.resolve_manage_token", resolver)
            )
        return runner.invoke(app, ["--config-dir", str(local_dir), *args])


@pytest.fixture(autouse=True)
def _no_manage_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)


class TestCommand:
    def test_mixed_adopt_and_mint_without_manage_token_exits_2_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        """R2a: no TTY, --json, no Manage token -- exit 2 and the adoption is NOT written."""
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), DEAD_LOCAL_B: _invalid(), GLOBAL_TOKEN: _verified(100)}
        )

        result = _invoke(
            tmp_path / "local", storage, ["--json", "project", "refresh", "--all-projects"]
        )

        assert result.exit_code == 2, result.output
        assert _token(local, "prod") == DEAD_LOCAL
        assert _token(local, "b") == DEAD_LOCAL_B

    def test_all_adoptable_needs_no_manage_token(self, tmp_path: Path) -> None:
        """R2b: --json, no TTY, everything adoptable -- exit 0, resolver never called."""
        local = _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})
        resolver = MagicMock(side_effect=AssertionError("resolver called"))

        result = _invoke(
            tmp_path / "local",
            storage,
            ["--json", "project", "refresh", "--all-projects"],
            resolver,
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [p["alias"] for p in payload["data"]["projects_adopted"]] == ["prod"]
        resolver.assert_not_called()
        assert _token(local, "prod") == GLOBAL_TOKEN
        # R14: the token is in neither the JSON nor any masked form.
        assert GLOBAL_TOKEN not in result.output
        assert GLOBAL_TOKEN[:10] not in result.output

    def test_human_output_never_shows_the_adopted_token(self, tmp_path: Path) -> None:
        """R14 (human) and the headline (D7)."""
        _store(tmp_path / "local", {"prod": _project(DEAD_LOCAL)})
        _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage({DEAD_LOCAL: _invalid(), GLOBAL_TOKEN: _verified(100)})

        result = _invoke(
            tmp_path / "local", storage, ["project", "refresh", "--all-projects", "--yes"]
        )

        assert result.exit_code == 0, result.output
        assert GLOBAL_TOKEN not in result.output
        assert GLOBAL_TOKEN[:10] not in result.output
        assert "g-prod" in result.output
        assert "1 token(s) taken from the global config, 0 refreshed." in result.output
        assert LOCAL_COPIES_HINT not in result.output

    def test_dry_run_never_calls_the_resolver(self, tmp_path: Path) -> None:
        """R2c at the command."""
        local = _store(
            tmp_path / "local", {"prod": _project(DEAD_LOCAL), "b": _project(DEAD_LOCAL_B, 200)}
        )
        _global({"g-prod": _project(GLOBAL_TOKEN)})
        storage = Storage(
            {DEAD_LOCAL: _invalid(), DEAD_LOCAL_B: _invalid(), GLOBAL_TOKEN: _verified(100)}
        )
        resolver = MagicMock(side_effect=AssertionError("resolver called"))

        result = _invoke(
            tmp_path / "local",
            storage,
            ["project", "refresh", "--all-projects", "--dry-run"],
            resolver,
        )

        assert result.exit_code == 0, result.output
        resolver.assert_not_called()
        assert (
            "1 token(s) would be taken from the global config, 1 would be refreshed."
            in result.output
        )
        assert _token(local, "prod") == DEAD_LOCAL

    def test_run_on_the_global_config_never_adopts_and_prints_the_hint(
        self, tmp_path: Path
    ) -> None:
        """D1 + D8: the global config is the refreshed one -- mint, then the hint."""
        global_store = _global({"prod": _project(DEAD_LOCAL)})
        storage = Storage({DEAD_LOCAL: _invalid(), MINTED: _verified(100)})
        resolver = MagicMock(return_value=MANAGE)

        result = _invoke(
            global_config_dir(),
            storage,
            ["project", "refresh", "--all-projects", "--yes"],
            resolver,
        )

        assert result.exit_code == 0, result.output
        resolver.assert_called_once()
        assert _token(global_store, "prod") == MINTED
        assert LOCAL_COPIES_HINT.split(";")[0] in " ".join(result.output.split())

    def test_local_mint_has_no_hint(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {"b": _project(DEAD_LOCAL_B, 200)})
        storage = Storage({DEAD_LOCAL_B: _invalid(), MINTED: _verified(200)})

        result = _invoke(
            tmp_path / "local",
            storage,
            ["project", "refresh", "--all-projects", "--yes"],
            MagicMock(return_value=MANAGE),
        )

        assert result.exit_code == 0, result.output
        assert _token(local, "b") == MINTED
        assert "init --from-global" not in result.output


class TestGlobalStoreLookup:
    def test_other_dir_gets_the_global_store(self, tmp_path: Path) -> None:
        _global({})
        local = _store(tmp_path / "local", {})
        found = global_store_for(local)
        assert found is not None
        assert found.config_dir == global_config_dir()

    def test_no_global_config_file_means_no_store(self, tmp_path: Path) -> None:
        local = _store(tmp_path / "local", {})
        assert global_store_for(local) is None

    def test_global_dir_by_another_spelling_is_the_global_config(self) -> None:
        """R5: canonical path compare, not the source label."""
        _global({})
        global_dir = global_config_dir()
        respelled = global_dir.parent / "." / global_dir.name / ".." / global_dir.name
        store = ConfigStore(config_dir=respelled, source="cli-flag")
        assert is_global_config(store)
        assert global_store_for(store) is None

    def test_unreadable_global_dir_means_no_store(self, tmp_path: Path) -> None:
        """Review fix 4: EACCES on the global dir must not crash `project refresh`."""
        local = _store(tmp_path / "local", {})
        with patch.object(Path, "is_file", side_effect=PermissionError(13, "denied")):
            assert global_store_for(local) is None

    def test_global_dir_through_a_symlink_is_the_global_config(self, tmp_path: Path) -> None:
        _global({})
        link = tmp_path / "link"
        link.symlink_to(global_config_dir(), target_is_directory=True)
        assert global_store_for(ConfigStore(config_dir=link, source="env-var")) is None
