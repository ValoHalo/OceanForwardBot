from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import msvcrt
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from urllib.parse import urlsplit

# Opt in to PTB's v23 time-delta behavior while remaining compatible with v22.
os.environ.setdefault("PTB_TIMEDELTA", "true")

import telegram
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = BASE_DIR / "config.json"
INSTANCE_LOCK_FILE = BASE_DIR / "bot.main.lock"
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ConfigError(ValueError):
    """Raised when config.json does not satisfy the runtime contract."""


class PartialCopyError(RuntimeError):
    """Raised when Telegram silently skips one or more batch-copy items."""


@dataclass(frozen=True, slots=True)
class BotSettings:
    token: str
    owner_id: int
    proxy_url: str | None
    silent_forwarding: bool
    log_level: str


@dataclass(frozen=True, slots=True)
class WatchdogSettings:
    heartbeat_file: Path
    heartbeat_interval_s: float
    heartbeat_timeout_s: float
    restart_delay_s: float
    max_consecutive_restarts: int
    startup_grace_s: float
    stable_reset_s: float


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    connection_pool_size: int
    read_timeout: float
    connect_timeout: float
    pool_timeout: float
    write_timeout: float
    media_write_timeout: float
    max_concurrent_sends: int
    max_retries: int
    retry_base_delay_s: float
    media_group_wait_s: float


@dataclass(frozen=True, slots=True)
class Destination:
    chat_id: int | str
    topic_ids: tuple[int, ...]
    silent_forwarding: bool | None


@dataclass(frozen=True, slots=True)
class SendTarget:
    chat_id: int | str
    thread_id: int | None
    silent: bool


@dataclass(slots=True)
class MediaGroupBuffer:
    messages: list[telegram.Message]
    deadline: float
    changed: asyncio.Event
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    bot: BotSettings
    watchdog: WatchdogSettings
    network: NetworkSettings
    destinations: tuple[Destination, ...]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' 必须是 JSON 对象。")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"配置文件包含重复字段: '{key}'。")
        result[key] = value
    return result


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"'{name}' 必须是 true 或 false。")
    return value


def _int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{name}' 必须是整数。")
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{name}' 必须大于等于 {minimum}。")
    return value


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{name}' 必须是数字。")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"'{name}' 必须是有限数字。")
    if result < minimum:
        raise ConfigError(f"'{name}' 必须大于等于 {minimum:g}。")
    return result


