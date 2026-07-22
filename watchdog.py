"""Supervise the Telegram bot process and its heartbeat file."""

from __future__ import annotations

import json
import logging
import math
import msvcrt
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, BinaryIO

import psutil


SCRIPT_DIR = Path(__file__).resolve().parent
BOT_SCRIPT = SCRIPT_DIR / "main.py"
CONFIG_FILE = SCRIPT_DIR / "config.json"
INSTANCE_LOCK_FILE = SCRIPT_DIR / "bot.watchdog.lock"

DEFAULT_STABLE_RESET_S = 600.0
MAX_CHECK_INTERVAL_S = 5.0
TERMINATE_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 5.0
GRACEFUL_SHUTDOWN_TIMEOUT_S = 5.0


logger = logging.getLogger("watchdog")


def configure_logging() -> None:
    """Configure console logging only when the watchdog is executed."""
    logging.basicConfig(
        format="%(asctime)s - [WATCHDOG] - %(levelname)s - %(message)s",
        level=logging.INFO,
        force=True,
    )


class ConfigError(ValueError):
    """Raised when the watchdog configuration is missing or invalid."""


class ProcessCleanupError(RuntimeError):
    """Raised when an old Bot process cannot be confirmed stopped."""


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    heartbeat_file: Path
    heartbeat_interval_s: float
    heartbeat_timeout_s: float
    startup_grace_s: float
    restart_delay_s: float
    max_consecutive_restarts: int
    stable_reset_s: float

    @property
    def check_interval_s(self) -> float:
        """Poll often enough to keep timeout overshoot small."""
        shortest_window = min(self.heartbeat_timeout_s, self.startup_grace_s)
        return max(0.25, min(MAX_CHECK_INTERVAL_S, shortest_window / 4))


class HeartbeatState(Enum):
    WAITING = auto()
    HEALTHY = auto()
    FAILED = auto()


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是 JSON 对象。")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"配置文件包含重复字段: {key}。")
        result[key] = value
    return result


def _require_string(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"watchdog.{key} 必须是非空字符串。")
    return value.strip()


def _require_number(
    config: dict[str, Any],
    key: str,
    *,
    default: float | None = None,
    allow_zero: bool = False,
) -> float:
    if key not in config:
        if default is None:
            raise ConfigError(f"缺少必填配置 watchdog.{key}。")
        value: Any = default
    else:
        value = config[key]

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"watchdog.{key} 必须是数字。")

    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ConfigError(f"watchdog.{key} 不是可用的数字。") from exc
    if not math.isfinite(number):
        raise ConfigError(f"watchdog.{key} 必须是有限数字。")
    if not (number >= 0 if allow_zero else number > 0):
        qualifier = "大于等于 0" if allow_zero else "大于 0"
        raise ConfigError(f"watchdog.{key} 必须{qualifier}。")
    return number


def _require_nonnegative_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"watchdog.{key} 必须是大于等于 0 的整数。")
    return value


