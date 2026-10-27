from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

import main


def make_config(
    *,
    topic_ids: tuple[int, ...] = (),
    max_retries: int = 2,
    media_group_wait_s: float = 0.01,
    url_rewrites: tuple[main.UrlRewriteRule, ...] = (),
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
            restart_delay_s=0,
            max_consecutive_restarts=2,
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
        message_processing=main.MessageProcessingSettings(
            url_rewrites=url_rewrites
        ),
    )


def make_fxtwitter_rule() -> main.UrlRewriteRule:
    return main.UrlRewriteRule(
        name="fxtwitter",
        enabled=True,
        source_hosts=("x.com", "www.x.com", "twitter.com", "www.twitter.com"),
        target_host="fxtwitter.com",
        path_pattern=re.compile(
            r"^/(?:i/status/[0-9]+|[^/]+/status/[0-9]+)(?:/.*)?$"
        ),
        strip_query=True,
        strip_fragment=True,
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
        self.assertEqual(config.message_processing.url_rewrites, ())

    def test_load_config_parses_url_rewrite_rule(self) -> None:
        payload = self.valid_payload()
        payload["message_processing"] = {
            "url_rewrites": [
                {
                    "name": "fxtwitter",
                    "source_hosts": ["X.COM", "x.com", "twitter.com"],
                    "target_host": "fxtwitter.com",
                    "path_pattern": r"^/.+/status/[0-9]+$",
                    "strip_query": True,
                    "strip_fragment": True,
                }
            ]
        }

        config = main.load_config(self.write_config(payload))

        rule = config.message_processing.url_rewrites[0]
        self.assertEqual(rule.source_hosts, ("x.com", "twitter.com"))
        self.assertEqual(rule.target_host, "fxtwitter.com")
        self.assertTrue(rule.path_pattern.fullmatch("/owner/status/123"))

    def test_url_rewrite_host_rejects_protocol(self) -> None:
        payload = self.valid_payload()
        payload["message_processing"] = {
            "url_rewrites": [
                {
                    "name": "bad",
                    "source_hosts": ["https://x.com"],
                    "target_host": "fxtwitter.com",
                    "path_pattern": ".*",
                }
            ]
        }
        with self.assertRaisesRegex(main.ConfigError, "不要包含协议"):
            main.load_config(self.write_config(payload))

    def test_url_rewrite_rejects_duplicate_name(self) -> None:
        payload = self.valid_payload()
        rule = {
            "name": "same",
            "source_hosts": ["x.com"],
            "target_host": "fxtwitter.com",
            "path_pattern": ".*",
        }
        payload["message_processing"] = {
            "url_rewrites": [rule, {**rule, "name": "SAME"}]
        }
        with self.assertRaisesRegex(main.ConfigError, "名称重复"):
            main.load_config(self.write_config(payload))

    def test_url_rewrite_rejects_invalid_regex_and_boolean(self) -> None:
        payload = self.valid_payload()
        base_rule = {
            "name": "bad",
            "source_hosts": ["x.com"],
            "target_host": "fxtwitter.com",
            "path_pattern": "[",
        }
        payload["message_processing"] = {"url_rewrites": [base_rule]}
        with self.assertRaisesRegex(main.ConfigError, "不是有效正则"):
            main.load_config(self.write_config(payload))

        base_rule["path_pattern"] = ".*"
        base_rule["strip_query"] = "yes"
        with self.assertRaisesRegex(main.ConfigError, "strip_query"):
            main.load_config(self.write_config(payload))

    def test_owner_id_rejects_boolean(self) -> None:
        payload = self.valid_payload()
        payload["bot"]["owner_id"] = True
        with self.assertRaisesRegex(main.ConfigError, "owner_id"):
            main.load_config(self.write_config(payload))

    def test_legacy_monitoring_settings_are_ignored(self) -> None:
        payload = self.valid_payload()
        payload["watchdog"]["heartbeat_file"] = "../outside.heartbeat"
        payload["watchdog"]["heartbeat_interval_s"] = 0
        payload["network"]["polling_stale_timeout_s"] = 0
        config = main.load_config(self.write_config(payload))
        self.assertEqual(config.watchdog.max_consecutive_restarts, 0)

    def test_watchdog_settings_are_optional(self) -> None:
        payload = self.valid_payload()
        del payload["watchdog"]
        config = main.load_config(self.write_config(payload))
        self.assertEqual(config.watchdog.max_consecutive_restarts, 5)
        self.assertEqual(config.watchdog.restart_delay_s, 5)
        self.assertEqual(config.watchdog.stable_reset_s, 600)

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

    def test_safe_error_includes_redacted_cause_chain(self) -> None:
        config = make_config()
        try:
            try:
                raise OSError(f"proxy failed: {config.bot.proxy_url}")
            except OSError as cause:
                raise NetworkError("polling failed") from cause
        except NetworkError as error:
            rendered = main.safe_error_text(error, config)

        self.assertIn("NetworkError", rendered)
        self.assertIn("OSError", rendered)
        self.assertNotIn("password", rendered)

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
    def test_process_draft_rewrites_supported_x_status_urls(self) -> None:
        service = main.ForwardBotService(
            make_config(url_rewrites=(make_fxtwitter_rule(),))
        )
        cases = {
            "https://x.com/owner/status/123?s=20#media": (
                "https://fxtwitter.com/owner/status/123"
            ),
            "http://twitter.com/i/status/456/video/1?ref_src=test": (
                "https://fxtwitter.com/i/status/456/video/1"
            ),
            "  https://WWW.X.COM/user/status/789/photo/1  ": (
                "https://fxtwitter.com/user/status/789/photo/1"
            ),
            "https://www.twitter.com/user/status/42/": (
                "https://fxtwitter.com/user/status/42/"
            ),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                message = SimpleNamespace(text=source)
                processed = service.process_draft((message,))
                self.assertIsNotNone(processed)
                self.assertEqual(processed.text, expected)
                self.assertEqual(processed.rule_name, "fxtwitter")

    def test_process_draft_rewrites_supported_bare_domain_urls(self) -> None:
        pixiv_rule = main.UrlRewriteRule(
            name="phixiv",
            enabled=True,
            source_hosts=("pixiv.net", "www.pixiv.net"),
            target_host="www.phixiv.net",
            path_pattern=re.compile(r"^/(?:[a-z]{2}/)?artworks/[0-9]+$"),
            strip_query=True,
            strip_fragment=True,
        )
        service = main.ForwardBotService(
            make_config(url_rewrites=(make_fxtwitter_rule(), pixiv_rule))
        )
        cases = {
            "x.com/owner/status/123?s=20": "https://fxtwitter.com/owner/status/123",
            "www.pixiv.net/artworks/147533083": (
                "https://www.phixiv.net/artworks/147533083"
            ),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                processed = service.process_draft((SimpleNamespace(text=source),))
                self.assertIsNotNone(processed)
                self.assertEqual(processed.text, expected)

    def test_process_draft_ignores_non_standalone_or_unsupported_content(self) -> None:
        service = main.ForwardBotService(
            make_config(url_rewrites=(make_fxtwitter_rule(),))
        )
        rejected = (
            "请看 https://x.com/owner/status/123",
            "https://x.com/a/status/1 https://x.com/b/status/2",
            "https://x.com/owner",
            "https://x.com/search?q=test",
            "https://example.com/owner/status/123",
            "ftp://x.com/owner/status/123",
        )
        for source in rejected:
            with self.subTest(source=source):
                self.assertIsNone(
                    service.process_draft((SimpleNamespace(text=source),))
                )

        caption_only = SimpleNamespace(text=None, caption="https://x.com/a/status/1")
        self.assertIsNone(service.process_draft((caption_only,)))
        self.assertIsNone(
            service.process_draft(
                (
                    SimpleNamespace(text="https://x.com/a/status/1"),
                    SimpleNamespace(text="https://x.com/b/status/2"),
                )
            )
        )

    def test_process_draft_uses_first_enabled_matching_rule(self) -> None:
        disabled = main.UrlRewriteRule(
            name="disabled",
            enabled=False,
            source_hosts=("x.com",),
            target_host="disabled.example",
            path_pattern=re.compile(r".*"),
            strip_query=False,
            strip_fragment=False,
        )
        second = make_fxtwitter_rule()
        service = main.ForwardBotService(
            make_config(url_rewrites=(disabled, second))
        )

        processed = service.process_draft(
            (SimpleNamespace(text="https://x.com/a/status/1?x=1"),)
        )

        self.assertEqual(processed.rule_name, "fxtwitter")
        self.assertEqual(processed.text, "https://fxtwitter.com/a/status/1")

    async def test_stage_draft_sends_processed_preview_with_buttons(self) -> None:
        service = main.ForwardBotService(
            make_config(url_rewrites=(make_fxtwitter_rule(),))
        )
        preview = SimpleNamespace(message_id=99, copy=AsyncMock())
        bot = SimpleNamespace(send_message=AsyncMock(return_value=preview))
        original = SimpleNamespace(
            message_id=7,
            text="https://x.com/a/status/1?s=20",
            copy=AsyncMock(),
        )

        await service._stage_draft(bot, [original])

        call = bot.send_message.await_args.kwargs
        self.assertEqual(call["text"], "https://fxtwitter.com/a/status/1")
        self.assertEqual(call["reply_to_message_id"], 7)
        button_labels = [
            button.text
            for row in call["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            button_labels, ["确认发送", "取消", "发送原文"]
        )
        self.assertEqual(service.pending_drafts[7].processed_messages, (preview,))

    async def test_stage_draft_without_match_waits_for_confirmation(self) -> None:
        service = main.ForwardBotService(
            make_config(url_rewrites=(make_fxtwitter_rule(),))
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        original = SimpleNamespace(
            message_id=8,
            text="普通消息",
            copy=AsyncMock(),
        )

        await service._stage_draft(bot, [original])

        call = bot.send_message.await_args.kwargs
        self.assertEqual(call["text"], "消息待发送。")
        self.assertEqual(
            [button.text for row in call["reply_markup"].inline_keyboard for button in row],
            ["确认发送", "取消"],
        )
        original.copy.assert_not_awaited()
        self.assertIsNone(service.pending_drafts[8].processed_messages)

    async def test_ping_command_replies_for_owner_private_chat(self) -> None:
        service = main.ForwardBotService(make_config())
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=service.config.bot.owner_id),
            effective_chat=SimpleNamespace(type=main.telegram.constants.ChatType.PRIVATE),
        )

        await service.ping_command(update, SimpleNamespace())

        message.reply_text.assert_awaited_once_with("Bot 在线。")

    async def test_ping_command_ignores_non_owner(self) -> None:
        service = main.ForwardBotService(make_config())
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(type=main.telegram.constants.ChatType.PRIVATE),
        )

        await service.ping_command(update, SimpleNamespace())

        message.reply_text.assert_not_awaited()

    async def test_application_uses_normal_polling_without_jobs(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0
        service = main.ForwardBotService(make_config())
        application = main.build_application(service.config, service)
        try:
            polling_request = application.bot._request[0]
            self.assertIsInstance(polling_request, main.HTTPXRequest)
            self.assertIsNone(application.job_queue)
            self.assertEqual(main.ALLOWED_UPDATES, ("message", "callback_query"))
            self.assertTrue(
                any(
                    isinstance(handler, main.CallbackQueryHandler)
                    for handlers in application.handlers.values()
                    for handler in handlers
                )
            )
        finally:
            await asyncio.gather(
                *(request.shutdown() for request in application.bot._request)
            )

    async def test_polling_network_warnings_are_throttled(self) -> None:
        service = main.ForwardBotService(make_config())
        context = SimpleNamespace(error=TimedOut("offline"))
        with patch.object(main.time, "monotonic", side_effect=[0, 1, 60]):
            with self.assertLogs(main.logger, level="WARNING") as captured:
                for _ in range(3):
                    await service.error_handler(None, context)
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all("自动重试" in line for line in captured.output))

    async def test_message_network_error_is_not_reported_as_polling_retry(self) -> None:
        service = main.ForwardBotService(make_config())
        with self.assertLogs(main.logger, level="ERROR") as captured:
            await service.error_handler(object(), SimpleNamespace(error=TimedOut()))
        self.assertNotIn("自动重试", captured.output[0])

    async def test_bad_request_is_not_reported_as_polling_retry(self) -> None:
        service = main.ForwardBotService(make_config())
        with self.assertLogs(main.logger, level="ERROR") as captured:
            await service.error_handler(None, SimpleNamespace(error=BadRequest("bad request")))
        self.assertNotIn("自动重试", captured.output[0])

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
        bot = SimpleNamespace(
            copy_messages=AsyncMock(return_value=(object(), object())),
            send_message=AsyncMock(),
        )

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

        bot.copy_messages.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "相册（2 项）待发送。")
        draft = service.pending_drafts[1]
        self.assertEqual(
            [message.message_id for message in draft.original_messages], [1, 2]
        )

    async def test_pending_album_is_staged_before_following_message(self) -> None:
        service = main.ForwardBotService(make_config(media_group_wait_s=60))
        prompt_order: list[int] = []

        async def send_prompt(**kwargs):
            prompt_order.append(kwargs["reply_to_message_id"])

        bot = SimpleNamespace(
            copy_messages=AsyncMock(return_value=(object(),)),
            send_message=AsyncMock(side_effect=send_prompt),
        )

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
            copy=AsyncMock(),
        )

        await service.handle_message(SimpleNamespace(message=album_message), context)
        album_task = service.media_groups["album-order"].task
        await service.handle_message(SimpleNamespace(message=normal_message), context)
        self.assertIsNotNone(album_task)
        await album_task

        self.assertEqual(prompt_order, [1, 2])
        bot.copy_messages.assert_not_awaited()
        normal_message.copy.assert_not_awaited()
        self.assertEqual(set(service.pending_drafts), {1, 2})

    async def test_confirm_button_sends_and_reports_target_count(self) -> None:
        service = main.ForwardBotService(make_config(topic_ids=(8, 9)))
        message = SimpleNamespace(message_id=7, copy=AsyncMock())
        service.pending_drafts[7] = main.PendingDraft(original_messages=(message,))
        query = SimpleNamespace(
            data="draft:7:confirm",
            from_user=SimpleNamespace(id=service.config.bot.owner_id),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(text="预览消息"),
        )
        context = SimpleNamespace(bot=SimpleNamespace())

        await service.handle_draft_action(
            SimpleNamespace(callback_query=query), context
        )

        self.assertEqual(message.copy.await_count, 2)
        query.edit_message_text.assert_awaited_once()
        edit_call = query.edit_message_text.await_args
        self.assertEqual(edit_call.args[0], "已发送 2/2 个目标。")
        status_buttons = edit_call.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(status_buttons), 1)
        self.assertEqual(len(status_buttons[0]), 1)
        self.assertEqual(status_buttons[0][0].text, "✅已发送")
        self.assertEqual(status_buttons[0][0].callback_data, "draft:7:done")
        self.assertNotIn(7, service.pending_drafts)

    async def test_sent_status_button_does_not_send_again(self) -> None:
        service = main.ForwardBotService(make_config())
        query = SimpleNamespace(
            data="draft:7:done",
            from_user=SimpleNamespace(id=service.config.bot.owner_id),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await service.handle_draft_action(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(bot=SimpleNamespace()),
        )

        query.answer.assert_awaited_once_with("消息已经处理，不会重复发送。")
        query.edit_message_text.assert_not_awaited()

    def test_partial_send_uses_warning_status_button(self) -> None:
        keyboard = main.ForwardBotService._draft_status_keyboard(
            7, succeeded=1, total=2
        )
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "⚠️发送未完成")

    async def test_direct_button_bypasses_processed_preview(self) -> None:
        service = main.ForwardBotService(make_config())
        original = SimpleNamespace(message_id=7, copy=AsyncMock())
        processed = SimpleNamespace(message_id=8, copy=AsyncMock())
        service.pending_drafts[7] = main.PendingDraft(
            original_messages=(original,), processed_messages=(processed,)
        )
        query = SimpleNamespace(
            data="draft:7:direct",
            from_user=SimpleNamespace(id=service.config.bot.owner_id),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await service.handle_draft_action(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(bot=SimpleNamespace()),
        )

        original.copy.assert_awaited_once()
        processed.copy.assert_not_awaited()
        self.assertEqual(query.edit_message_text.await_args.args[0], "已发送 1/1 个目标。")

    async def test_confirm_sends_processed_preview_and_preserves_its_text(self) -> None:
        service = main.ForwardBotService(make_config())
        original = SimpleNamespace(message_id=7, copy=AsyncMock())
        processed = SimpleNamespace(
            message_id=8, text="https://fxtwitter.com/a/status/1", copy=AsyncMock()
        )
        service.pending_drafts[7] = main.PendingDraft(
            original_messages=(original,), processed_messages=(processed,)
        )
        query = SimpleNamespace(
            data="draft:7:confirm",
            from_user=SimpleNamespace(id=service.config.bot.owner_id),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await service.handle_draft_action(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=SimpleNamespace())
        )
        processed.copy.assert_awaited_once()
        original.copy.assert_not_awaited()
        self.assertEqual(
            query.edit_message_text.await_args.args[0],
            processed.text + "\n\n已发送 1/1 个目标。",
        )

    async def test_cancel_button_never_sends(self) -> None:
        service = main.ForwardBotService(make_config())
        message = SimpleNamespace(message_id=7, copy=AsyncMock())
        service.pending_drafts[7] = main.PendingDraft(original_messages=(message,))
        query = SimpleNamespace(
            data="draft:7:cancel",
            from_user=SimpleNamespace(id=service.config.bot.owner_id),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await service.handle_draft_action(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(bot=SimpleNamespace()),
        )

        message.copy.assert_not_awaited()
        query.edit_message_text.assert_awaited_once_with("已取消，消息未发送。")

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
