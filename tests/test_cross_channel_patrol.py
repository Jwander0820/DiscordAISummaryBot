import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from discord_bot.db.social_duplicate_repository import SocialDuplicateRepository
from discord_bot.features.social_preview.duplicate_service import DuplicateMatch, SocialDuplicateService
from tests.test_social_duplicate_service import event


class CrossChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(self.temp.name) / "test.db"),
                                          "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7"})
        self.env.start()
        self.repo = SocialDuplicateRepository(retry_interval_seconds=0)
        self.service = SocialDuplicateService(self.repo)
        self.valid = {"10", "11", "12", "13"}
        self.service.text_channel_ids = lambda guild: self.valid
        await self.service.rebuild()

    async def asyncTearDown(self):
        await self.service.close()
        self.env.stop()
        self.temp.cleanup()

    async def configure(self, channels=("10", "11"), enabled=True, **kwargs):
        return await self.service.configure_cross_channel("1", enabled=enabled, channel_ids=channels,
                                                          updated_by="42", **kwargs)

    async def test_default_disabled_and_insufficient_channels(self):
        self.assertEqual(self.service.cross_effective("1"), (False, frozenset()))
        await self.service.register(event())
        self.assertIsNone(await self.service.register(event("101", channel_id="11")))
        await self.configure(("10",))
        self.assertFalse(self.service.cross_channels("1"))
        match = await self.service.register(event("102"))
        self.assertEqual(match.share_count, 2)

    async def test_bidirectional_shared_counts_first_reference_and_idempotence(self):
        await self.configure(("10", "11", "12"))
        first = event()
        await self.service.register(first)
        records = [event("101", channel_id="11"), event("102", channel_id="12"), event("103")]
        for count, record in enumerate(records, 2):
            self.assertEqual(await self.service.register(record), DuplicateMatch(first, count))
            self.assertIsNone(await self.service.register(record))
        for record in (event("104", channel_id="13"), event("105", guild_id="2"),
                       event("106", platform="instagram"), event("107", post_key="Other"),
                       event("108", channel_id="99")):
            self.assertIsNone(await self.service.register(record))

    async def test_enable_existing_history_restart_remove_and_disable(self):
        first = event(occurred_at=time.time() - 20)
        second = event("101", channel_id="11", occurred_at=time.time() - 10)
        await self.service.register(first)
        await self.service.register(second)
        await self.configure(("10", "11", "12"))
        self.assertEqual(await self.service.register(event("102", channel_id="12")), DuplicateMatch(first, 3))
        await self.service.close()
        self.service = SocialDuplicateService(self.repo)
        self.service.text_channel_ids = lambda guild: self.valid
        await self.service.rebuild()
        self.assertEqual(await self.service.register(event("103", channel_id="11")), DuplicateMatch(first, 4))
        await self.configure(("11", "12"))
        self.assertEqual(await self.service.register(event("104", channel_id="12")), DuplicateMatch(second, 4))
        await self.configure(("11", "12"), enabled=False)
        self.assertEqual((await self.service.register(event("105", channel_id="12"))).share_count, 3)
        self.assertEqual(self.service.cross_effective("1"), (False, frozenset({"11", "12"})))

    async def test_window_master_switch_and_deleted_channel_exclusion(self):
        await self.service.register(event(occurred_at=time.time() - 10 * 86400))
        await self.configure()
        self.assertIsNone(await self.service.register(event("101", channel_id="11")))
        self.valid.remove("10")
        self.assertFalse(self.service.cross_channels("1"))
        self.assertIsNone(await self.service.register(event("102")))
        await self.service.configure("1", state="disabled", updated_by="42")
        self.assertIsNone(await self.service.register(event("103", channel_id="11")))
        self.assertFalse(self.service.cache)

    async def test_concurrent_shares_get_one_combined_sequence(self):
        await self.configure()
        results = await asyncio.gather(*(self.service.register(event(str(100 + i), channel_id=str(10 + i % 2)))
                                         for i in range(10)))
        self.assertEqual(sum(match is None for match in results), 1)
        self.assertEqual(sorted(match.share_count for match in results if match), list(range(2, 11)))

    async def test_invalid_channels_stale_panel_and_database_failures(self):
        for channels in (("99", "10"), ("thread",)):
            with self.assertRaises(ValueError):
                await self.configure(channels)
        original = self.service.cross_effective("1")
        await self.configure()
        with self.assertRaisesRegex(ValueError, "其他操作"):
            await self.configure(("11", "12"), expected=original)
        with patch.object(self.repo, "configure_cross_channel", side_effect=OSError("synthetic")):
            self.assertFalse(await self.configure())
        self.assertFalse(self.service.ready)
        await self.service.rebuild()
        with patch.object(self.repo, "cross_channel_settings", side_effect=OSError("synthetic")):
            self.assertFalse(await self.service.rebuild())
        self.assertFalse(self.service.ready)

    async def test_no_discord_channel_inventory_fails_closed(self):
        await self.configure()
        self.service.text_channel_ids = None
        await self.service.register(event())
        self.assertIsNone(await self.service.register(event("101", channel_id="11")))

    async def test_edit_one_guild_keeps_other_guild_runtime_history(self):
        old = event(guild_id="2", occurred_at=time.time() - 100 * 86400)
        await self.service.register(old)
        await self.configure()
        self.assertEqual(await self.service.register(event("101", guild_id="2")), DuplicateMatch(old, 2))
