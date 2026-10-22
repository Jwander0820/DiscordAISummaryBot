"""Raw deletion callbacks use real SDK payloads and a temporary SQLite database."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord

from tests.support import isolated_discord_suite
from tests.test_x_duplicate_patrol import source, URL as X_URL
from tests.test_youtube_duplicate_links import URL as YOUTUBE_URL
from discord_bot.cogs import social_duplicate_cog
from discord_bot.db.social_duplicate_repository import SocialDuplicateRepository
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview.duplicate_service import SocialDuplicateService
from discord_bot.features.social_preview.patrol_records import PatrolDelivery

load_tests = isolated_discord_suite(__name__)


def deletion(message_id=100, *, guild_id=1, channel_id=10):
    data = {"id": str(message_id), "channel_id": str(channel_id)}
    if guild_id is not None:
        data["guild_id"] = str(guild_id)
    return discord.RawMessageDeleteEvent(data)


class OriginalShareDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(self.temp.name) / "shares.db"),
            "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7",
        })
        self.env.start()
        self.repo = SocialDuplicateRepository(retry_interval_seconds=0)
        self.service = SocialDuplicateService(self.repo)
        self.assertTrue(await self.service.rebuild())
        for module in (patrol, social_duplicate_cog):
            patcher = patch.object(module, "social_duplicate_service", self.service)
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

    async def share(self, message_id, *, content=X_URL + " " + YOUTUBE_URL, **kwargs):
        message = source(message_id, content=content, **kwargs)
        await self.cog.on_message(message)
        return message

    async def test_uncached_delete_clears_both_platforms_and_repost_is_first_after_restart(self):
        await self.share(100)
        self.assertEqual(len(self.service.cache), 2)
        payload = deletion()
        self.assertIsNone(payload.cached_message)
        await self.cog.on_raw_message_delete(payload)
        self.assertEqual(self.repo._execute("SELECT COUNT(*) FROM social_share_events"), [(0,)])
        self.assertFalse(self.service.cache)
        self.assertFalse(self.service.counts)
        await self.service.close()
        self.assertTrue(await self.service.rebuild())
        await self.share(101)
        self.announce.assert_not_awaited()
        self.assertEqual(sorted(self.service.counts.values()), [1, 1])

    async def test_single_delete_promotes_survivor_and_decrements_counts_without_erasing_audit(self):
        await self.share(100)
        await self.share(101)
        await self.share(102)
        audits = self.repo._execute("SELECT COUNT(*) FROM social_patrol_events")
        await self.cog.on_raw_message_delete(deletion(100))
        self.assertEqual({event.source_message_id for event in self.service.cache.values()}, {"101"})
        self.assertEqual(sorted(self.service.counts.values()), [2, 2])
        await self.cog.on_raw_message_delete(deletion(102))
        self.assertEqual(sorted(self.service.counts.values()), [1, 1])
        self.assertEqual(self.repo._execute("SELECT COUNT(*) FROM social_patrol_events"), audits)
        self.announce.reset_mock()
        await self.share(103)
        self.assertEqual(self.announce.await_count, 2)
        for call in self.announce.await_args_list:
            self.assertEqual(call.args[1].source_message_id, "101")
            self.assertEqual(call.kwargs["share_count"], 2)

    async def test_bulk_delete_is_scoped_and_leaves_all_three_preview_platforms_untouched(self):
        await self.share(100)
        await self.share(101)
        await self.share(100, guild=2)
        await self.share(100, channel=11)
        original = self.service.cache[("1", "10", "x", X_URL.rsplit("/", 1)[-1])]
        for platform in ("threads", "facebook", "instagram"):
            await self.service.register(replace(original, platform=platform, preview_message_id="9100"))
        saved = {key: event for key, event in self.service.cache.items()
                 if key[:2] != ("1", "10") or key[2] not in {"x", "youtube"}}
        payload = discord.RawBulkMessageDeleteEvent({
            "guild_id": "1", "channel_id": "10", "ids": ["100", "101", "999"],
        })
        self.assertEqual(payload.cached_messages, [])
        await self.cog.on_raw_bulk_message_delete(payload)
        await self.cog.on_raw_bulk_message_delete(payload)  # Repeated events are harmless.
        self.assertEqual(self.service.cache, saved)
        self.assertEqual(set(self.service.counts), set(saved))
        self.assertEqual(self.repo._execute("SELECT COUNT(*) FROM social_share_events"), [(7,)])
        self.assertTrue(await self.service.rebuild())
        self.assertEqual(self.service.cache, saved)

    async def test_raw_delete_uses_source_id_instead_of_preview_reference(self):
        await self.share(100, content=X_URL)
        original = next(iter(self.service.cache.values()))
        await self.service.register(replace(original, source_message_id="101", preview_message_id="9000"))
        await self.cog.on_raw_message_delete(deletion(9000))
        self.assertEqual(self.service.counts[original.key], 2)
        await self.cog.on_raw_message_delete(deletion(101))
        self.assertEqual(self.service.counts[original.key], 1)
        self.assertEqual(self.repo._execute("SELECT source_message_id FROM social_share_events"), [("100",)])

    async def test_cross_channel_match_switches_to_surviving_original(self):
        self.service.text_channel_ids = lambda guild: {"10", "11"}
        self.assertTrue(await self.service.configure_cross_channel(
            "1", enabled=True, channel_ids={"10", "11"}, updated_by="42"))
        await self.share(100)
        await self.share(101, channel=11)
        await self.cog.on_raw_message_delete(deletion(100))
        self.announce.reset_mock()
        await self.share(102)
        self.assertEqual(self.announce.await_count, 2)
        for call in self.announce.await_args_list:
            self.assertEqual(call.args[1].channel_id, "11")
            self.assertEqual(call.args[1].source_message_id, "101")
            self.assertEqual(call.kwargs["share_count"], 2)

    async def test_deletion_while_disabled_removes_persistent_history(self):
        await self.share(100)
        self.assertTrue(await self.service.configure("1", state="disabled", updated_by="42"))
        await self.cog.on_raw_message_delete(deletion())
        self.assertEqual(self.repo._execute("SELECT COUNT(*) FROM social_share_events"), [(0,)])
        self.assertTrue(await self.service.configure("1", state="enabled", updated_by="42"))
        await self.share(101)
        self.announce.assert_not_awaited()

    async def test_untracked_deletion_does_not_touch_cache_and_dm_or_empty_batch_skips_db(self):
        await self.share(100)
        saved, counts = dict(self.service.cache), dict(self.service.counts)
        await self.cog.on_raw_message_delete(deletion(999))
        self.assertEqual(self.service.cache, saved)
        self.assertEqual(self.service.counts, counts)
        with patch.object(self.service, "forget_original_messages", AsyncMock()) as forget:
            await self.cog.on_raw_message_delete(deletion(guild_id=None))
            await self.cog.on_raw_bulk_message_delete(discord.RawBulkMessageDeleteEvent(
                {"channel_id": "10", "ids": ["100"]}))
            await self.cog.on_raw_bulk_message_delete(discord.RawBulkMessageDeleteEvent(
                {"guild_id": "1", "channel_id": "10", "ids": []}))
        forget.assert_not_awaited()

    async def test_database_failure_degrades_and_never_claims_cleanup_succeeded(self):
        await self.share(100)
        saved = dict(self.service.cache)
        with patch.object(self.repo, "delete_original_messages", side_effect=OSError("synthetic failure")), \
                self.assertLogs("discord_digest_bot", level="WARNING") as logs:
            await self.cog.on_raw_message_delete(deletion())
        self.assertFalse(self.service.ready)
        self.assertEqual(self.service.cache, saved)
        self.assertIn("operation=write error_type=OSError", logs.output[0])
        self.assertNotIn(X_URL, " ".join(logs.output))
        self.assertTrue(await self.service.rebuild())
        self.assertEqual(self.service.cache, saved)
        await self.cog.on_raw_message_delete(deletion())
        self.assertFalse(self.service.cache)

    async def test_delete_during_mixed_share_waits_for_both_registrations(self):
        await self.share(100)
        started, release = asyncio.Event(), asyncio.Event()

        async def announce(*args, **kwargs):
            started.set()
            await release.wait()
            return PatrolDelivery("webhook", "900")

        self.announce.side_effect = announce
        sharing = asyncio.create_task(self.share(101))
        removing = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            with patch.object(self.repo, "delete_original_messages", wraps=self.repo.delete_original_messages) as delete:
                removing = asyncio.create_task(self.cog.on_raw_message_delete(deletion(101)))
                await asyncio.sleep(0)  # Let the deletion callback reach its pending-share wait.
                delete.assert_not_called()
                self.assertFalse(removing.done())
                release.set()
                await asyncio.wait_for(asyncio.gather(sharing, removing), timeout=2)
                delete.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(*(task for task in (sharing, removing) if task is not None), return_exceptions=True)
        self.assertFalse(self.cog._pending_originals)
        self.assertEqual(self.repo._execute("SELECT DISTINCT source_message_id FROM social_share_events"), [("100",)])
        self.assertEqual(sorted(self.service.counts.values()), [1, 1])

    async def test_cancelled_registration_releases_pending_delete(self):
        started = asyncio.Event()

        async def register(*args, **kwargs):
            started.set()
            await asyncio.Future()

        with patch.object(social_duplicate_cog, "record_original_share", side_effect=register):
            sharing = asyncio.create_task(self.share(100))
            await asyncio.wait_for(started.wait(), timeout=2)
            sharing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sharing
        self.assertFalse(self.cog._pending_originals)
        await asyncio.wait_for(self.cog.on_raw_message_delete(deletion()), timeout=2)

    async def test_cog_registers_both_raw_listeners(self):
        names = {name for name, callback in self.cog.get_listeners()}
        self.assertIn("on_raw_message_delete", names)
        self.assertIn("on_raw_bulk_message_delete", names)

    async def test_successful_delete_logs_platform_reference_and_url_without_claiming_actor(self):
        await self.share(100)
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_raw_message_delete(deletion())
        self.assertEqual(len(logs.records), 2)
        messages = [record.getMessage() for record in logs.records]
        for label, url in (("X", X_URL), ("YouTube", YOUTUBE_URL)):
            self.assertIn(
                f"{label} 連結已刪除，分享紀錄與快取已更新：guild_id=1 channel_id=10 message_id=100 url={url}",
                messages)
        self.assertTrue(all("user_id" not in message and "tester" not in message for message in messages))

    async def test_unmatched_and_repeated_deletions_do_not_log_success(self):
        from discord_bot.features.social_preview import duplicate_service

        await self.share(100)
        await self.cog.on_raw_message_delete(deletion())
        with patch.object(duplicate_service.logger, "info") as info, \
                patch.object(self.repo, "_execute", wraps=self.repo._execute) as execute:
            await self.cog.on_raw_message_delete(deletion())
            await self.cog.on_raw_message_delete(deletion(999))
        info.assert_not_called()
        self.assertEqual(execute.call_count, 2)  # One indexed lookup per event, no history reload.
        self.assertTrue(all(call.args[0].startswith("DELETE FROM social_share_events")
                            for call in execute.call_args_list))

    async def test_bulk_deletion_log_is_one_line_per_removed_share_and_sanitizes_fields(self):
        await self.share(100, content=X_URL)
        original = next(iter(self.service.cache.values()))
        # Synthetic dirty stored data must not create forged log lines.
        await self.service.register(replace(original, source_message_id="101", canonical_url=X_URL + "\r\nINJECTED"))
        payload = discord.RawBulkMessageDeleteEvent({
            "guild_id": "1", "channel_id": "10", "ids": ["100", "101", "999"],
        })
        with self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_raw_bulk_message_delete(payload)
        self.assertEqual(len(logs.records), 2)
        for record in logs.records:
            self.assertNotIn("\r", record.getMessage())
            self.assertNotIn("\n", record.getMessage())
        self.assertTrue(any("message_id=100" in record.getMessage() for record in logs.records))
        self.assertTrue(any("message_id=101" in record.getMessage() for record in logs.records))

    async def test_cache_refresh_failure_does_not_log_success(self):
        await self.share(100)
        with patch.object(self.repo, "remaining_shares", side_effect=OSError("synthetic failure")), \
                self.assertLogs("discord_digest_bot", level="INFO") as logs:
            await self.cog.on_raw_message_delete(deletion())
        self.assertFalse(self.service.ready)
        self.assertTrue(all(record.levelname == "WARNING" for record in logs.records))
        self.assertNotIn("已更新", " ".join(logs.output))
