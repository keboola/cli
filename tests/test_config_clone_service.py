"""Tests for ConfigService.clone_config (the `config clone` command, issue #587).

Cloning a configuration by hand -- reading `config detail` and rebuilding the
body -- silently drops sibling keys of `parameters` (`runtime`, `storage`,
`authorization`). The reporter of #587 lost `runtime.parallelism` that way and
a 65-row writer ran sequentially for 140 minutes instead of ~60-90.

Two paths, deliberately different:

- **Same project**: the Storage API copies server-side
  (`POST .../versions/{v}/create`), so nothing is rebuilt and encrypted
  `KBC::` values stay valid.
- **Cross project**: we assemble it ourselves, because encrypted values are
  scoped to their project and cannot travel. Those are detected and must be
  re-supplied, or the clone is refused.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.services.config_service import ConfigService

SOURCE_DETAIL = {
    "id": "src-1",
    "name": "Snowflake writer",
    "description": "writes to prod",
    "version": 7,
    "configuration": {
        "parameters": {"db": {"host": "h", "#password": "KBC::ProjectSecure::abc"}},
        "runtime": {"parallelism": "20"},
        "storage": {"input": {"tables": [{"source": "in.c-main.orders"}]}},
    },
    "rows": [
        {"id": "r1", "name": "orders", "configuration": {"parameters": {"table": "orders"}}},
        {"id": "r2", "name": "users", "configuration": {"parameters": {"table": "users"}}},
    ],
}


def _make_service(
    tmp_config_dir: Path,
    *,
    detail: dict | None = None,
) -> tuple[ConfigService, MagicMock]:
    """Build a ConfigService wired to a single mock Storage client."""
    store = setup_single_project(tmp_config_dir)

    client = MagicMock()
    client.get_config_detail.return_value = dict(detail or SOURCE_DETAIL)
    client.create_config_copy.return_value = {"id": "clone-1"}
    client.create_config.return_value = {"id": "clone-1", "version": 1}
    client.create_config_row.return_value = {"id": "row-new"}
    client.update_config.return_value = {"id": "clone-1", "version": 2}

    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: client,
    )
    return service, client


class TestCloneSameProject:
    """Same-project clone delegates to the server-side copy endpoint."""

    def test_uses_server_side_copy_at_the_source_version(self, tmp_config_dir: Path) -> None:
        """The copy is taken from the source's CURRENT version, and nothing is
        rebuilt client-side -- that is the whole point of the same-project path.
        """
        service, client = _make_service(tmp_config_dir)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="Snowflake writer (copy)",
        )

        client.create_config_copy.assert_called_once()
        kwargs = client.create_config_copy.call_args.kwargs
        assert kwargs["config_id"] == "src-1"
        assert kwargs["version"] == 7
        assert kwargs["name"] == "Snowflake writer (copy)"
        # Nothing is assembled by hand on this path.
        client.create_config.assert_not_called()
        assert result["id"] == "clone-1"
        assert result["mode"] == "same-project"

    def test_encrypted_values_are_not_an_obstacle_within_a_project(
        self, tmp_config_dir: Path
    ) -> None:
        """`KBC::` values stay valid in the same project, so a clone carrying
        them must not be refused (the cross-project path is where they block).
        """
        service, client = _make_service(tmp_config_dir)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
        )

        assert result["encrypted_paths"] == []
        client.create_config_copy.assert_called_once()

    def test_dry_run_makes_no_write_call(self, tmp_config_dir: Path) -> None:
        """Dry-run reports the plan; no copy, no create, no update."""
        service, client = _make_service(tmp_config_dir)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            dry_run=True,
        )

        client.create_config_copy.assert_not_called()
        client.create_config.assert_not_called()
        client.update_config.assert_not_called()
        assert result["dry_run"] is True
        assert result["mode"] == "same-project"
        assert result["source_version"] == 7

    def test_set_overrides_are_applied_after_the_copy(self, tmp_config_dir: Path) -> None:
        """--set edits land via a follow-up update on the NEW config, leaving
        every key the copy brought along intact.
        """
        service, client = _make_service(tmp_config_dir)
        # The clone is re-read before patching, so return its (copied) body.
        client.get_config_detail.side_effect = [
            dict(SOURCE_DETAIL),
            {"id": "clone-1", "version": 1, "configuration": SOURCE_DETAIL["configuration"]},
        ]

        service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            set_overrides={"parameters.db.host": "new-host"},
        )

        client.update_config.assert_called_once()
        patched = client.update_config.call_args.kwargs["configuration"]
        assert patched["parameters"]["db"]["host"] == "new-host"
        # The sibling that #587 is about survives the override step.
        assert patched["runtime"] == {"parallelism": "20"}

    def test_no_overrides_means_no_update_call(self, tmp_config_dir: Path) -> None:
        """Without --set the copy is already final; no pointless second write."""
        service, client = _make_service(tmp_config_dir)

        service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
        )

        client.update_config.assert_not_called()


def _make_two_project_service(
    tmp_config_dir: Path,
    *,
    detail: dict | None = None,
) -> tuple[ConfigService, MagicMock, MagicMock]:
    """Build a service over two DISTINCT projects, returning (service, source, target)."""
    from keboola_agent_cli.models import ProjectConfig

    store = setup_single_project(tmp_config_dir)
    config = store.load()
    config.projects["prod"].project_id = 100
    config.projects["dev"] = ProjectConfig(
        stack_url="https://connection.keboola.com",
        token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
        project_name="Dev",
        project_id=200,
    )
    store.save(config)

    source_client = MagicMock()
    source_client.get_config_detail.return_value = dict(detail or SOURCE_DETAIL)
    target_client = MagicMock()
    target_client.create_config.return_value = {"id": "clone-1"}
    target_client.create_config_row.side_effect = [{"id": "new-r1"}, {"id": "new-r2"}]
    clients = [source_client, target_client]

    def factory(url: str, token: str) -> MagicMock:
        return clients.pop(0) if clients else target_client

    service = ConfigService(config_store=store, client_factory=factory)
    return service, source_client, target_client


class TestCloneCrossProjectEncryptedValues:
    """Ciphertext is project-scoped, so a cross-project clone must not carry it.

    Copying a `KBC::` value into another project produces a configuration that
    looks complete and fails at runtime -- in a project the operator is not
    watching. The clone is refused until each value is re-supplied.
    """

    def test_refuses_when_an_encrypted_value_was_not_resupplied(self, tmp_config_dir: Path) -> None:
        service, _, target = _make_two_project_service(tmp_config_dir)

        with pytest.raises(ConfigError, match="encrypted value"):
            service.clone_config(
                alias="prod",
                component_id="keboola.wr-db-snowflake",
                config_id="src-1",
                name="copy",
                target_alias="dev",
            )
        target.create_config.assert_not_called()

    def test_refusal_names_every_path_that_needs_a_value(self, tmp_config_dir: Path) -> None:
        """The error must be actionable -- a path the caller can pass to --secret."""
        service, _, _ = _make_two_project_service(tmp_config_dir)

        with pytest.raises(ConfigError) as exc_info:
            service.clone_config(
                alias="prod",
                component_id="keboola.wr-db-snowflake",
                config_id="src-1",
                name="copy",
                target_alias="dev",
            )
        assert "parameters.db.#password" in str(exc_info.value)
        assert "--secret" in str(exc_info.value)

    def test_supplied_secret_is_written_and_encrypted_in_the_target(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-supplied plaintext replaces the ciphertext, and encryption is
        scoped to the TARGET project -- encrypting against the source would
        produce a value the target still cannot read.
        """
        service, _, target = _make_two_project_service(tmp_config_dir)
        seen: list[tuple[int | None, dict]] = []

        def fake_encrypt(client, project, component_id, configuration, *, allow_plaintext_fallback):
            seen.append((project.project_id, configuration))
            return configuration

        monkeypatch.setattr(service, "_encrypt_secrets_before_write", fake_encrypt)

        service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="dev",
            secret_overrides={"parameters.db.#password": "fresh-secret"},
        )

        target.create_config.assert_called_once()
        body = target.create_config.call_args.kwargs["configuration"]
        assert body["parameters"]["db"]["#password"] == "fresh-secret"
        # Every encryption call was scoped to the target project (id 200).
        assert {project_id for project_id, _ in seen} == {200}

    def test_rows_are_copied_into_the_new_configuration(self, tmp_config_dir: Path) -> None:
        """Cross-project has no server-side copy, so rows are recreated by hand."""
        service, _, target = _make_two_project_service(tmp_config_dir)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="dev",
            secret_overrides={"parameters.db.#password": "fresh"},
        )

        assert target.create_config_row.call_count == 2
        names = [c.kwargs["name"] for c in target.create_config_row.call_args_list]
        assert names == ["orders", "users"]
        assert result["copied_rows"] == [
            {"source_row_id": "r1", "id": "new-r1"},
            {"source_row_id": "r2", "id": "new-r2"},
        ]

    def test_sibling_keys_survive_a_cross_project_clone(self, tmp_config_dir: Path) -> None:
        """The whole reason the command exists: runtime/storage must travel."""
        service, _, target = _make_two_project_service(tmp_config_dir)

        service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="dev",
            secret_overrides={"parameters.db.#password": "fresh"},
        )

        body = target.create_config.call_args.kwargs["configuration"]
        assert body["runtime"] == {"parallelism": "20"}
        assert body["storage"] == {"input": {"tables": [{"source": "in.c-main.orders"}]}}

    def test_dry_run_reports_missing_secrets_instead_of_raising(self, tmp_config_dir: Path) -> None:
        """--dry-run is how a caller discovers what --secret values to gather,
        so it must report rather than refuse.
        """
        service, _, target = _make_two_project_service(tmp_config_dir)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="dev",
            dry_run=True,
        )

        target.create_config.assert_not_called()
        assert result["mode"] == "cross-project"
        assert result["missing_secrets"] == ["parameters.db.#password"]
        assert result["row_count"] == 2

    def test_row_level_encrypted_values_are_detected_too(self, tmp_config_dir: Path) -> None:
        """A row can hold its own secret; missing it breaks the clone just as
        thoroughly, so rows are scanned as well as the parent body.
        """
        detail = dict(SOURCE_DETAIL)
        detail["rows"] = [
            {
                "id": "r1",
                "name": "orders",
                "configuration": {"parameters": {"#token": "KBC::ProjectSecure::xyz"}},
            }
        ]
        service, _, _ = _make_two_project_service(tmp_config_dir, detail=detail)

        result = service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="dev",
            dry_run=True,
        )

        assert "rows[0].parameters.#token" in result["missing_secrets"]


