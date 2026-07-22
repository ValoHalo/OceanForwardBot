from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, RetryAfter, TimedOut

import main


def make_config(
    *,
    topic_ids: tuple[int, ...] = (),
    max_retries: int = 2,
    media_group_wait_s: float = 0.01,
) -> main.AppConfig:
    return main.AppConfig(
        bot=main.BotSettings(
            token="123456:TEST_TOKEN_VALUE",
            owner_id=123456,
            proxy_url="socks5://user:password@127.0.0.1:1080",
            silent_forwarding=False,
            log_level="INFO",
        ),
        watchdog=main.WatchdogSettings(
            heartbeat_file=main.BASE_DIR / "test.heartbeat",
            heartbeat_interval_s=10,
            heartbeat_timeout_s=30,
            restart_delay_s=0,
            max_consecutive_restarts=2,
            startup_grace_s=30,
            stable_reset_s=60,
        ),
        network=main.NetworkSettings(
            connection_pool_size=8,
            read_timeout=20,
            connect_timeout=10,
            pool_timeout=10,
            write_timeout=20,
            media_write_timeout=60,
            max_concurrent_sends=2,
            max_retries=max_retries,
            retry_base_delay_s=0.01,
            media_group_wait_s=media_group_wait_s,
        ),
        destinations=(
            main.Destination(
                chat_id=-1001234567890,
                topic_ids=topic_ids,
                silent_forwarding=None,
            ),
        ),
    )


