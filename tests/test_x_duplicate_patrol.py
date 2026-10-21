"""X patrol uses original messages only; fake Discord network and temporary DB."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from tests.support import isolated_discord_suite
from discord_bot.cogs import social_duplicate_cog, social_preview_cog
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview.duplicate_service import SocialDuplicateService
from discord_bot.db.social_duplicate_repository import SocialDuplicateRepository

load_tests = isolated_discord_suite(__name__)

STATUS_ID = "1234567890123456789"
URL = f"https://x.com/example_user/status/{STATUS_ID}"


def source(message_id, *, content=URL, channel=10, guild=1, bot=False, webhook_id=None):
    return SimpleNamespace(id=message_id, content=content, author=SimpleNamespace(id=42, bot=bot, display_name="範例使用者"),
        channel=SimpleNamespace(id=channel, name="範例頻道", send=AsyncMock()), guild=SimpleNamespace(id=guild) if guild else None,
        webhook_id=webhook_id, delete=AsyncMock(), edit=AsyncMock(), reply=AsyncMock())


class XPatrolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(self.temp.name) / "test.db"),
            "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7",
            "THREADS_PREVIEW_ENABLED": "0", "FACEBOOK_PREVIEW_ENABLED": "0", "INSTAGRAM_PREVIEW_ENABLED": "0"})
        self.env.start()
        self.service = SocialDuplicateService(SocialDuplicateRepository(retry_interval_seconds=0))
        await self.service.rebuild()
        patcher = patch.object(patrol, "social_duplicate_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.announce_patch = patch.object(patrol, "_announce", AsyncMock())
        self.announce = self.announce_patch.start()
        self.addCleanup(self.announce_patch.stop)
        self.cog = social_duplicate_cog.SocialDuplicateCog(None)

    async def asyncTearDown(self):
        await self.service.close()
        self.env.stop()
        self.temp.cleanup()

    def assert_original_untouched(self, *messages):
        for message in messages:
            message.delete.assert_not_awaited()
            message.edit.assert_not_awaited()
            message.reply.assert_not_awaited()
            message.channel.send.assert_not_awaited()

    async def test_first_share_records_original_and_tracking_duplicate_alerts(self):
        first = source(100)
        duplicate = source(101, content=URL + "?s=46")
        await self.cog.on_message(first)
        self.announce.assert_not_awaited()
        await self.cog.on_message(duplicate)
        previous = self.announce.await_args.args[1]
        self.assertEqual((previous.platform, previous.post_key, previous.canonical_url), ("x", STATUS_ID, URL))
        self.assertEqual(previous.source_message_id, "100")
        self.assertEqual(previous.preview_message_id, "100")
        self.announce.assert_awaited_once_with(duplicate, previous, share_count=2)
        self.assert_original_untouched(first, duplicate)

    async def test_activity_logs_each_share_once_with_normalized_url_and_safe_names(self):
        first = source(100, content=URL + "?s=46#tracking")
        first.author.nick = "範例\n使用者"
        first.channel.name = "範例\r頻道"
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_message(first)
        self.assertEqual([record.getMessage() for record in logs.records],
                         [f"範例 使用者 在 範例 頻道 貼了 X url {URL}"])
        second = source(101, content=URL + "/photo/1?s=46")
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_message(second)
        self.assertEqual([record.getMessage() for record in logs.records],
                         [f"範例使用者 在 範例頻道 貼了 X url {URL}"])
        self.assert_original_untouched(first, second)

    async def test_detection_is_logged_when_patrol_disabled_but_history_is_not_written(self):
        await self.service.configure("1", state="disabled", updated_by="42")
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_message(source(100, content=URL + "?s=46"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f"貼了 X url {URL}", logs.records[0].getMessage())
        self.assertFalse(self.service.cache)
        self.announce.assert_not_awaited()

    async def test_restart_escalation_and_duplicate_source_idempotence(self):
        first, second = source(100), source(101, content=URL + "/photo/1")
        await self.cog.on_message(first)
        await self.cog.on_message(second)
        await self.cog.on_message(second)
        self.assertEqual(self.announce.await_count, 1)
        await self.service.close()
        await self.service.rebuild()
        third = source(102, content=f"https://twitter.com/renamed_user/status/{STATUS_ID}")
        await self.cog.on_message(third)
        self.assertEqual(self.announce.await_args.kwargs["share_count"], 3)
        self.assertEqual(self.announce.await_args.args[1].preview_message_id, "100")
        self.assert_original_untouched(first, second, third)

    async def test_same_channel_only_and_first_valid_link_only(self):
        await self.cog.on_message(source(100))
        await self.cog.on_message(source(101, channel=11))
        await self.cog.on_message(source(102, guild=2))
        await self.cog.on_message(source(103, content="https://x.com/another/status/2234567890123456789 " + URL))
        self.announce.assert_not_awaited()
        await self.cog.on_message(source(104, content=URL + " " + URL + "?s=46"))
        self.assertEqual(self.announce.await_args.kwargs["share_count"], 2)

    async def test_bot_webhook_dm_and_non_x_are_skipped_without_history(self):
        for message in (source(100, bot=True), source(101, webhook_id=99), source(102, guild=None),
                        source(103, content="https://x.com/example_user"),
                        source(104, content="https://www.threads.com/@demo/post/Example001")):
            await self.cog.on_message(message)
            self.assert_original_untouched(message)
        self.assertFalse(self.service.cache)
        self.announce.assert_not_awaited()

    async def test_disabled_or_unavailable_service_leaves_original_and_does_not_record(self):
        await self.service.configure("1", state="disabled", updated_by="42")
        disabled = source(100)
        await self.cog.on_message(disabled)
        self.assertFalse(self.service.cache)
        await self.service.configure("1", state="enabled", updated_by="42")
        self.service.ready = False
        unavailable = source(101)
        await self.cog.on_message(unavailable)
        self.assertFalse(self.service.cache)
        self.assert_original_untouched(disabled, unavailable)
        self.announce.assert_not_awaited()

    async def test_all_preview_handlers_are_untouched_for_x_message(self):
        first = source(100)
        previews = social_preview_cog.SocialPreviewCog(None)
        with patch.object(social_preview_cog, "handle_threads_in_message", AsyncMock()) as threads, \
                patch.object(social_preview_cog, "handle_instagram_in_message", AsyncMock()) as instagram, \
                patch.object(social_preview_cog, "handle_facebook_in_message", AsyncMock()) as facebook:
            await previews.on_message(first)
            await self.cog.on_message(first)
        threads.assert_not_awaited()
        instagram.assert_not_awaited()
        facebook.assert_not_awaited()
        self.assert_original_untouched(first)

    async def test_parallel_sources_have_atomic_counts_and_notice_failure_does_not_touch_originals(self):
        messages = [source(100 + index) for index in range(3)]
        self.announce.side_effect = RuntimeError("synthetic notice failure")
        await asyncio.gather(*(self.cog.on_message(message) for message in messages))
        self.assertEqual(sorted(call.kwargs["share_count"] for call in self.announce.await_args_list), [2, 3])
        self.assert_original_untouched(*messages)


class XNativeFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_reference_uses_original_x_message_without_deleting_or_editing(self):
        from discord_bot.db.social_duplicate_repository import ShareEvent
        original = source(100)
        duplicate = source(101)
        previous = ShareEvent("1", "10", "x", "100", "42", STATUS_ID, URL, "100", 1.0)
        with patch.object(patrol, "send_patrol_webhook", AsyncMock(return_value=False)):
            await patrol._announce(duplicate, previous, share_count=2)
        kwargs = duplicate.channel.send.await_args.kwargs
        self.assertEqual(kwargs["reference"].message_id, original.id)
        self.assertFalse(kwargs["mention_author"])
        for message in (original, duplicate):
            message.delete.assert_not_awaited()
            message.edit.assert_not_awaited()