def _positive_number(value: Any, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise ConfigError(f"'{name}' 必须大于 0。")
    return result


def _heartbeat_path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("'watchdog.heartbeat_file' 必须是非空路径。")

    raw_path = Path(value.strip())
    resolved = (raw_path if raw_path.is_absolute() else BASE_DIR / raw_path).resolve()
    try:
        resolved.relative_to(BASE_DIR)
    except ValueError as exc:
        raise ConfigError("'watchdog.heartbeat_file' 必须位于项目目录内。") from exc
    if resolved == BASE_DIR or not resolved.parent.is_dir():
        raise ConfigError("'watchdog.heartbeat_file' 的父目录必须已经存在。")
    if resolved.suffix.lower() != ".heartbeat":
        raise ConfigError("'watchdog.heartbeat_file' 必须使用 .heartbeat 后缀。")
    protected_paths = {
        DEFAULT_CONFIG_FILE.resolve(),
        (BASE_DIR / "main.py").resolve(),
        (BASE_DIR / "watchdog.py").resolve(),
    }
    if resolved in protected_paths:
        raise ConfigError("'watchdog.heartbeat_file' 不能指向程序或配置文件。")
    if resolved.exists() and not resolved.is_file():
        raise ConfigError("'watchdog.heartbeat_file' 必须是普通文件路径。")
    if resolved.exists():
        try:
            heartbeat_value = float(resolved.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError) as exc:
            raise ConfigError("现有 heartbeat_file 不是本程序生成的有效心跳。") from exc
        if not math.isfinite(heartbeat_value) or heartbeat_value <= 0:
            raise ConfigError("现有 heartbeat_file 不是本程序生成的有效心跳。")
    return resolved


def _parse_destination(value: Any, index: int) -> Destination:
    name = f"destinations[{index}]"
    item = _mapping(value, name)

    chat_id = item.get("chat_id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
        raise ConfigError(f"'{name}.chat_id' 必须是非零整数或非空字符串。")
    if isinstance(chat_id, int) and chat_id == 0:
        raise ConfigError(f"'{name}.chat_id' 不能为 0。")
    if isinstance(chat_id, str):
        chat_id = chat_id.strip()
        if not chat_id:
            raise ConfigError(f"'{name}.chat_id' 不能为空。")

    topic_values = item.get("topic_ids", [])
    if not isinstance(topic_values, list):
        raise ConfigError(f"'{name}.topic_ids' 必须是整数数组。")
    topic_ids: list[int] = []
    seen_topics: set[int] = set()
    for topic_index, topic_value in enumerate(topic_values):
        topic_id = _int(topic_value, f"{name}.topic_ids[{topic_index}]", minimum=1)
        if topic_id not in seen_topics:
            seen_topics.add(topic_id)
            topic_ids.append(topic_id)

    silent: bool | None = None
    if "silent_forwarding" in item:
        silent = _bool(item["silent_forwarding"], f"{name}.silent_forwarding")

    return Destination(chat_id=chat_id, topic_ids=tuple(topic_ids), silent_forwarding=silent)


def load_config(config_file: Path | str = DEFAULT_CONFIG_FILE) -> AppConfig:
    """Load and strictly validate the complete application configuration."""
    path = Path(config_file)
    if not path.is_absolute():
        path = BASE_DIR / path
    path = path.resolve()

    if not path.is_file():
        raise ConfigError(f"找不到配置文件: {path}")
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）。"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取配置文件: {exc}") from exc

    root = _mapping(raw, "config")
    bot_raw = _mapping(root.get("bot"), "bot")
    watchdog_raw = _mapping(root.get("watchdog"), "watchdog")
    network_raw = _mapping(root.get("network", {}), "network")

    token = bot_raw.get("token")
    if (
        not isinstance(token, str)
        or not token.strip()
        or ":" not in token
        or "YOUR_BOT_TOKEN" in token.upper()
    ):
        raise ConfigError("'bot.token' 缺失、仍是占位值或格式明显无效。")
    token = token.strip()

    owner_id = _int(bot_raw.get("owner_id"), "bot.owner_id", minimum=1)
    silent_forwarding = _bool(
        bot_raw.get("silent_forwarding", False), "bot.silent_forwarding"
    )
    log_level = bot_raw.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in LOG_LEVELS:
        raise ConfigError(f"'bot.log_level' 必须是 {', '.join(sorted(LOG_LEVELS))} 之一。")
    log_level = log_level.upper()

    proxy_value = bot_raw.get("proxy_url", "")
    if proxy_value is None:
        proxy_url = None
    elif isinstance(proxy_value, str):
        proxy_url = proxy_value.strip() or None
    else:
        raise ConfigError("'bot.proxy_url' 必须是字符串。")
    if proxy_url:
        try:
            parsed_proxy = urlsplit(proxy_url)
            parsed_proxy.port
        except ValueError as exc:
            raise ConfigError("'bot.proxy_url' 格式或端口无效。") from exc
        if (
            parsed_proxy.scheme.lower() not in SUPPORTED_PROXY_SCHEMES
            or not parsed_proxy.hostname
            or parsed_proxy.query
            or parsed_proxy.fragment
        ):
            raise ConfigError(
                "'bot.proxy_url' 必须是带主机名且不含 query/fragment 的 "
                "http、https、socks5 或 socks5h URL。"
            )

    heartbeat_file = _heartbeat_path(watchdog_raw.get("heartbeat_file"))
    heartbeat_interval = _positive_number(
        watchdog_raw.get("heartbeat_interval_s"), "watchdog.heartbeat_interval_s"
    )
    heartbeat_timeout = _positive_number(
        watchdog_raw.get("heartbeat_timeout_s"), "watchdog.heartbeat_timeout_s"
    )
    if heartbeat_timeout <= heartbeat_interval:
        raise ConfigError(
            "'watchdog.heartbeat_timeout_s' 必须大于 heartbeat_interval_s。"
        )
    restart_delay = _number(
        watchdog_raw.get("restart_delay_s"), "watchdog.restart_delay_s"
    )
    max_restarts = _int(
        watchdog_raw.get("max_consecutive_restarts"),
        "watchdog.max_consecutive_restarts",
        minimum=0,
    )
    startup_grace = _positive_number(
        watchdog_raw.get("startup_grace_s", max(heartbeat_timeout, 60)),
        "watchdog.startup_grace_s",
    )
    if startup_grace < heartbeat_interval:
        raise ConfigError(
            "'watchdog.startup_grace_s' 不能小于 heartbeat_interval_s。"
        )
    stable_reset = _positive_number(
        watchdog_raw.get("stable_reset_s", 600), "watchdog.stable_reset_s"
    )
    if stable_reset <= heartbeat_timeout:
        raise ConfigError(
            "'watchdog.stable_reset_s' 必须大于 heartbeat_timeout_s。"
        )

    connection_pool_size = _int(
        network_raw.get("connection_pool_size", 32),
        "network.connection_pool_size",
        minimum=1,
    )
    max_concurrent_sends = _int(
        network_raw.get("max_concurrent_sends", 4),
        "network.max_concurrent_sends",
        minimum=1,
    )
    if max_concurrent_sends > connection_pool_size:
        raise ConfigError(
            "'network.max_concurrent_sends' 不能大于 connection_pool_size。"
        )

    network = NetworkSettings(
        connection_pool_size=connection_pool_size,
        read_timeout=_positive_number(
            network_raw.get("read_timeout", 20.0), "network.read_timeout"
        ),
        connect_timeout=_positive_number(
            network_raw.get("connect_timeout", 10.0), "network.connect_timeout"
        ),
        pool_timeout=_positive_number(
            network_raw.get("pool_timeout", 10.0), "network.pool_timeout"
        ),
        write_timeout=_positive_number(
            network_raw.get("write_timeout", 20.0), "network.write_timeout"
        ),
        media_write_timeout=_positive_number(
            network_raw.get("media_write_timeout", 60.0),
            "network.media_write_timeout",
        ),
        max_concurrent_sends=max_concurrent_sends,
        max_retries=_int(
            network_raw.get("max_retries", 3), "network.max_retries", minimum=0
        ),
        retry_base_delay_s=_positive_number(
            network_raw.get("retry_base_delay_s", 1.0),
            "network.retry_base_delay_s",
        ),
        media_group_wait_s=_positive_number(
            network_raw.get("media_group_wait_s", 2.0),
            "network.media_group_wait_s",
        ),
    )

    destination_values = root.get("destinations")
    if not isinstance(destination_values, list) or not destination_values:
        raise ConfigError("'destinations' 必须是非空数组。")
    destinations = tuple(
        _parse_destination(value, index)
        for index, value in enumerate(destination_values)
    )
    target_silence: dict[tuple[int | str, int | None], bool] = {}
    for destination in destinations:
        effective_silent = (
            silent_forwarding
            if destination.silent_forwarding is None
            else destination.silent_forwarding
        )
        for thread_id in destination.topic_ids or (None,):
            key = (destination.chat_id, thread_id)
            previous = target_silence.get(key)
            if previous is not None and previous != effective_silent:
                raise ConfigError(
                    "重复 destination/topic 的 silent_forwarding 设置相互冲突。"
                )
            target_silence[key] = effective_silent

    return AppConfig(
        bot=BotSettings(
            token=token,
            owner_id=owner_id,
            proxy_url=proxy_url,
            silent_forwarding=silent_forwarding,
            log_level=log_level,
        ),
        watchdog=WatchdogSettings(
            heartbeat_file=heartbeat_file,
            heartbeat_interval_s=heartbeat_interval,
            heartbeat_timeout_s=heartbeat_timeout,
            restart_delay_s=restart_delay,
            max_consecutive_restarts=max_restarts,
            startup_grace_s=startup_grace,
            stable_reset_s=stable_reset,
        ),
        network=network,
        destinations=destinations,
    )


def redact_sensitive_text(text: str, config: AppConfig) -> str:
    text = text.replace(config.bot.token, "<redacted-token>")
    if config.bot.proxy_url:
        text = text.replace(config.bot.proxy_url, "<redacted-proxy>")
    return re.sub(
        r"(?i)(https?://|socks5h?://)[^/@\s]+:[^/@\s]+@",
        r"\1***@",
        text,
    )


class RedactingFormatter(logging.Formatter):
    def __init__(self, config: AppConfig):
        super().__init__("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        self.config = config

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record), self.config)


