"""Tests for AuthStateStore (auth/state_store.py) persistence of auth.json."""

import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from keboola_agent_cli.auth.models import AuthState, StackSession
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import AUTH_STATE_VERSION
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig


def _make_session(stack_url: str = "https://connection.keboola.com") -> StackSession:
    return StackSession(
        stack_url=stack_url,
        session_id="sess-1",
        user_email="user@example.com",
        user_name="Test User",
        access_token="kbc_at_abc123",
        refresh_token="kbc_rt_def456",
        access_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        refresh_expires_at=datetime(2026, 2, 1, tzinfo=UTC),
        created_at=datetime(2025, 12, 1, tzinfo=UTC),
    )


class TestFilePermissions:
    def test_saved_file_is_0600(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.put_session(_make_session())

        mode = stat.S_IMODE(store.state_path.stat().st_mode)
        assert mode == 0o600

    def test_overly_broad_permissions_are_reset_on_load(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.put_session(_make_session())
        os.chmod(store.state_path, 0o644)

        store.load()

        mode = stat.S_IMODE(store.state_path.stat().st_mode)
        assert mode == 0o600


class TestAtomicWrite:
    def test_save_never_leaves_tmp_file_behind(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.put_session(_make_session())

        assert store.state_path.exists()
        assert not store.state_path.with_suffix(".tmp").exists()

    def test_load_missing_file_returns_empty_state(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        state = store.load()
        assert state == AuthState()
        assert state.sessions == {}


class TestUnknownFieldPassthrough:
    def test_extra_field_on_session_round_trips(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        raw_state = {
            "version": AUTH_STATE_VERSION,
            "sessions": {
                "https://connection.keboola.com": {
                    **_make_session().model_dump(mode="json"),
                    "future_field": "from-a-newer-kbagent",
                }
            },
        }
        store.state_path.parent.mkdir(parents=True, exist_ok=True)
        store.state_path.write_text(json.dumps(raw_state), encoding="utf-8")
        os.chmod(store.state_path, 0o600)

        loaded = store.load()
        session = loaded.sessions["https://connection.keboola.com"]
        assert session.model_extra is not None
        assert session.model_extra.get("future_field") == "from-a-newer-kbagent"


class TestCorruptJson:
    def test_corrupt_json_raises_config_error(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.state_path.parent.mkdir(parents=True, exist_ok=True)
        store.state_path.write_text("{not valid json", encoding="utf-8")

        with pytest.raises(ConfigError, match="not valid JSON"):
            store.load()

    def test_non_object_json_raises_config_error(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.state_path.parent.mkdir(parents=True, exist_ok=True)
        store.state_path.write_text("[1, 2, 3]", encoding="utf-8")

        with pytest.raises(ConfigError, match="invalid structure"):
            store.load()


class TestFutureVersion:
    def test_future_version_raises_config_error(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.state_path.parent.mkdir(parents=True, exist_ok=True)
        store.state_path.write_text(
            json.dumps({"version": AUTH_STATE_VERSION + 1, "sessions": {}}), encoding="utf-8"
        )

        with pytest.raises(ConfigError, match="upgrade"):
            store.load()


class TestStackKeyNormalization:
    def test_deep_link_and_bare_host_share_a_key(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        session = _make_session(
            stack_url="https://connection.keboola.com/admin/projects/10105/dashboard"
        )
        store.put_session(session)

        found = store.get_session("connection.keboola.com")
        assert found is not None
        assert found.session_id == "sess-1"

        found_trailing_slash = store.get_session("https://connection.keboola.com/")
        assert found_trailing_slash is not None
        assert found_trailing_slash.session_id == "sess-1"

    def test_put_session_normalizes_stored_stack_url(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        session = _make_session(stack_url="https://connection.keboola.com/")
        store.put_session(session)

        state = store.load()
        assert list(state.sessions.keys()) == ["https://connection.keboola.com"]
        assert state.sessions["https://connection.keboola.com"].stack_url == (
            "https://connection.keboola.com"
        )


class TestMutators:
    def test_delete_session_returns_true_when_removed(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.put_session(_make_session())

        assert store.delete_session("https://connection.keboola.com") is True
        assert store.get_session("https://connection.keboola.com") is None

    def test_delete_session_returns_false_when_absent(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        assert store.delete_session("https://connection.keboola.com") is False

    def test_record_and_clear_orphans(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        store.put_session(_make_session())

        store.record_orphan("https://connection.keboola.com", "old-session-1")
        store.record_orphan("https://connection.keboola.com", "old-session-1")  # idempotent

        session = store.get_session("https://connection.keboola.com")
        assert session is not None
        assert session.orphaned_session_ids == ["old-session-1"]

        store.clear_orphans("https://connection.keboola.com")
        session = store.get_session("https://connection.keboola.com")
        assert session is not None
        assert session.orphaned_session_ids == []

    def test_record_orphan_is_noop_when_session_missing(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        # Must not raise.
        store.record_orphan("https://connection.keboola.com", "orphan-1")
        assert store.get_session("https://connection.keboola.com") is None


class TestTransactionReentrancy:
    def test_nested_transaction_does_not_deadlock(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        with store.transaction():
            with store.transaction():
                store.save(AuthState())
            state = store.load()
        assert state == AuthState()

    def test_transaction_serializes_across_threads(self, tmp_config_dir: Path) -> None:
        store = AuthStateStore(config_dir=tmp_config_dir)
        order: list[str] = []
        barrier = threading.Barrier(2)

        def worker(label: str, hold_seconds: float) -> None:
            barrier.wait()
            with store.transaction():
                order.append(f"{label}-start")
                threading.Event().wait(hold_seconds)
                order.append(f"{label}-end")

        t1 = threading.Thread(target=worker, args=("a", 0.05))
        t2 = threading.Thread(target=worker, args=("b", 0.0))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Whichever thread starts first must fully finish (start, end) before
        # the other thread's transaction is allowed to start.
        first_start = order[0]
        first_label = first_start.split("-")[0]
        assert order[1] == f"{first_label}-end"


class TestTokensNeverReachConfigJson:
    def test_config_store_and_auth_state_store_write_separate_files(
        self, tmp_config_dir: Path
    ) -> None:
        config_store = ConfigStore(config_dir=tmp_config_dir)
        config_store.add_project(
            "static-project",
            ProjectConfig(stack_url="https://connection.keboola.com", token="123-abc-def"),
        )

        auth_store = AuthStateStore.from_config_store(config_store)
        auth_store.put_session(_make_session())

        config_text = config_store.config_path.read_text(encoding="utf-8")
        assert "kbc_at_abc123" not in config_text
        assert "kbc_rt_def456" not in config_text

        auth_text = auth_store.state_path.read_text(encoding="utf-8")
        assert "123-abc-def" not in auth_text

        assert config_store.config_path != auth_store.state_path
        assert auth_store.config_dir == tmp_config_dir
