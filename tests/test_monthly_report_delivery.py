"""Discord transport and cog entry points for the monthly report, using the real SDK."""
import asyncio
from datetime import datetime, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from tests.support import isolated_discord_suite
from discord_bot.cogs import social_duplicate_cog, social_preview_cog, social_report_cog
from discord_bot.features.monthly_report import delivery
from discord_bot.features.monthly_report.service import DeliveryRejected, DeliveryUncertain

load_tests = isolated_discord_suite(__name__)
ALL = ("view_channel", "send_messages", "read_message_history")


def http_error(kind, status):
    return kind(SimpleNamespace(status=status, reason="test"), {"code": 0, "message": "test"})


def channel(*, guild_id=1, permissions=ALL, history=()):
    me = object()
    guild = SimpleNamespace(id=guild_id, me=me)

    async def history_iter(**kwargs):
        channel.history_kwargs = kwargs
        for item in history:
            yield item

    channel = SimpleNamespace(
        id=55, guild=guild, send=AsyncMock(return_value=SimpleNamespace(id=777)), history=history_iter,
        permissions_for=lambda member: SimpleNamespace(**{name: name in permissions for name in ALL}))
    return channel


def bot_with(target):
    return SimpleNamespace(get_channel=Mock(return_value=target), fetch_channel=AsyncMock(),
                           user=SimpleNamespace(id=999), get_guild=Mock())


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_suppresses_embeds_and_mentions_in_real_payload(self):
        target = channel()
        transport = delivery.DiscordReportTransport(bot_with(target))
        self.assertEqual(await transport.send_page(1, 55, "月報 <https://example.com/a> @everyone"), "777")
        args, kwargs = target.send.await_args
        self.assertTrue(kwargs["suppress_embeds"])
        from discord.http import handle_message_parameters
        flags = discord.MessageFlags()
        flags.suppress_embeds = kwargs["suppress_embeds"]  # Same conversion as Messageable.send.
        with handle_message_parameters(content=args[0], flags=flags,
                                       allowed_mentions=kwargs["allowed_mentions"]) as params:
            self.assertEqual(params.payload["allowed_mentions"], {"parse": []})
            self.assertEqual(params.payload["flags"] & 4, 4)

    async def test_exception_mapping_separates_rejected_from_uncertain(self):
        cases = ((http_error(discord.Forbidden, 403), DeliveryRejected, "Forbidden"),
                 (http_error(discord.NotFound, 404), DeliveryRejected, "ChannelNotFound"),
                 (http_error(discord.HTTPException, 400), DeliveryRejected, "HTTP400"),
                 (http_error(discord.HTTPException, 503), DeliveryUncertain, None),
                 (asyncio.TimeoutError(), DeliveryUncertain, None),
                 (ConnectionResetError(), DeliveryUncertain, None))
        for error, expected, reason in cases:
            with self.subTest(error=type(error).__name__):
                target = channel()
                target.send.side_effect = error
                with self.assertRaises(expected) as caught:
                    await delivery.DiscordReportTransport(bot_with(target)).send_page(1, 55, "x")
                if reason:
                    self.assertEqual(caught.exception.reason, reason)

    async def test_destination_must_be_in_guild_with_view_send_and_history(self):
        self.assertIsNone(delivery.channel_problem(channel(), 1))
        self.assertEqual(delivery.channel_problem(channel(guild_id=2), 1), "ChannelOutsideGuild")
        self.assertEqual(delivery.channel_problem(channel(permissions=ALL[:2]), 1), "MissingPermissions")
        self.assertEqual(delivery.channel_problem(None, 1), "ChannelNotFound")
        self.assertEqual(delivery.channel_problem(SimpleNamespace(guild=SimpleNamespace(id=1)), 1), "ChannelNotText")
        target = channel(permissions=("view_channel",))
        with self.assertRaises(DeliveryRejected) as caught:
            await delivery.DiscordReportTransport(bot_with(target)).send_page(1, 55, "x")
        self.assertEqual(caught.exception.reason, "MissingPermissions")
        target.send.assert_not_awaited()

    async def test_find_page_requires_bot_author_and_exact_fixed_text(self):
        history = [SimpleNamespace(id=1, author=SimpleNamespace(id=5), content="頁面"),
                   SimpleNamespace(id=2, author=SimpleNamespace(id=999), content="頁面 不同"),
                   SimpleNamespace(id=3, author=SimpleNamespace(id=999), content="頁面\n")]
        target = channel(history=history)
        transport = delivery.DiscordReportTransport(bot_with(target))
        self.assertEqual(await transport.find_page(1, 55, "頁面", after=100.0), "3")
        self.assertEqual(target.history_kwargs["after"], datetime.fromtimestamp(100, timezone.utc))
        self.assertIsNone(await transport.find_page(1, 55, "沒有", after=100.0))

    async def test_names_use_member_cache_then_bounded_fetch(self):
        guild = SimpleNamespace(get_member=lambda user: SimpleNamespace(display_name="快取") if user == 1 else None,
                                fetch_member=AsyncMock(side_effect=[SimpleNamespace(display_name="查詢"),
                                                                    http_error(discord.NotFound, 404)]))
        bot = SimpleNamespace(get_guild=Mock(return_value=guild))
        names = await delivery.DiscordReportTransport(bot).resolve_names("1", ["1", "2", "3", "abc"])
        self.assertEqual(names, {"1": "快取", "2": "查詢"})

    async def test_admin_notice_uses_botlog_without_mentions(self):
        target = channel()
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "55",
                                     "DISCORD_NOTIFY_FORWARD_GUILD_ID": "1"}):
            self.assertTrue(await delivery.DiscordReportTransport(bot_with(target)).notify_admin("提醒"))
            self.assertIs(target.send.await_args.kwargs["allowed_mentions"].everyone, False)
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": ""}):
            self.assertFalse(await delivery.DiscordReportTransport(bot_with(target)).notify_admin("提醒"))


