"""Real SDK + temporary DB; no Discord or YouTube requests."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import isolated_discord_suite
from tests.test_x_duplicate_patrol import source
from tests.test_youtube_duplicate_links import VIDEO, URL
from discord_bot.cogs import social_duplicate_cog, social_preview_cog
from discord_bot.db.social_duplicate_repository import SocialDuplicateRepository
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview.duplicate_service import SocialDuplicateService
from discord_bot.features.social_preview.patrol_records import PatrolDelivery

load_tests = isolated_discord_suite(__name__)


class YouTubePatrolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(self.temp.name) / "test.db"),
            "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7",
            "THREADS_PREVIEW_ENABLED": "0", "FACEBOOK_PREVIEW_ENABLED": "0", "INSTAGRAM_PREVIEW_ENABLED": "0"})
        self.env.start()
        self.repo = SocialDuplicateRepository(retry_interval_seconds=0)
        self.service = SocialDuplicateService(self.repo)
        self.service.text_channel_ids = lambda guild: {"10", "11"}
        await self.service.rebuild()
        patcher = patch.object(patrol, "social_duplicate_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(patrol, "_announce", AsyncMock(return_value=PatrolDelivery("webhook", "900")))
        self.announce = patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = social_duplicate_cog.SocialDuplicateCog(None)

    async def asyncTearDown(self):
        await self.service.close()
        self.env.stop()
        self.temp.cleanup()

    async def share(self, message_id, content=URL, **kwargs):
        message = source(message_id, content=content, **kwargs)
        await self.cog.on_message(message)
        message.delete.assert_not_awaited()
        message.edit.assert_not_awaited()
        message.reply.assert_not_awaited()
        message.channel.send.assert_not_awaited()
        return message

    async def test_alias_repeat_keeps_original_reference_and_records_audit(self):
        await self.share(100, URL + "?si=example")
        self.announce.assert_not_awaited()
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            second = await self.share(101, f"https://youtube.com/watch?v={VIDEO}&t=42")
        self.assertIn(f"貼了 YouTube url {URL}", logs.output[0])
        previous = self.announce.await_args.args[1]
        self.assertEqual((previous.platform, previous.post_key, previous.canonical_url), ("youtube", VIDEO, URL))
        self.assertEqual(previous.preview_message_id, "100")
        self.announce.assert_awaited_once_with(second, previous, share_count=2, taunt_text="")
        rows = self.repo._execute("SELECT platform, share_count, reference_channel_id, delivery_status FROM social_patrol_events")
        self.assertEqual(rows, [("youtube", 2, "10", "sent")])

    async def test_cross_channel_shared_counts_restart_and_duplicate_delivery(self):
        await self.service.configure_cross_channel("1", enabled=True, channel_ids={"10", "11"}, updated_by="42")
        await self.share(100)
        await self.share(101, f"https://youtube.com/shorts/{VIDEO}", channel=11)
        await self.share(101, channel=11)
        self.assertEqual(self.announce.await_count, 1)
        await self.service.close()
        await self.service.rebuild()
        await self.share(102)
        self.assertEqual(self.announce.await_args.kwargs["share_count"], 3)
        self.assertEqual(self.announce.await_args.args[1].preview_message_id, "100")

    async def test_same_channel_default_and_platform_guild_isolation(self):
        await self.share(100)
        await self.share(101, channel=11)
        await self.share(102, guild=2)
        await self.share(103, "https://youtu.be/OtherVid002")
        self.announce.assert_not_awaited()
        await self.share(104)
        self.assertEqual(self.announce.await_args.kwargs["share_count"], 2)

    async def test_only_first_video_per_message_and_mixed_x_records_both(self):
        x_url = "https://x.com/example_user/status/1234567890123456789"
        await self.share(100, f"{URL} {URL}?si=example https://youtu.be/OtherVid002 {x_url}")
        self.assertEqual(self.repo._execute("SELECT platform, post_key FROM social_share_events ORDER BY platform"),
                         [("x", "1234567890123456789"), ("youtube", VIDEO)])
        await self.share(101, "https://youtu.be/OtherVid002")
        self.announce.assert_not_awaited()

    async def test_disabled_and_unavailable_do_not_record_or_alert(self):
        await self.service.configure("1", state="disabled", updated_by="42")
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.share(100)
        self.assertIn("YouTube", logs.output[0])
        self.service.ready = False
        await self.share(101)
        self.assertFalse(self.service.cache)
        self.assertEqual(self.repo._execute("SELECT COUNT(*) FROM social_share_events"), [(0,)])
        self.announce.assert_not_awaited()

    async def test_bots_webhooks_dms_and_non_video_links_skipped(self):
        await self.share(100, bot=True)
        await self.share(101, webhook_id=99)
        await self.share(102, guild=None)
        await self.share(103, "https://youtube.com/playlist?list=example")
        self.assertFalse(self.service.cache)
        self.announce.assert_not_awaited()

    async def test_preview_handlers_are_not_invoked_for_youtube(self):
        message = source(100, content=URL)
        with patch.object(social_preview_cog, "handle_threads_in_message", AsyncMock()) as threads, \
                patch.object(social_preview_cog, "handle_instagram_in_message", AsyncMock()) as instagram, \
                patch.object(social_preview_cog, "handle_facebook_in_message", AsyncMock()) as facebook:
            await social_preview_cog.SocialPreviewCog(None).on_message(message)
            await self.cog.on_message(message)
        threads.assert_not_awaited()
        instagram.assert_not_awaited()
        facebook.assert_not_awaited()