def configure_logging(config: AppConfig) -> None:
    level = getattr(logging, config.bot.log_level)
    logging.basicConfig(
        level=level,
        force=True,
    )
    formatter = RedactingFormatter(config)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)

    # PTB records can contain the token, complete updates, or private message text.
    logging.getLogger("telegram").setLevel(logging.CRITICAL + 1)
    logging.getLogger("httpx").setLevel(logging.CRITICAL + 1)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL + 1)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def create_request(config: AppConfig, *, for_updates: bool) -> HTTPXRequest:
    network = config.network
    params: dict[str, Any] = {
        "connection_pool_size": 1 if for_updates else network.connection_pool_size,
        "read_timeout": network.read_timeout,
        "connect_timeout": network.connect_timeout,
        "pool_timeout": network.pool_timeout,
        "write_timeout": network.write_timeout,
    }
    if not for_updates:
        params["media_write_timeout"] = network.media_write_timeout
    if config.bot.proxy_url:
        params["proxy"] = config.bot.proxy_url
    return HTTPXRequest(**params)


def safe_error_text(error: BaseException, config: AppConfig) -> str:
    """Format an exception without exposing the token or proxy credentials."""
    text = redact_sensitive_text(str(error), config)
    return f"{type(error).__name__}: {text}"


def acquire_instance_lock(path: Path = INSTANCE_LOCK_FILE) -> BinaryIO | None:
    """Acquire a Windows byte-range lock that is released automatically on crash."""
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
        logger.warning("释放 Bot 单实例锁失败: %s", type(exc).__name__)
    finally:
        handle.close()