def interaction(*, guild_id=1, manager=True, target=None):
    guild = SimpleNamespace(id=guild_id, get_channel=Mock(return_value=target))
    return SimpleNamespace(guild=guild, user=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(manage_guild=manager)),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


class CogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = Mock()
        self.service.settings = AsyncMock(return_value={"enabled": False, "channel_id": None,
                                                        "first_report_month": "2026-11"})
        self.service.configure = AsyncMock(return_value={"enabled": True, "channel_id": "55",
                                                         "first_report_month": "2026-11"})
        self.service.runs = AsyncMock(return_value=[])
        patcher = patch.object(social_report_cog, "monthly_report_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = social_report_cog.SocialReportCog(SimpleNamespace(wait_until_ready=AsyncMock()))

    def test_commands_register_with_real_sdk(self):
        commands = {command.name: command for command in self.cog.get_app_commands()}
        self.assertEqual(set(commands), {"社群月報設定"})
        self.assertEqual([parameter.display_name for parameter in commands["社群月報設定"].parameters],
                         ["頻道", "狀態"])
        self.assertTrue(commands["社群月報設定"].default_permissions.manage_guild)
        choices = commands["社群月報設定"].parameters[1].choices
        self.assertEqual([choice.value for choice in choices], ["enabled", "disabled"])

    async def test_non_manager_and_direct_message_are_rejected(self):
        dm = interaction()
        dm.guild = None
        for item in (interaction(manager=False), dm):
            await self.cog.configure_report.callback(self.cog, item)
            item.response.defer.assert_not_awaited()
        self.service.configure.assert_not_called()

    async def test_any_guild_manager_can_configure_its_own_guild(self):
        target = channel(guild_id=2)
        item = interaction(guild_id=2, target=target)
        await self.cog.configure_report.callback(self.cog, item, channel=target,
                                                 state=SimpleNamespace(value="enabled"))
        self.service.configure.assert_awaited_once_with(2, state="enabled", channel_id=55, updated_by=42)

    async def test_scheduler_always_starts_once_and_stops_on_unload(self):
        started = []

        async def run_forever(transport, *, wait_ready=None):
            started.append(transport)
            await asyncio.Event().wait()
        self.service.run_forever = run_forever
        self.service.close = AsyncMock()
        await self.cog.cog_load()
        await self.cog.cog_load()
        await asyncio.sleep(0)
        self.assertEqual(len(started), 1)
        await self.cog.cog_unload()
        self.assertIsNone(self.cog._task)
        self.service.close.assert_awaited_once()

    def test_transport_knows_whether_bot_is_in_guild(self):
        bot = SimpleNamespace(get_guild=Mock(side_effect=lambda gid: object() if gid == 1 else None))
        transport = delivery.DiscordReportTransport(bot)
        self.assertEqual((transport.has_guild("1"), transport.has_guild("2")), (True, False))

    async def test_channel_without_permissions_is_not_saved(self):
        target = channel(permissions=("view_channel",))
        item = interaction(target=target)
        await self.cog.configure_report.callback(self.cog, item, channel=target)
        self.assertIn("讀取訊息歷史", item.response.send_message.await_args.args[0])
        self.service.configure.assert_not_called()

    async def test_valid_channel_override_and_status_text(self):
        target = channel()
        item = interaction(target=target)
        await self.cog.configure_report.callback(self.cog, item, channel=target)
        self.service.configure.assert_awaited_once_with(1, state=None, channel_id=55, updated_by=42)
        text = item.followup.send.await_args.args[0]
        self.assertIn("已更新", text)
        self.assertIn("<#55>", text)

    async def test_status_without_arguments_explains_how_to_enable(self):
        item = interaction()
        await self.cog.configure_report.callback(self.cog, item)
        text = item.followup.send.await_args.args[0]
        self.assertIn("停用｜頻道：未指定", text)
        self.assertIn("狀態:啟用", text)
        self.service.configure.assert_not_called()

    async def test_enabling_without_channel_reports_validation_error(self):
        self.service.configure.side_effect = ValueError("啟用前請同時指定月報頻道。")
        item = interaction()
        await self.cog.configure_report.callback(self.cog, item, state=SimpleNamespace(value="enabled"))
        self.assertIn("指定月報頻道", item.followup.send.await_args.args[0])

    async def test_database_failure_is_reported_explicitly(self):
        self.service.settings.side_effect = RuntimeError("down")
        item = interaction()
        await self.cog.configure_report.callback(self.cog, item)
        self.assertIn("資料庫暫時無法使用", item.followup.send.await_args.args[0])


class DatabaseNoticeTests(unittest.TestCase):
    def test_monthly_report_db_failures_use_existing_botlog_format(self):
        from discord_bot.db.health import DatabaseEvent
        from discord_bot.features.notifications.database import format_database_event
        text = format_database_event(DatabaseEvent("social_monthly_report_runs", "postgres", "read", False, True,
                                                   "OperationalError", None, "2026-11-01T00:00:00+00:00"))
        self.assertIn("社群出警月報", text)
        self.assertIn("5 分鐘後重試", text)


class BotAuthorTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_event_entries_ignore_bot_report_messages(self):
        report = SimpleNamespace(author=SimpleNamespace(bot=True, id=999), webhook_id=None,
                                 guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=55),
                                 content="1. <https://www.threads.com/@demo/post/Demo1> "
                                         "<https://x.com/demo/status/123> <https://youtu.be/DemoVid0001>")
        with patch.object(social_preview_cog, "handle_threads_in_message", AsyncMock()) as threads, \
                patch.object(social_duplicate_cog, "record_original_share", AsyncMock()) as record:
            await social_preview_cog.SocialPreviewCog(bot=None).on_message(report)
            await social_duplicate_cog.SocialDuplicateCog(bot=None).on_message(report)
        threads.assert_not_awaited()
        record.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
