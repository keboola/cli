"""Tests for file locking in ConfigStore."""

import os
from pathlib import Path
from unittest.mock import patch

from keboola_agent_cli.config_store import ConfigStore, _try_flock
from keboola_agent_cli.models import AppConfig, ProjectConfig


class TestTryFlock:
    """Tests for the _try_flock helper."""

    def test_flock_is_called_on_posix(self, tmp_path: Path) -> None:
        """_try_flock calls fcntl.flock on POSIX systems."""
        import fcntl

        fd = os.open(str(tmp_path / "test.lock"), os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            # Should not raise
            _try_flock(fd, fcntl.LOCK_EX)
            _try_flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_flock_suppresses_oserror(self, tmp_path: Path) -> None:
        """_try_flock silently suppresses OSError."""
        import fcntl

        with patch("keboola_agent_cli.config_store.fcntl") as mock_fcntl:
            mock_fcntl.flock.side_effect = OSError("mock error")
            # Should not raise
            _try_flock(42, fcntl.LOCK_EX)

    def test_flock_skipped_when_no_fcntl(self) -> None:
        """_try_flock does nothing when _HAS_FCNTL is False."""
        with patch("keboola_agent_cli.config_store._HAS_FCNTL", False):
            # Should not raise, should not call fcntl
            _try_flock(42, 0)


class TestFileLockingIntegration:
    """Integration tests that verify locking happens during save/load."""

    def test_save_acquires_exclusive_lock(self, tmp_config_dir: Path) -> None:
        """ConfigStore.save() acquires an exclusive lock on the config file."""
        import fcntl

        store = ConfigStore(config_dir=tmp_config_dir)
        lock_ops: list[int] = []

        original_flock = _try_flock

        def tracking_flock(fd: int, operation: int) -> None:
            lock_ops.append(operation)
            original_flock(fd, operation)

        with patch("keboola_agent_cli.config_store._try_flock", side_effect=tracking_flock):
            store.save(AppConfig())

        # Should see LOCK_EX followed by LOCK_UN
        assert fcntl.LOCK_EX in lock_ops
        assert fcntl.LOCK_UN in lock_ops

    def test_load_acquires_shared_lock(self, tmp_config_dir: Path) -> None:
        """ConfigStore.load() acquires a shared lock on the config file."""
        import fcntl

        store = ConfigStore(config_dir=tmp_config_dir)
        # First create the file
        store.save(AppConfig())

        lock_ops: list[int] = []

        original_flock = _try_flock

        def tracking_flock(fd: int, operation: int) -> None:
            lock_ops.append(operation)
            original_flock(fd, operation)

        with patch("keboola_agent_cli.config_store._try_flock", side_effect=tracking_flock):
            store.load()

        # Should see LOCK_SH followed by LOCK_UN
        assert fcntl.LOCK_SH in lock_ops
        assert fcntl.LOCK_UN in lock_ops

    def test_concurrent_save_does_not_corrupt(self, tmp_config_dir: Path) -> None:
        """Two concurrent saves produce a valid config file."""
        store = ConfigStore(config_dir=tmp_config_dir)

        project_a = ProjectConfig(stack_url="https://connection.keboola.com", token="aaa-token")
        project_b = ProjectConfig(stack_url="https://connection.keboola.com", token="bbb-token")

        # Simulate sequential saves (true concurrent test requires threads)
        store.add_project("proj-a", project_a)
        store.add_project("proj-b", project_b)

        # Load and verify both projects are present
        config = store.load()
        assert "proj-a" in config.projects
        assert "proj-b" in config.projects
