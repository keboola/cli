"""`sync push` encrypts every secret before its first write (issue #792 F).

Push used to encrypt each config's ``#`` secrets just before its own write. An
``ENCRYPTION_FAILED`` on a later config then stopped the push after earlier
configs were created, and the manifest, saved only at the end, did not record
them: the retry created them again. Push now encrypts all secrets first, so an
encryption failure stops it before anything reaches the remote.

The replay of the original finding is
``test_sync_formal_counterexamples.py::test_f_aborted_push_does_not_duplicate_already_created_config``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services._encryption import encrypt_secrets_in_config
from keboola_agent_cli.services._sync_push_ops import PushPlan
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestNaming,
    ManifestProject,
    load_manifest,
)
from test_sync_formal_counterexamples import COMP, PROD, World


def _new_config(w: World, name: str, secret: str) -> Path:
    """A new local config (no id yet), which push creates."""
    config_dir = w.root / "main" / "extractor" / COMP / name.lower()
    config_dir.mkdir(parents=True)
    (config_dir / CONFIG_FILENAME).write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": name,
                "parameters": {"#token": secret},
                "_keboola": {"component_id": COMP},
            }
        ),
        encoding="utf-8",
    )
    return config_dir


def _encrypt(fail_on: str | None = None) -> Any:
    def encrypt_values(project_id: int, component_id: str, data: dict[str, str]) -> dict[str, str]:
        if fail_on is not None and fail_on in data.values():
            raise RuntimeError("Encryption API unavailable")
        return {key: f"KBC::ProjectSecure::{value}" for key, value in data.items()}

    return encrypt_values


def _world_with_two_new_configs(tmp_path: Path, fail_on: str | None) -> World:
    w = World(tmp_path)
    w.init()
    w.pull()
    _new_config(w, "Orders", "ok-secret")
    _new_config(w, "Contacts", "bad-secret")
    w.api.encrypt_values = MagicMock(side_effect=_encrypt(fail_on))
    return w


def _world_with_an_edit_and_a_new_config(tmp_path: Path) -> World:
    """A tracked config with a new secret, then a new config whose secret fails.

    Push writes tracked configs before new ones, so the update used to reach
    the remote before the create failed.
    """
    w = World(tmp_path)
    w.api.put(PROD, "cfg-1", "Orders", "a", extra={"#token": "KBC::ProjectSecure::old"})
    w.init()
    w.pull()
    config_file = w.config_dir("orders") / CONFIG_FILENAME
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["parameters"]["#token"] = "ok-secret"
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    _new_config(w, "Contacts", "bad-secret")
    w.api.encrypt_values = MagicMock(side_effect=_encrypt("bad-secret"))
    return w


def test_encryption_failure_stops_the_push_before_any_write(tmp_path: Path) -> None:
    w = _world_with_an_edit_and_a_new_config(tmp_path)
    manifest_before = load_manifest(w.root).model_dump()
    files_before = {p: p.read_text() for p in w.root.rglob(CONFIG_FILENAME)}

    with pytest.raises(KeboolaApiError) as exc_info:
        w.push()

    assert exc_info.value.error_code == ErrorCode.ENCRYPTION_FAILED
    assert "Nothing was pushed." in exc_info.value.message
    assert w.api.log == []
    assert load_manifest(w.root).model_dump() == manifest_before
    assert {p: p.read_text() for p in w.root.rglob(CONFIG_FILENAME)} == files_before


def test_retry_after_the_failure_applies_each_change_once(tmp_path: Path) -> None:
    w = _world_with_an_edit_and_a_new_config(tmp_path)
    with pytest.raises(KeboolaApiError):
        w.push()

    w.api.encrypt_values = MagicMock(side_effect=_encrypt())
    result = w.push()

    assert (result["updated"], result["created"]) == (1, 1)
    assert sorted(line.split()[0] for line in w.api.log) == ["CREATE", "UPDATE"]
    assert w.diff()["changes"] == []


def test_secrets_are_encrypted_once_and_the_writes_send_the_ciphertext(tmp_path: Path) -> None:
    w = _world_with_two_new_configs(tmp_path, fail_on=None)

    w.push()

    # One call for the component, before the writes; none from the writes.
    assert w.api.encrypt_values.call_count == 1
    sent = sorted(
        cfg["configuration"]["parameters"]["#token"] for cfg in w.api.remote[PROD].values()
    )
    assert sent == ["KBC::ProjectSecure::bad-secret", "KBC::ProjectSecure::ok-secret"]


# ---------------------------------------------------------------------------
# PushPlan.encrypt_secrets and the known-value cache
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / CONFIG_FILENAME).write_text(yaml.safe_dump(data), encoding="utf-8")


def _service() -> MagicMock:
    service = MagicMock()
    service._read_config_file.side_effect = lambda config_dir: (
        yaml.safe_load((config_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
        if (config_dir / CONFIG_FILENAME).exists()
        else None
    )
    return service


def _manifest() -> Manifest:
    return Manifest(
        project=ManifestProject(id=258, apiHost="connection.example.com"),
        naming=ManifestNaming(),
        configurations=[
            ManifestConfiguration(
                branchId=0,
                componentId=COMP,
                id="cfg-1",
                path="extractor/keboola.ex-http/orders",
                rows=[ManifestConfigRow(id="row-1", path="rows/first")],
            )
        ],
    )


def test_encrypt_secrets_covers_rows_and_skips_what_push_does_not_write(tmp_path: Path) -> None:
    config_dir = tmp_path / "extractor/keboola.ex-http/orders"
    _write_yaml(config_dir, {"name": "Orders", "parameters": {"#token": "config-secret"}})
    _write_yaml(config_dir / "rows/first", {"name": "First", "parameters": {"#pw": "row-secret"}})
    _write_yaml(tmp_path / "gone", {"name": "Gone", "parameters": {"#pw": "deleted-secret"}})
    client = MagicMock()
    client.encrypt_values.side_effect = _encrypt()
    plan = PushPlan(
        changes=[
            {
                "change_type": "modified",
                "component_id": COMP,
                "path": "extractor/keboola.ex-http/orders",
            },
            {
                "change_type": "modified",
                "component_id": COMP,
                "path": "rows/first",
                "is_row": True,
                "parent_config_id": "cfg-1",
            },
            {
                "change_type": "added",
                "component_id": COMP,
                "path": "rows/orphan",
                "is_row": True,
                "parent_config_id": "not-tracked",
            },
            {"change_type": "deleted", "component_id": COMP, "path": "gone"},
        ],
        skipped=[],
        skipped_deletions=[],
    )

    cache = plan.encrypt_secrets(_service(), client, tmp_path, _manifest(), False)

    assert cache == {
        (COMP, "config-secret"): "KBC::ProjectSecure::config-secret",
        (COMP, "row-secret"): "KBC::ProjectSecure::row-secret",
    }
    assert client.encrypt_values.call_count == 1


def test_encrypt_secrets_never_caches_a_value_the_api_left_in_plaintext(tmp_path: Path) -> None:
    """A partial API answer must not turn a plaintext value into a cache hit."""
    config_dir = tmp_path / "orders"
    _write_yaml(config_dir, {"name": "Orders", "parameters": {"#a": "first", "#b": "second"}})
    client = MagicMock()
    client.encrypt_values.return_value = {"#secret0": "KBC::ProjectSecure::first"}
    plan = PushPlan(
        changes=[{"change_type": "added", "component_id": COMP, "path": "orders"}],
        skipped=[],
        skipped_deletions=[],
    )

    cache = plan.encrypt_secrets(_service(), client, tmp_path, _manifest(), False)

    assert cache == {(COMP, "first"): "KBC::ProjectSecure::first"}


def test_encrypt_secrets_does_nothing_with_the_plaintext_fallback(tmp_path: Path) -> None:
    config_dir = tmp_path / "orders"
    _write_yaml(config_dir, {"name": "Orders", "parameters": {"#token": "config-secret"}})
    client = MagicMock()
    plan = PushPlan(
        changes=[{"change_type": "added", "component_id": COMP, "path": "orders"}],
        skipped=[],
        skipped_deletions=[],
    )

    assert plan.encrypt_secrets(_service(), client, tmp_path, _manifest(), True) == {}
    client.encrypt_values.assert_not_called()


def test_known_values_are_applied_without_an_api_call() -> None:
    client = MagicMock()
    client.encrypt_values.side_effect = _encrypt()
    configuration = {"parameters": {"#known": "a", "nested": {"#new": "b"}}}

    encrypt_secrets_in_config(
        client, 258, COMP, configuration, known={(COMP, "a"): "KBC::ProjectSecure::cached"}
    )

    assert configuration == {
        "parameters": {
            "#known": "KBC::ProjectSecure::cached",
            "nested": {"#new": "KBC::ProjectSecure::b"},
        }
    }
    client.encrypt_values.assert_called_once_with(
        project_id=258, component_id=COMP, data={"#parameters.nested.#new": "b"}
    )
