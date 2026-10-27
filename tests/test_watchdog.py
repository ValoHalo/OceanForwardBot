from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import watchdog


class WatchdogTests(unittest.TestCase):
    def config(self, *, restarts: int = 2) -> watchdog.WatchdogSettings:
        return watchdog.WatchdogSettings(
            restart_delay_s=0, max_consecutive_restarts=restarts, stable_reset_s=60
        )

    def write_config(self, settings: dict) -> Path:
        payload = json.loads(
            (watchdog.SCRIPT_DIR / "example.config.json").read_text(encoding="utf-8")
        )
        payload["bot"]["token"] = "123456:TEST_TOKEN_VALUE"
        payload["watchdog"] = settings
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            json.dump(payload, handle)
        return Path(handle.name)

    def test_zero_restarts_is_valid_without_heartbeat(self) -> None:
        config = watchdog.load_watchdog_config(
            self.write_config({"max_consecutive_restarts": 0})
        )
        self.assertEqual(config.max_consecutive_restarts, 0)

    def test_restart_settings_reject_invalid_values(self) -> None:
        for key, value in (
            ("max_consecutive_restarts", -1),
            ("max_consecutive_restarts", True),
            ("restart_delay_s", -1),
            ("stable_reset_s", 0),
            ("stable_reset_s", float("inf")),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(watchdog.ConfigError):
                    watchdog.load_watchdog_config(self.write_config({key: value}))

    def test_legacy_heartbeat_file_is_not_read_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "old.heartbeat"
            heartbeat.write_text("old data", encoding="ascii")
            config = watchdog.load_watchdog_config(self.write_config({
                "heartbeat_file": str(heartbeat), "heartbeat_timeout_s": 0,
                "startup_grace_s": 0,
            }))
            process = Mock()
            process.poll.return_value = 0
            with patch.object(watchdog.subprocess, "Popen", return_value=process):
                self.assertEqual(watchdog.run_watchdog(config), 0)
            self.assertEqual(heartbeat.read_text(encoding="ascii"), "old data")

    def test_watchdog_instance_lock_is_exclusive(self) -> None:
        lock_path = watchdog.SCRIPT_DIR / "test-watchdog.lock"
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

    def test_running_process_is_never_killed_for_elapsed_time(self) -> None:
        process = Mock()
        process.poll.side_effect = [None, None, 1]
        with patch.object(watchdog.time, "monotonic", side_effect=[0, 10, 3600, 3601]):
            with patch.object(watchdog.time, "sleep"):
                code, restarts = watchdog._monitor_process(process, self.config(), 2)
        self.assertEqual((code, restarts), (1, 0))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_short_run_preserves_restart_count(self) -> None:
        process = Mock()
        process.poll.return_value = 1
        with patch.object(watchdog.time, "monotonic", side_effect=[0, 10]):
            self.assertEqual(watchdog._monitor_process(process, self.config(), 2), (1, 2))

    def test_crashes_stop_at_restart_limit(self) -> None:
        for restarts in (0, 2):
            with self.subTest(restarts=restarts):
                process = Mock()
                process.poll.return_value = 1
                with patch.object(watchdog.subprocess, "Popen", return_value=process) as spawn:
                    with patch.object(watchdog.time, "sleep"):
                        self.assertEqual(watchdog.run_watchdog(self.config(restarts=restarts)), 1)
                self.assertEqual(spawn.call_count, restarts + 1)

    def test_crash_restarts_then_normal_exit_stops(self) -> None:
        failed, stopped = Mock(), Mock()
        failed.poll.return_value = 1
        stopped.poll.return_value = 0
        with patch.object(watchdog.subprocess, "Popen", side_effect=[failed, stopped]) as spawn:
            with patch.object(watchdog.time, "sleep"):
                self.assertEqual(watchdog.run_watchdog(self.config()), 0)
        self.assertEqual(spawn.call_count, 2)

    def test_normal_config_duplicate_and_interrupt_exits_do_not_restart(self) -> None:
        for code in watchdog.STOP_EXIT_CODES:
            with self.subTest(code=code):
                process = Mock()
                process.poll.return_value = code
                with patch.object(watchdog.subprocess, "Popen", return_value=process) as spawn:
                    result = watchdog.run_watchdog(self.config())
                self.assertEqual(result, code if code in (2, 3) else 0)
                spawn.assert_called_once()

    def test_launch_failures_are_bounded(self) -> None:
        with patch.object(watchdog.subprocess, "Popen", side_effect=OSError("failed")) as spawn:
            with patch.object(watchdog.time, "sleep"):
                self.assertEqual(watchdog.run_watchdog(self.config()), 1)
        self.assertEqual(spawn.call_count, 3)

    def test_ctrl_c_waits_for_child_without_restart(self) -> None:
        process = Mock()
        process.poll.side_effect = [None, 0]
        with patch.object(watchdog.subprocess, "Popen", return_value=process) as spawn:
            with patch.object(watchdog, "_monitor_process", side_effect=KeyboardInterrupt):
                self.assertEqual(watchdog.run_watchdog(self.config()), 130)
        spawn.assert_called_once()
        process.wait.assert_called_once_with(timeout=watchdog.GRACEFUL_SHUTDOWN_TIMEOUT_S)

    def test_supervisor_error_cleans_up_live_child(self) -> None:
        process = Mock()
        process.poll.return_value = None
        with patch.object(watchdog.subprocess, "Popen", return_value=process):
            with patch.object(watchdog, "_monitor_process", side_effect=RuntimeError):
                with patch.object(watchdog, "terminate_process_tree", return_value=True) as cleanup:
                    with self.assertRaises(RuntimeError):
                        watchdog.run_watchdog(self.config())
        cleanup.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