def load_watchdog_config(config_file: Path = CONFIG_FILE) -> WatchdogConfig:
    """Load and validate watchdog settings without silently hiding errors."""
    try:
        with config_file.open("r", encoding="utf-8") as handle:
            root = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件: {config_file}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是有效 JSON: {config_file} "
            f"(第 {exc.lineno} 行，第 {exc.colno} 列: {exc.msg})"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {config_file}: {exc}") from exc

    root_config = _require_object(root, "配置文件根节点")
    if "watchdog" not in root_config:
        raise ConfigError("缺少必填配置段 watchdog。")
    watchdog = _require_object(root_config["watchdog"], "watchdog")

    try:
        heartbeat_path = Path(_require_string(watchdog, "heartbeat_file")).expanduser()
        if not heartbeat_path.is_absolute():
            heartbeat_path = SCRIPT_DIR / heartbeat_path
        heartbeat_path = heartbeat_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"watchdog.heartbeat_file 路径无效: {exc}") from exc
    if not heartbeat_path.is_relative_to(SCRIPT_DIR):
        raise ConfigError(
            "watchdog.heartbeat_file 必须位于脚本目录内，"
            f"当前解析结果为: {heartbeat_path}"
        )
    if heartbeat_path.suffix.lower() != ".heartbeat":
        raise ConfigError("watchdog.heartbeat_file 必须使用 .heartbeat 后缀。")
    protected_paths = {
        BOT_SCRIPT.resolve(),
        CONFIG_FILE.resolve(),
        Path(__file__).resolve(),
    }
    if heartbeat_path in protected_paths:
        raise ConfigError(
            f"watchdog.heartbeat_file 不能指向程序或配置文件: {heartbeat_path}"
        )
    if not heartbeat_path.parent.is_dir():
        raise ConfigError(
            f"watchdog.heartbeat_file 的父目录不存在: {heartbeat_path.parent}"
        )
    if heartbeat_path.exists() and not heartbeat_path.is_file():
        raise ConfigError(
            f"watchdog.heartbeat_file 必须是普通文件路径: {heartbeat_path}"
        )
    if heartbeat_path.exists():
        try:
            heartbeat_value = float(
                heartbeat_path.read_text(encoding="ascii").strip()
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise ConfigError(
                "现有 watchdog.heartbeat_file 不是本程序生成的有效心跳。"
            ) from exc
        if not math.isfinite(heartbeat_value) or heartbeat_value <= 0:
            raise ConfigError(
                "现有 watchdog.heartbeat_file 不是本程序生成的有效心跳。"
            )

    heartbeat_interval_s = _require_number(watchdog, "heartbeat_interval_s")
    heartbeat_timeout_s = _require_number(watchdog, "heartbeat_timeout_s")
    if heartbeat_timeout_s <= heartbeat_interval_s:
        raise ConfigError(
            "watchdog.heartbeat_timeout_s 必须大于 heartbeat_interval_s，"
            "以容纳调度和文件系统延迟。"
        )

    startup_grace_s = _require_number(
        watchdog,
        "startup_grace_s",
        default=max(heartbeat_timeout_s, 60.0),
    )
    if startup_grace_s < heartbeat_interval_s:
        raise ConfigError(
            "watchdog.startup_grace_s 不能小于 heartbeat_interval_s。"
        )
    restart_delay_s = _require_number(watchdog, "restart_delay_s", allow_zero=True)
    stable_reset_s = _require_number(
        watchdog, "stable_reset_s", default=DEFAULT_STABLE_RESET_S
    )
    if stable_reset_s <= heartbeat_timeout_s:
        raise ConfigError(
            "watchdog.stable_reset_s 必须大于 heartbeat_timeout_s。"
        )

    return WatchdogConfig(
        heartbeat_file=heartbeat_path,
        heartbeat_interval_s=heartbeat_interval_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
        startup_grace_s=startup_grace_s,
        restart_delay_s=restart_delay_s,
        max_consecutive_restarts=_require_nonnegative_int(
            watchdog, "max_consecutive_restarts"
        ),
        stable_reset_s=stable_reset_s,
    )


def cleanup_heartbeat(heartbeat_file: Path) -> bool:
    """Remove a stale heartbeat, logging failures instead of hiding them."""
    try:
        heartbeat_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("无法清理心跳文件 %s: %s", heartbeat_file, exc)
        return False
    return True


def acquire_instance_lock(path: Path = INSTANCE_LOCK_FILE) -> BinaryIO | None:
    """Acquire the watchdog's Windows single-instance lock."""
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release_instance_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError as exc:
        logger.warning("释放看门狗单实例锁失败: %s", type(exc).__name__)
    finally:
        handle.close()


def inspect_heartbeat(
    config: WatchdogConfig,
    *,
    process_started_at: float,
    heartbeat_seen: bool,
) -> tuple[HeartbeatState, str]:
    """Return the current heartbeat state and a diagnostic message."""
    try:
        modified_at = config.heartbeat_file.stat().st_mtime
    except FileNotFoundError:
        if heartbeat_seen:
            return HeartbeatState.FAILED, "心跳文件在运行期间消失"

        startup_age = time.monotonic() - process_started_at
        if startup_age >= config.startup_grace_s:
            return (
                HeartbeatState.FAILED,
                f"启动 {startup_age:.1f}s 后仍未生成心跳文件 "
                f"(宽限期 {config.startup_grace_s:g}s)",
            )
        return HeartbeatState.WAITING, "等待首次心跳"
    except OSError as exc:
        return HeartbeatState.FAILED, f"无法读取心跳文件状态: {exc}"

    heartbeat_age = max(0.0, time.time() - modified_at)
    if heartbeat_age > config.heartbeat_timeout_s:
        return (
            HeartbeatState.FAILED,
            f"心跳已超时 {heartbeat_age:.1f}s "
            f"(阈值 {config.heartbeat_timeout_s:g}s)",
        )
    return HeartbeatState.HEALTHY, f"心跳正常，最后更新于 {heartbeat_age:.1f}s 前"


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
    """Terminate and reap a process tree, returning whether cleanup was complete."""
    pid = process.pid
    successful = True
    try:
        parent = psutil.Process(pid)
        # Descendants first prevents a live child from outlasting its parent.
        descendants = parent.children(recursive=True)
        targets = list(reversed(descendants)) + [parent]
    except psutil.NoSuchProcess:
        targets = []
    except (psutil.AccessDenied, OSError) as exc:
        logger.error("无法枚举进程树 PID %s: %s", pid, exc)
        targets = []
        successful = False

    if targets:
        logger.info(
            "正在终止机器人进程树: 主进程 PID %s，共 %s 个进程",
            pid,
            len(targets),
        )
        signalled, signal_ok = _signal_processes(targets, "terminate")
        successful = successful and signal_ok
        try:
            _, alive = psutil.wait_procs(signalled, timeout=TERMINATE_TIMEOUT_S)
        except (psutil.Error, OSError) as exc:
            logger.error("等待进程树正常退出时失败: %s", exc)
            alive = signalled
            successful = False
        if alive:
            logger.warning(
                "%s 个进程未及时退出，将强制终止: %s",
                len(alive),
                ", ".join(str(item.pid) for item in alive),
            )
            killed, kill_ok = _signal_processes(alive, "kill")
            successful = successful and kill_ok
            try:
                _, still_alive = psutil.wait_procs(killed, timeout=KILL_TIMEOUT_S)
            except (psutil.Error, OSError) as exc:
                logger.error("等待进程树强制退出时失败: %s", exc)
                still_alive = killed
                successful = False
            if still_alive:
                logger.error(
                    "以下进程在强制终止后仍未退出: %s",
                    ", ".join(str(item.pid) for item in still_alive),
                )
                successful = False

    elif process.poll() is None:
        # Fall back to the Popen handle if psutil could not inspect the tree.
        try:
            process.terminate()
        except OSError as exc:
            logger.error("无法终止主进程 PID %s: %s", pid, exc)
            successful = False

    # Reap the direct child even if psutil observed it exit already.
    try:
        process.wait(timeout=KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("主进程 PID %s 仍未退出，直接调用 kill()", pid)
        try:
            process.kill()
            process.wait(timeout=KILL_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("无法回收主进程 PID %s: %s", pid, exc)
            successful = False
    except OSError as exc:
        logger.error("等待主进程 PID %s 退出时失败: %s", pid, exc)
        successful = False

    return successful and process.poll() is not None


def _monitor_process(
    process: subprocess.Popen[Any],
    config: WatchdogConfig,
    consecutive_restarts: int,
) -> tuple[int | None, int]:
    """Monitor one child run and return (exit_code, updated restart count)."""
    process_started_at = time.monotonic()
    heartbeat_seen = False
    failure_reason: str | None = None
    restart_counter_reset = False

    while True:
        return_code = process.poll()
        if return_code is not None:
            if return_code == 0:
                logger.info("机器人正常退出。")
                return 0, consecutive_restarts
            if return_code in (2, 3):
                logger.critical(
                    "机器人以不可重试的退出码 %s 结束，看门狗停止。",
                    return_code,
                )
                return return_code, consecutive_restarts
            failure_reason = f"机器人异常退出，退出码 {return_code}"
            break

        state, detail = inspect_heartbeat(
            config,
            process_started_at=process_started_at,
            heartbeat_seen=heartbeat_seen,
        )
        if state is HeartbeatState.FAILED:
            failure_reason = detail
            if not terminate_process_tree(process):
                raise ProcessCleanupError(
                    f"检测到 {failure_reason}，但无法确认旧进程已完全退出"
                )
            break
        if state is HeartbeatState.HEALTHY:
            heartbeat_seen = True

        uptime = time.monotonic() - process_started_at
        if (
            heartbeat_seen
            and not restart_counter_reset
            and uptime >= config.stable_reset_s
        ):
            if consecutive_restarts:
                logger.info(
                    "机器人已稳定运行 %.1fs，连续重启计数从 %s 重置为 0。",
                    uptime,
                    consecutive_restarts,
                )
                consecutive_restarts = 0
            restart_counter_reset = True

        time.sleep(config.check_interval_s)

    logger.error("本次运行失败: %s", failure_reason)
    return None, consecutive_restarts


def run_watchdog(config: WatchdogConfig) -> int:
    """Run the supervision loop and return a process-style exit code."""
    if not BOT_SCRIPT.is_file():
        logger.critical("找不到机器人脚本: %s", BOT_SCRIPT)
        return 2

    logger.info("看门狗启动，脚本目录: %s", SCRIPT_DIR)
    logger.info("监控脚本: %s", BOT_SCRIPT)
    logger.info("心跳文件: %s", config.heartbeat_file)

    consecutive_restarts = 0
    process: subprocess.Popen[Any] | None = None

    try:
        while True:
            if not cleanup_heartbeat(config.heartbeat_file):
                logger.critical("无法建立可靠的心跳监控，机器人不会启动。")
                return 1
            logger.info(
                "正在启动机器人（连续重启计数: %s/%s）...",
                consecutive_restarts,
                config.max_consecutive_restarts,
            )

            try:
                process = subprocess.Popen(
                    [sys.executable, str(BOT_SCRIPT)],
                    cwd=SCRIPT_DIR,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.exception("无法启动机器人进程: %s", exc)
                process = None
            else:
                try:
                    exit_code, consecutive_restarts = _monitor_process(
                        process, config, consecutive_restarts
                    )
                except KeyboardInterrupt:
                    raise
                except ProcessCleanupError as exc:
                    logger.critical("%s。为避免重复 Bot，看门狗停止。", exc)
                    return 1
                except Exception as exc:
                    logger.exception("监控循环发生未预期异常: %s", exc)
                    if process.poll() is None and not terminate_process_tree(process):
                        logger.critical("旧进程未完全退出，看门狗停止。")
                        return 1
                    exit_code = None

                if exit_code in (0, 2, 3):
                    return exit_code

            if consecutive_restarts >= config.max_consecutive_restarts:
                logger.critical(
                    "已达到最大连续重启次数 %s，看门狗停止。",
                    config.max_consecutive_restarts,
                )
                return 1

            consecutive_restarts += 1
            logger.info(
                "将在 %gs 后执行第 %s 次连续重启。",
                config.restart_delay_s,
                consecutive_restarts,
            )
            time.sleep(config.restart_delay_s)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，等待机器人优雅退出...")
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=GRACEFUL_SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.warning("机器人未在宽限期内退出，开始终止进程树。")
                if not terminate_process_tree(process):
                    logger.critical("无法确认机器人进程已退出。")
                    return 1
            except OSError as exc:
                logger.error("等待机器人优雅退出时失败: %s", exc)
                if not terminate_process_tree(process):
                    return 1
        return 130
    finally:
        cleanup_heartbeat(config.heartbeat_file)


def main() -> int:
    configure_logging()
    try:
        instance_lock = acquire_instance_lock()
    except OSError as exc:
        logger.critical("无法创建看门狗单实例锁: %s", exc)
        return 1
    if instance_lock is None:
        logger.critical("检测到另一个看门狗实例，本进程不会启动。")
        return 3

    try:
        try:
            config = load_watchdog_config()
        except ConfigError as exc:
            logger.critical("看门狗配置无效: %s", exc)
            return 2
        except KeyboardInterrupt:
            logger.info("配置加载期间收到 Ctrl+C。")
            return 130
        return run_watchdog(config)
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    sys.exit(main())
