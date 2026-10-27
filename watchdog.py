"""Restart the bot after a crash, with a bounded retry budget."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from main import (
    ConfigError,
    WatchdogSettings,
    acquire_instance_lock,
    load_config,
    release_instance_lock,
)


SCRIPT_DIR = Path(__file__).resolve().parent
BOT_SCRIPT = SCRIPT_DIR / "main.py"
CONFIG_FILE = SCRIPT_DIR / "config.json"
INSTANCE_LOCK_FILE = SCRIPT_DIR / "bot.watchdog.lock"
CHECK_INTERVAL_S = 1.0
TERMINATE_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 5.0
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5.0
# Windows may report Ctrl+C as a signed or unsigned process exit status.
STOP_EXIT_CODES = {0, 2, 3, 130, 0xC000013A, -1073741510}

logger = logging.getLogger("watchdog")


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        force=True,
    )


def load_watchdog_config(config_file: Path = CONFIG_FILE) -> WatchdogSettings:
    return load_config(config_file).watchdog


def _signal_processes(
    processes: list[psutil.Process], action: str
) -> tuple[list[psutil.Process], bool]:
    signalled: list[psutil.Process] = []
    successful = True
    for process in processes:
        try:
            getattr(process, action)()
            signalled.append(process)
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as exc:
            logger.error("无法%s进程 PID %s: %s", action, process.pid, exc)
            successful = False
    return signalled, successful


def terminate_process_tree(process: subprocess.Popen[Any]) -> bool:
    """Also stop Windows venv launcher children before releasing the instance lock."""
    successful = True
    try:
        parent = psutil.Process(process.pid)
        targets = list(reversed(parent.children(recursive=True))) + [parent]
    except psutil.NoSuchProcess:
        targets = []
    except (psutil.AccessDenied, OSError) as exc:
        logger.error("无法枚举进程树 PID %s: %s", process.pid, exc)
        targets = []
        successful = False

    if targets:
        signalled, signal_ok = _signal_processes(targets, "terminate")
        successful = successful and signal_ok
        try:
            _, alive = psutil.wait_procs(signalled, timeout=TERMINATE_TIMEOUT_S)
        except (psutil.Error, OSError) as exc:
            logger.error("等待进程退出时失败: %s", exc)
            alive = signalled
            successful = False
        if alive:
            killed, kill_ok = _signal_processes(alive, "kill")
            successful = successful and kill_ok
            try:
                _, still_alive = psutil.wait_procs(killed, timeout=KILL_TIMEOUT_S)
            except (psutil.Error, OSError) as exc:
                logger.error("等待进程强制退出时失败: %s", exc)
                still_alive = killed
                successful = False
            if still_alive:
                logger.error("仍有 %s 个子进程未退出。", len(still_alive))
                successful = False
    elif process.poll() is None:
        try:
            process.terminate()
        except OSError as exc:
            logger.error("无法终止主进程 PID %s: %s", process.pid, exc)
            successful = False

    try:
        process.wait(timeout=KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=KILL_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("无法回收主进程 PID %s: %s", process.pid, exc)
            successful = False
    except OSError as exc:
        logger.error("等待主进程退出时失败: %s", exc)
        successful = False
    return successful and process.poll() is not None


def _monitor_process(
    process: subprocess.Popen[Any],
    config: WatchdogSettings,
    consecutive_restarts: int,
) -> tuple[int, int]:
    started_at = time.monotonic()
    while True:
        return_code = process.poll()
        if time.monotonic() - started_at >= config.stable_reset_s:
            consecutive_restarts = 0
        if return_code is not None:
            return return_code, consecutive_restarts
        time.sleep(CHECK_INTERVAL_S)


def run_watchdog(config: WatchdogSettings) -> int:
    if not BOT_SCRIPT.is_file():
        logger.error("找不到机器人脚本: %s", BOT_SCRIPT)
        return 2

    logger.info("启动 Bot，异常退出后最多连续重启 %s 次。", config.max_consecutive_restarts)
    consecutive_restarts = 0
    process: subprocess.Popen[Any] | None = None
    try:
        while True:
            try:
                process = subprocess.Popen(
                    [sys.executable, str(BOT_SCRIPT)], cwd=SCRIPT_DIR
                )
            except OSError as exc:
                logger.error("无法启动机器人进程: %s", exc)
                process = None
            else:
                exit_code, consecutive_restarts = _monitor_process(
                    process, config, consecutive_restarts
                )
                if exit_code in STOP_EXIT_CODES:
                    if exit_code in (2, 3):
                        logger.error("Bot 已停止，请解决配置错误或重复实例问题后重新启动。")
                        return exit_code
                    logger.info("Bot 已停止。")
                    return 0
                logger.warning("Bot 异常退出（退出码 %s）。", exit_code)

            if consecutive_restarts >= config.max_consecutive_restarts:
                logger.error("已达到连续重启上限，请检查前面的错误后重新启动。")
                return 1
            consecutive_restarts += 1
            logger.info(
                "%g 秒后重启（%s/%s）。",
                config.restart_delay_s,
                consecutive_restarts,
                config.max_consecutive_restarts,
            )
            time.sleep(config.restart_delay_s)
    except KeyboardInterrupt:
        logger.info("正在停止 Bot。")
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=GRACEFUL_SHUTDOWN_TIMEOUT_S)
            except (subprocess.TimeoutExpired, OSError):
                if not terminate_process_tree(process):
                    logger.error("无法确认 Bot 已退出。")
                    return 1
        return 130
    finally:
        # Do not leave a live bot behind if the supervisor itself fails.
        if process is not None and process.poll() is None:
            terminate_process_tree(process)


def main() -> int:
    configure_logging()
    try:
        instance_lock = acquire_instance_lock(INSTANCE_LOCK_FILE)
    except OSError as exc:
        logger.error("无法创建启动器单实例锁: %s", exc)
        return 1
    if instance_lock is None:
        logger.error("Bot 启动器已经在运行。")
        return 3

    try:
        try:
            config = load_watchdog_config()
        except ConfigError as exc:
            logger.error("配置错误: %s", exc)
            return 2
        return run_watchdog(config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.error("启动器异常退出（%s）。", type(exc).__name__)
        return 1
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    sys.exit(main())
