from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watchdog


class WatchdogConfigTests(unittest.TestCase):
    def write_config(self, watchdog_config: dict) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            json.dump({"watchdog": watchdog_config}, handle)
        return Path(handle.name)

    def test_zero_restarts_is_valid(self) -> None:
        path = self.write_config(
            {
                "heartbeat_file": "test.heartbeat",
                "heartbeat_interval_s": 10,
                "heartbeat_timeout_s": 30,
                "startup_grace_s": 30,
                "restart_delay_s": 0,
                "max_consecutive_restarts": 0,
                "stable_reset_s": 60,
            }
        )
        config = watchdog.load_watchdog_config(path)
        self.assertEqual(config.max_consecutive_restarts, 0)

    def test_stable_reset_must_exceed_heartbeat_timeout(self) -> None:
        path = self.write_config(
            {
                "heartbeat_file": "test.heartbeat",
                "heartbeat_interval_s": 10,
                "heartbeat_timeout_s": 30,
                "startup_grace_s": 30,
                "restart_delay_s": 0,
                "max_consecutive_restarts": 1,
                "stable_reset_s": 30,
            }
        )
        with self.assertRaisesRegex(watchdog.ConfigError, "stable_reset_s"):
            watchdog.load_watchdog_config(path)

    def test_watchdog_instance_lock_is_exclusive(self) -> None:
        lock_path = watchdog.SCRIPT_DIR / "test-watchdog.lock"
        lock_path.unlink(missing_ok=True)
        self.addCleanup(lambda: lock_path.unlink(missing_ok=True))
        first = watchdog.acquire_instance_lock(lock_path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(watchdog.acquire_instance_lock(lock_path))
        finally:
            watchdog.release_instance_lock(first)

    def test_terminate_process_tree_confirms_exit(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=watchdog.SCRIPT_DIR,
        )
        try:
            self.assertTrue(watchdog.terminate_process_tree(process))
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_missing_first_heartbeat_fails_after_grace_period(self) -> None:
        heartbeat = watchdog.SCRIPT_DIR / "missing-test.heartbeat"
        heartbeat.unlink(missing_ok=True)
        config = watchdog.WatchdogConfig(
            heartbeat_file=heartbeat,
            heartbeat_interval_s=1,
            heartbeat_timeout_s=2,
            startup_grace_s=3,
            restart_delay_s=0,
            max_consecutive_restarts=1,
            stable_reset_s=10,
        )
        with patch.object(watchdog.time, "monotonic", return_value=104):
            state, _ = watchdog.inspect_heartbeat(
                config, process_started_at=100, heartbeat_seen=False
            )
        self.assertIs(state, watchdog.HeartbeatState.FAILED)

    def test_stale_heartbeat_is_failed(self) -> None:
        heartbeat = watchdog.SCRIPT_DIR / "stale-test.heartbeat"
        heartbeat.write_text("test", encoding="ascii")
        self.addCleanup(lambda: heartbeat.unlink(missing_ok=True))
        config = watchdog.WatchdogConfig(
            heartbeat_file=heartbeat,
            heartbeat_interval_s=1,
            heartbeat_timeout_s=2,
            startup_grace_s=3,
            restart_delay_s=0,
            max_consecutive_restarts=1,
            stable_reset_s=10,
        )
        with patch.object(watchdog.time, "time", return_value=time.time() + 10):
            state, _ = watchdog.inspect_heartbeat(
                config, process_started_at=0, heartbeat_seen=True
            )
        self.assertIs(state, watchdog.HeartbeatState.FAILED)


if __name__ == "__main__":
    unittest.main()