class TestCloneTargetsTheRightClient:
    """The client used for writes must agree with the same/cross decision.

    If the service decided "same project" (reuse the source client) while the
    clone logic decided "cross project", the cross-project write would go to
    the SOURCE project -- creating a configuration in the wrong place while
    reporting success for the target.
    """

    def test_two_aliases_without_project_ids_do_not_share_a_client(
        self, tmp_config_dir: Path
    ) -> None:
        """An unrecorded project_id must not be treated as "same project"."""
        from keboola_agent_cli.models import ProjectConfig

        store = setup_single_project(tmp_config_dir)
        config = store.load()
        config.projects["prod"].project_id = None
        config.projects["other"] = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
            project_name="Other",
            project_id=None,
        )
        store.save(config)

        source_client = MagicMock()
        source_client.get_config_detail.return_value = dict(SOURCE_DETAIL)
        target_client = MagicMock()
        target_client.create_config.return_value = {"id": "clone-1"}
        target_client.create_config_row.return_value = {"id": "row-new"}
        handed_out: list[MagicMock] = []

        def factory(url: str, token: str) -> MagicMock:
            client = source_client if not handed_out else target_client
            handed_out.append(client)
            return client

        service = ConfigService(config_store=store, client_factory=factory)

        service.clone_config(
            alias="prod",
            component_id="keboola.wr-db-snowflake",
            config_id="src-1",
            name="copy",
            target_alias="other",
            # The source body holds one KBC:: value; supply it so the clone
            # is not refused and we can observe WHERE it gets written.
            secret_overrides={"parameters.db.#password": "fresh"},
        )

        # Two distinct clients were requested, and the write went to the target.
        assert len(handed_out) == 2
        target_client.create_config.assert_called_once()
        source_client.create_config.assert_not_called()
        source_client.create_config_copy.assert_not_called()


class TestCloneRejectsUnknownProjects:
    def test_unknown_source_alias_raises(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(ConfigError):
            service.clone_config(
                alias="nope",
                component_id="keboola.wr-db-snowflake",
                config_id="src-1",
                name="copy",
            )
        client.create_config_copy.assert_not_called()

    def test_unknown_target_alias_raises(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(ConfigError):
            service.clone_config(
                alias="prod",
                component_id="keboola.wr-db-snowflake",
                config_id="src-1",
                name="copy",
                target_alias="nope",
            )
        client.create_config_copy.assert_not_called()