class ForwardBotService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.targets = self._expand_targets(config)
        self.send_semaphore = asyncio.Semaphore(config.network.max_concurrent_sends)
        self.forward_lock = asyncio.Lock()
        self.media_groups: dict[str, MediaGroupBuffer] = {}

    @staticmethod
    def _expand_targets(config: AppConfig) -> tuple[SendTarget, ...]:
        targets: list[SendTarget] = []
        seen: set[tuple[int | str, int | None]] = set()
        for destination in config.destinations:
            silent = (
                config.bot.silent_forwarding
                if destination.silent_forwarding is None
                else destination.silent_forwarding
            )
            threads = destination.topic_ids or (None,)
            for thread_id in threads:
                key = (destination.chat_id, thread_id)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(
                    SendTarget(
                        chat_id=destination.chat_id,
                        thread_id=thread_id,
                        silent=silent,
                    )
                )
        return tuple(targets)

    @staticmethod
    def _target_label(target: SendTarget) -> str:
        if target.thread_id is None:
            return str(target.chat_id)
        return f"{target.chat_id} (Topic {target.thread_id})"

    @staticmethod
    def _retry_after_seconds(error: RetryAfter) -> float:
        value = error.retry_after
        if isinstance(value, timedelta):
            return max(value.total_seconds(), 0.0)
        return max(float(value), 0.0)

    async def _send_with_retry(
        self,
        target: SendTarget,
        action: Callable[[], Awaitable[T]],
    ) -> bool:
        attempts = self.config.network.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                async with self.send_semaphore:
                    await action()
                return True
            except RetryAfter as exc:
                delay = self._retry_after_seconds(exc) + 0.25
                error: Exception = exc
            except (BadRequest, Forbidden) as exc:
                logger.error(
                    "转发到 %s 失败且不可重试: %s",
                    self._target_label(target),
                    safe_error_text(exc, self.config),
                )
                return False
            except PartialCopyError as exc:
                logger.error(
                    "批量复制到 %s 仅部分成功且不会重试: %s",
                    self._target_label(target),
                    safe_error_text(exc, self.config),
                )
                return False
            except TimedOut as exc:
                delay = min(
                    self.config.network.retry_base_delay_s * (2 ** (attempt - 1)),
                    30.0,
                )
                error = exc
            except NetworkError as exc:
                delay = min(
                    self.config.network.retry_base_delay_s * (2 ** (attempt - 1)),
                    30.0,
                )
                error = exc
            except TelegramError as exc:
                logger.error(
                    "转发到 %s 失败且不可重试: %s",
                    self._target_label(target),
                    safe_error_text(exc, self.config),
                )
                return False
            except Exception as exc:
                logger.error(
                    "转发到 %s 时发生意外错误: %s",
                    self._target_label(target),
                    safe_error_text(exc, self.config),
                )
                return False

            if attempt >= attempts:
                logger.error(
                    "转发到 %s 在 %s 次尝试后失败: %s",
                    self._target_label(target),
                    attempt,
                    safe_error_text(error, self.config),
                )
                return False
            logger.warning(
                "转发到 %s 失败，将在 %.2f 秒后重试（%s/%s）: %s",
                self._target_label(target),
                delay,
                attempt,
                attempts - 1,
                safe_error_text(error, self.config),
            )
            await asyncio.sleep(delay)
        return False

    async def _fan_out(
        self,
        action_factory: Callable[[SendTarget], Callable[[], Awaitable[Any]]],
        description: str,
    ) -> None:
        results = await asyncio.gather(
            *(
                self._send_with_retry(target, action_factory(target))
                for target in self.targets
            )
        )
        failures = results.count(False)
        if failures:
            logger.error("%s：%s/%s 个目标失败。", description, failures, len(results))
        else:
            logger.info("%s：已发送到 %s 个目标。", description, len(results))

    async def _forward_message_unlocked(self, message: telegram.Message) -> None:
        def action_factory(target: SendTarget) -> Callable[[], Awaitable[Any]]:
            async def action() -> Any:
                return await message.copy(
                    chat_id=target.chat_id,
                    message_thread_id=target.thread_id,
                    disable_notification=target.silent,
                )

            return action

        await self._fan_out(action_factory, f"消息 {message.message_id}")

    async def forward_message(self, message: telegram.Message) -> None:
        async with self.forward_lock:
            await self._forward_message_unlocked(message)

    async def _forward_media_group_unlocked(
        self,
        bot: telegram.Bot,
        messages: list[telegram.Message],
    ) -> None:
        messages.sort(key=lambda message: message.message_id)
        source_chat_id = messages[0].chat_id
        message_ids = [message.message_id for message in messages]

        def action_factory(target: SendTarget) -> Callable[[], Awaitable[Any]]:
            async def action() -> Any:
                copied = await bot.copy_messages(
                    chat_id=target.chat_id,
                    from_chat_id=source_chat_id,
                    message_ids=message_ids,
                    message_thread_id=target.thread_id,
                    disable_notification=target.silent,
                )
                if len(copied) != len(message_ids):
                    raise PartialCopyError(
                        f"请求 {len(message_ids)} 条，Telegram 仅复制 {len(copied)} 条"
                    )
                return copied

            return action

        await self._fan_out(action_factory, f"相册（{len(message_ids)} 项）")

    async def forward_media_group(
        self,
        bot: telegram.Bot,
        messages: list[telegram.Message],
    ) -> None:
        async with self.forward_lock:
            await self._forward_media_group_unlocked(bot, messages)

    async def _flush_all_media_groups_unlocked(self, bot: telegram.Bot) -> None:
        for media_group_id in list(self.media_groups):
            buffer = self.media_groups.pop(media_group_id, None)
            if buffer is None:
                continue
            buffer.changed.set()
            await self._forward_media_group_unlocked(bot, buffer.messages)

    async def _flush_media_group(
        self,
        bot: telegram.Bot,
        media_group_id: str,
        expected_buffer: MediaGroupBuffer,
    ) -> None:
        async with self.forward_lock:
            if self.media_groups.get(media_group_id) is not expected_buffer:
                return
            buffer = self.media_groups.pop(media_group_id)
            buffer.changed.set()
            await self._forward_media_group_unlocked(bot, buffer.messages)

    async def _wait_for_media_group(
        self,
        bot: telegram.Bot,
        media_group_id: str,
        buffer: MediaGroupBuffer,
    ) -> None:
        loop = asyncio.get_running_loop()
        while self.media_groups.get(media_group_id) is buffer:
            remaining = buffer.deadline - loop.time()
            if remaining <= 0:
                break
            buffer.changed.clear()
            try:
                await asyncio.wait_for(buffer.changed.wait(), timeout=remaining)
            except TimeoutError:
                pass

        await self._flush_media_group(bot, media_group_id, buffer)

    async def handle_message(
        self,
        update: telegram.Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.message
        if (
            message is None
            or message.from_user is None
            or message.from_user.id != self.config.bot.owner_id
        ):
            return
        if message.text and message.text.startswith("/"):
            return

        if not message.media_group_id:
            async with self.forward_lock:
                await self._flush_all_media_groups_unlocked(context.bot)
                await self._forward_message_unlocked(message)
            return

        media_group_id = message.media_group_id
        buffer = self.media_groups.get(media_group_id)
        if buffer is None:
            # Telegram normally delivers one album contiguously. Flushing an older
            # pending group here preserves source order if a new group starts.
            async with self.forward_lock:
                await self._flush_all_media_groups_unlocked(context.bot)

            loop = asyncio.get_running_loop()
            buffer = MediaGroupBuffer(
                messages=[],
                deadline=loop.time() + self.config.network.media_group_wait_s,
                changed=asyncio.Event(),
            )
            self.media_groups[media_group_id] = buffer
            buffer.task = context.application.create_task(
                self._wait_for_media_group(context.bot, media_group_id, buffer),
                update=update,
                name=f"media-group-{media_group_id}",
            )

        if any(item.message_id == message.message_id for item in buffer.messages):
            return
        buffer.messages.append(message)
        buffer.deadline = (
            asyncio.get_running_loop().time() + self.config.network.media_group_wait_s
        )
        buffer.changed.set()

    def write_heartbeat(self) -> None:
        heartbeat = self.config.watchdog.heartbeat_file
        temporary = heartbeat.with_name(f".{heartbeat.name}.tmp")
        temporary.write_text(str(time.time()), encoding="ascii")
        os.replace(temporary, heartbeat)

    async def heartbeat_task(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            self.write_heartbeat()
        except OSError:
            logger.exception("写入心跳文件失败。")

    async def post_init(self, application: Application) -> None:
        self.write_heartbeat()
        identity = application.bot.bot
        username = f"@{identity.username}" if identity.username else str(identity.id)
        logger.info(
            "机器人 %s 已认证并启动；%s 个发送目标；代理%s启用。",
            username,
            len(self.targets),
            "已" if self.config.bot.proxy_url else "未",
        )

    async def error_handler(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        error = context.error
        if isinstance(error, NetworkError):
            logger.error("Telegram 网络错误: %s", safe_error_text(error, self.config))
        else:
            detail = safe_error_text(error, self.config) if error else "未知错误"
            logger.error("处理更新时发生未捕获异常: %s", detail)


def build_application(config: AppConfig, service: ForwardBotService) -> Application:
    application = (
        ApplicationBuilder()
        .token(config.bot.token)
        .request(create_request(config, for_updates=False))
        .get_updates_request(create_request(config, for_updates=True))
        .post_init(service.post_init)
        .build()
    )

    message_filter = (
        filters.ChatType.PRIVATE
        & filters.User(user_id=config.bot.owner_id)
        & ~filters.COMMAND
    )
    application.add_handler(MessageHandler(message_filter, service.handle_message))
    application.add_error_handler(service.error_handler)

    if application.job_queue is None:
        raise RuntimeError("JobQueue 不可用，请安装 python-telegram-bot[job-queue]。")
    application.job_queue.run_repeating(
        service.heartbeat_task,
        interval=config.watchdog.heartbeat_interval_s,
        name="heartbeat",
    )
    return application


async def check_telegram_connectivity(config: AppConfig) -> telegram.User:
    """Check both configured HTTP routes with getMe without consuming updates."""
    identities: list[telegram.User] = []
    for for_updates in (False, True):
        primary = create_request(config, for_updates=for_updates)
        secondary = create_request(config, for_updates=for_updates)
        bot = telegram.Bot(
            token=config.bot.token,
            request=primary,
            get_updates_request=secondary,
        )
        async with bot:
            identities.append(await bot.get_me())
    if identities[0].id != identities[1].id:
        raise RuntimeError("普通请求与轮询请求返回了不同的 Bot 身份。")
    return identities[0]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram 私聊消息转发机器人")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="配置文件路径（默认: 项目目录/config.json）",
    )
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument(
        "--check-config",
        action="store_true",
        help="只校验配置，不访问 Telegram",
    )
    checks.add_argument(
        "--check",
        action="store_true",
        help="校验配置并用普通/轮询网络路径分别调用 getMe",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    configure_logging(config)
    if config.network.connection_pool_size > 256:
        logger.warning(
            "connection_pool_size=%s 通常远高于本程序所需；建议降至 32 左右。",
            config.network.connection_pool_size,
        )
    logger.info(
        "配置校验通过：%s 个 destination，%s 个实际发送目标。",
        len(config.destinations),
        len(ForwardBotService._expand_targets(config)),
    )

    if args.check_config:
        return 0
    if args.check:
        try:
            identity = asyncio.run(check_telegram_connectivity(config))
        except Exception as exc:
            logger.error(
                "Telegram 连通性检查失败: %s", safe_error_text(exc, config)
            )
            return 1
        username = f"@{identity.username}" if identity.username else str(identity.id)
        logger.info("Telegram 连通性检查通过：%s；未读取或转发任何消息。", username)
        return 0

    service = ForwardBotService(config)
    try:
        instance_lock = acquire_instance_lock()
    except OSError as exc:
        logger.error("无法创建单实例锁: %s", safe_error_text(exc, config))
        return 1
    if instance_lock is None:
        logger.critical("检测到另一个 Bot 主进程正在运行，本进程不会启动。")
        return 3
    try:
        application = build_application(config, service)
        application.run_polling(
            allowed_updates=["message"],
            bootstrap_retries=3,
            close_loop=True,
        )
        return 0
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出。")
        return 0
    except Exception as exc:
        logger.error("机器人异常退出: %s", safe_error_text(exc, config))
        return 1
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    raise SystemExit(main())