class ConfigTests(unittest.TestCase):
    def write_config(self, payload: dict) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            json.dump(payload, handle)
        return Path(handle.name)

    @staticmethod
    def valid_payload() -> dict:
        return {
            "bot": {
                "token": "123456:TEST_TOKEN_VALUE",
                "owner_id": 123456,
                "proxy_url": "",
                "silent_forwarding": False,
                "log_level": "INFO",
            },
            "watchdog": {
                "heartbeat_file": "test.heartbeat",
                "heartbeat_interval_s": 10,
                "heartbeat_timeout_s": 30,
                "restart_delay_s": 0,
                "max_consecutive_restarts": 0,
            },
            "network": {},
            "destinations": [{"chat_id": -1001234567890, "topic_ids": [2, 2, 3]}],
        }

    def test_load_config_applies_defaults_and_deduplicates_topics(self) -> None:
        config = main.load_config(self.write_config(self.valid_payload()))
        self.assertEqual(config.network.connection_pool_size, 32)
        self.assertEqual(config.watchdog.max_consecutive_restarts, 0)
        self.assertEqual(config.destinations[0].topic_ids, (2, 3))

    def test_owner_id_rejects_boolean(self) -> None:
        payload = self.valid_payload()
        payload["bot"]["owner_id"] = True
        with self.assertRaisesRegex(main.ConfigError, "owner_id"):
            main.load_config(self.write_config(payload))

    def test_heartbeat_cannot_escape_project_directory(self) -> None:
        payload = self.valid_payload()
        payload["watchdog"]["heartbeat_file"] = "../outside.heartbeat"
        with self.assertRaisesRegex(main.ConfigError, "项目目录"):
            main.load_config(self.write_config(payload))

    def test_heartbeat_cannot_target_existing_project_file(self) -> None:
        payload = self.valid_payload()
        payload["watchdog"]["heartbeat_file"] = "README.md"
        with self.assertRaisesRegex(main.ConfigError, "heartbeat"):
            main.load_config(self.write_config(payload))

    def test_duplicate_json_key_is_rejected(self) -> None:
        valid = self.valid_payload()
        serialized = json.dumps(valid)
        duplicated = serialized.replace(
            '"log_level": "INFO"',
            '"log_level": "INFO", "log_level": "DEBUG"',
        )
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        )
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            handle.write(duplicated)
        with self.assertRaisesRegex(main.ConfigError, "重复字段"):
            main.load_config(Path(handle.name))

    def test_proxy_requires_valid_host_and_port(self) -> None:
        payload = self.valid_payload()
        payload["bot"]["proxy_url"] = "socks5://127.0.0.1:not-a-port"
        with self.assertRaisesRegex(main.ConfigError, "端口"):
            main.load_config(self.write_config(payload))

    def test_request_settings_cover_api_and_polling(self) -> None:
        config = make_config()
        api_request = object()
        polling_request = object()
        with patch.object(
            main, "HTTPXRequest", side_effect=[api_request, polling_request]
        ) as constructor:
            self.assertIs(main.create_request(config, for_updates=False), api_request)
            self.assertIs(main.create_request(config, for_updates=True), polling_request)

        api_kwargs = constructor.call_args_list[0].kwargs
        polling_kwargs = constructor.call_args_list[1].kwargs
        self.assertEqual(api_kwargs["connection_pool_size"], 8)
        self.assertEqual(polling_kwargs["connection_pool_size"], 1)
        self.assertEqual(api_kwargs["proxy"], config.bot.proxy_url)
        self.assertEqual(polling_kwargs["proxy"], config.bot.proxy_url)
        self.assertIn("media_write_timeout", api_kwargs)
        self.assertNotIn("media_write_timeout", polling_kwargs)

    def test_safe_error_redacts_token_and_proxy_credentials(self) -> None:
        config = make_config()
        error = RuntimeError(
            f"failed {config.bot.token} via {config.bot.proxy_url} "
            "and http://other:secret@example.invalid"
        )
        rendered = main.safe_error_text(error, config)
        self.assertNotIn(config.bot.token, rendered)
        self.assertNotIn("password", rendered)
        self.assertNotIn("secret", rendered)

    def test_formatter_redacts_exception_traceback(self) -> None:
        config = make_config()
        try:
            raise RuntimeError(
                f"token={config.bot.token} proxy={config.bot.proxy_url}"
            )
        except RuntimeError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="request failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        rendered = main.RedactingFormatter(config).format(record)
        self.assertNotIn(config.bot.token, rendered)
        self.assertNotIn("password", rendered)

    def test_main_instance_lock_is_exclusive(self) -> None:
        lock_path = main.BASE_DIR / "test-main.lock"
        lock_path.unlink(missing_ok=True)
        self.addCleanup(lambda: lock_path.unlink(missing_ok=True))
        first = main.acquire_instance_lock(lock_path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(main.acquire_instance_lock(lock_path))
        finally:
            main.release_instance_lock(first)

        second = main.acquire_instance_lock(lock_path)
        self.assertIsNotNone(second)
        main.release_instance_lock(second)


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_write_is_atomic_and_numeric(self) -> None:
        config = make_config()
        heartbeat = config.watchdog.heartbeat_file
        temporary = heartbeat.with_name(f".{heartbeat.name}.tmp")
        heartbeat.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        self.addCleanup(lambda: heartbeat.unlink(missing_ok=True))
        self.addCleanup(lambda: temporary.unlink(missing_ok=True))

        main.ForwardBotService(config).write_heartbeat()

        self.assertGreater(float(heartbeat.read_text(encoding="ascii")), 0)
        self.assertFalse(temporary.exists())

    async def test_timed_out_request_retries_then_succeeds(self) -> None:
        service = main.ForwardBotService(make_config(max_retries=2))
        target = service.targets[0]
        action = AsyncMock(side_effect=[TimedOut("temporary"), None])
        with patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertTrue(await service._send_with_retry(target, action))
        self.assertEqual(action.await_count, 2)
        sleep.assert_awaited_once()

    async def test_bad_request_is_not_retried(self) -> None:
        service = main.ForwardBotService(make_config(max_retries=3))
        action = AsyncMock(side_effect=BadRequest("bad topic"))
        self.assertFalse(await service._send_with_retry(service.targets[0], action))
        self.assertEqual(action.await_count, 1)

    async def test_retry_after_accepts_timedelta(self) -> None:
        service = main.ForwardBotService(make_config(max_retries=1))
        action = AsyncMock(side_effect=[RetryAfter(main.timedelta(seconds=0)), None])
        with patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertTrue(await service._send_with_retry(service.targets[0], action))
        self.assertGreaterEqual(sleep.await_args.args[0], 0.25)

    async def test_copy_messages_sorts_album_and_preserves_topics(self) -> None:
        service = main.ForwardBotService(make_config(topic_ids=(8, 9)))
        bot = SimpleNamespace(copy_messages=AsyncMock(return_value=(object(), object())))
        messages = [
            SimpleNamespace(message_id=12, chat_id=123456),
            SimpleNamespace(message_id=10, chat_id=123456),
        ]

        await service.forward_media_group(bot, messages)

        self.assertEqual(bot.copy_messages.await_count, 2)
        calls = bot.copy_messages.await_args_list
        self.assertEqual(calls[0].kwargs["message_ids"], [10, 12])
        self.assertEqual(
            {call.kwargs["message_thread_id"] for call in calls}, {8, 9}
        )

    async def test_media_group_debounce_resets_and_deduplicates(self) -> None:
        service = main.ForwardBotService(make_config())
        bot = SimpleNamespace(copy_messages=AsyncMock(return_value=(object(), object())))

        class FakeApplication:
            @staticmethod
            def create_task(coroutine, update=None, *, name=None):
                return asyncio.create_task(coroutine, name=name)

        context = SimpleNamespace(application=FakeApplication(), bot=bot)
        user = SimpleNamespace(id=service.config.bot.owner_id)

        def update(message_id: int) -> SimpleNamespace:
            message = SimpleNamespace(
                message_id=message_id,
                chat_id=service.config.bot.owner_id,
                from_user=user,
                text=None,
                caption=None,
                media_group_id="album-1",
            )
            return SimpleNamespace(message=message)

        first = update(2)
        await service.handle_message(first, context)
        await service.handle_message(first, context)
        await service.handle_message(update(1), context)
        task = service.media_groups["album-1"].task
        self.assertIsNotNone(task)
        await task

        bot.copy_messages.assert_awaited_once()
        self.assertEqual(bot.copy_messages.await_args.kwargs["message_ids"], [1, 2])

    async def test_pending_album_is_sent_before_following_message(self) -> None:
        service = main.ForwardBotService(make_config(media_group_wait_s=60))
        send_order: list[str] = []

        async def copy_album(**kwargs):
            send_order.append("album")
            return (object(),)

        async def copy_message(**kwargs):
            send_order.append("message")
            return object()

        bot = SimpleNamespace(copy_messages=AsyncMock(side_effect=copy_album))

        class FakeApplication:
            @staticmethod
            def create_task(coroutine, update=None, *, name=None):
                return asyncio.create_task(coroutine, name=name)

        context = SimpleNamespace(application=FakeApplication(), bot=bot)
        user = SimpleNamespace(id=service.config.bot.owner_id)
        album_message = SimpleNamespace(
            message_id=1,
            chat_id=service.config.bot.owner_id,
            from_user=user,
            text=None,
            caption=None,
            media_group_id="album-order",
        )
        normal_message = SimpleNamespace(
            message_id=2,
            chat_id=service.config.bot.owner_id,
            from_user=user,
            text="after album",
            caption=None,
            media_group_id=None,
            copy=AsyncMock(side_effect=copy_message),
        )

        await service.handle_message(SimpleNamespace(message=album_message), context)
        album_task = service.media_groups["album-order"].task
        await service.handle_message(SimpleNamespace(message=normal_message), context)
        self.assertIsNotNone(album_task)
        await album_task

        self.assertEqual(send_order, ["album", "message"])

    async def test_partial_batch_copy_is_reported_without_retry(self) -> None:
        service = main.ForwardBotService(make_config(max_retries=3))
        bot = SimpleNamespace(copy_messages=AsyncMock(return_value=(object(),)))
        messages = [
            SimpleNamespace(message_id=1, chat_id=123456),
            SimpleNamespace(message_id=2, chat_id=123456),
        ]
        with self.assertLogs(main.logger, level="ERROR") as captured:
            await service.forward_media_group(bot, messages)
        self.assertEqual(bot.copy_messages.await_count, 1)
        self.assertTrue(any("部分成功" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
