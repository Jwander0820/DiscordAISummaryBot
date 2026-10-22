import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from discord_bot.db.social_duplicate_repository import ShareEvent, SocialDuplicateRepository
from discord_bot.features.social_preview.duplicate_identity import post_identity
from discord_bot.features.social_preview.duplicate_service import DuplicateMatch, SocialDuplicateService


def event(source="100", **overrides):
    record = ShareEvent("1", "10", "threads", source, "42", "Demo001",
                        "https://www.threads.com/@demo/post/Demo001", str(int(source) + 1000), time.time())
    return replace(record, **overrides)


class DuplicateIdentityTests(unittest.TestCase):
    def test_tracking_aliases_and_versions(self):
        for platform, first, second in [
            ("threads", "https://www.threads.net/@demo/post/Ab_c?xmt=test#part", "https://www.threads.com/@renamed/post/Ab_c"),
            ("instagram", "https://instagram.com/reel/Ab_c/?igsh=test", "https://www.instagram.com/p/Ab_c/"),
            ("facebook", "https://m.facebook.com/watch/?v=123&utm_source=test", "https://www.facebook.com/demo/videos/123/"),
            ("facebook", "https://www.facebook.com/demo/posts/123", "https://m.facebook.com/permalink.php?story_fbid=123&id=456"),
        ]:
            with self.subTest(platform=platform, first=first):
                self.assertEqual(post_identity(platform, first)[0], post_identity(platform, second)[0])
                self.assertNotIn("test", post_identity(platform, first)[1])

    def test_unresolved_shares_access_walls_and_untrusted_hosts_are_skipped(self):
        for platform, url in [
            ("facebook", "https://www.facebook.com/share/p/Demo/"),
            ("facebook", "https://www.facebook.com/login/?next=/demo/posts/123"),
            ("facebook", "https://www.facebook.com/login/?story_fbid=123"),
            ("facebook", "https://www.facebook.com/share/p/Demo/?story_fbid=123"),
            ("facebook", "https://example.com/demo/posts/123"),
            ("threads", "https://www.threads.com/share/Demo"),
            ("instagram", "https://instagram.com/accounts/login/"),
            ("threads", "https://www.threads.com.evil.example/@demo/post/Demo001"),
            ("threads", "https://user@www.threads.com/@demo/post/Demo001"),
        ]:
            with self.subTest(url=url):
                self.assertIsNone(post_identity(platform, url))

    def test_case_sensitive_ids_are_not_collapsed(self):
        self.assertNotEqual(post_identity("instagram", "https://instagram.com/p/AbC/")[0],
                            post_identity("instagram", "https://instagram.com/p/abc/")[0])


class DuplicateServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(self.temp.name) / "test.db"),
                                          "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7"})
        self.env.start()
        self.repository = SocialDuplicateRepository(retry_interval_seconds=0)
        self.service = SocialDuplicateService(self.repository)
        self.assertTrue(await self.service.rebuild())

    async def asyncTearDown(self):
        await self.service.close()
        self.env.stop()
        self.temp.cleanup()

    async def test_defaults_and_same_channel_first_preview(self):
        self.assertEqual(self.service.effective("1"), (True, 7))
        first = event()
        self.assertIsNone(await self.service.register(first))
        self.assertEqual(await self.service.register(event("101")), DuplicateMatch(first, 2))
        self.assertIsNone(await self.service.register(event("102", channel_id="11")))
        self.assertIsNone(await self.service.register(event("103", guild_id="2")))
        self.assertIsNone(await self.service.register(event("104", platform="instagram")))

    async def test_concurrent_delivery_and_idempotent_source(self):
        results = await asyncio.gather(*(self.service.register(event(str(100 + i))) for i in range(12)))
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(len(self.service.cache), 1)
        self.assertEqual([result.share_count for result in results if result is not None], list(range(2, 13)))
        self.assertIsNone(await self.service.register(event("100")))
        self.assertEqual(self.service.counts[event().key], 12)
        count = await asyncio.to_thread(self.repository._execute, "SELECT COUNT(*) FROM social_share_events")
        self.assertEqual(count[0][0], 12)

    async def test_restart_rebuild_and_permanent_history(self):
        now = time.time()
        older = event("100", occurred_at=now - 10 * 86400)
        recent = event("101", occurred_at=now - 2 * 86400)
        await asyncio.to_thread(self.repository.insert, older)
        await asyncio.to_thread(self.repository.insert, recent)
        await self.service.close()
        self.assertTrue(await self.service.rebuild())
        self.assertEqual(self.service.cache[recent.key], recent)
        rows = await asyncio.to_thread(self.repository._execute, "SELECT COUNT(*) FROM social_share_events")
        self.assertEqual(rows[0][0], 2)
        self.assertTrue(await self.service.configure("1", days=19, updated_by="42"))
        self.assertEqual(self.service.cache[older.key], older)
        self.assertEqual(self.service.effective("1"), (True, 19))

    async def test_runtime_lookup_never_checks_expiration_or_reads_database(self):
        old = event(occurred_at=time.time() - 100 * 86400)
        await self.service.register(old)
        with patch.object(self.repository, "snapshot", side_effect=AssertionError("No read on share")):
            self.assertEqual(await self.service.register(event("101")), DuplicateMatch(old, 2))

    async def test_button_removal_promotes_remaining_share_and_updates_count(self):
        first, second, third = event(), event("101"), event("102")
        for share in (first, second, third):
            await self.service.register(share)
        self.assertTrue(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        self.assertEqual(self.service.cache[first.key], second)
        self.assertEqual(self.service.counts[first.key], 2)
        self.assertTrue(await self.service.forget_previews("1", "10", [third.preview_message_id]))
        self.assertEqual(self.service.cache[first.key], second)
        self.assertEqual(self.service.counts[first.key], 1)
        self.assertEqual(await self.service.register(event("103")), DuplicateMatch(second, 2))
        self.assertTrue(await self.service.rebuild())
        self.assertEqual(self.service.cache[first.key], second)
        self.assertEqual(self.service.counts[first.key], 2)

    async def test_button_removal_is_scoped_and_does_not_remove_original_only_platforms(self):
        first = event()
        others = [event("101", post_key="Other001"), event("102", channel_id="11"),
                  event("103", guild_id="2"), event("104", platform="instagram"),
                  event("105", platform="x"), event("106", platform="youtube")]
        # Exercise scope guards even with colliding preview IDs in synthetic data.
        others = [replace(share, preview_message_id=first.preview_message_id) for share in others]
        for share in (first, *others):
            await self.service.register(share)
        self.assertTrue(await self.service.forget_previews("1", "10", ["not-present"]))
        self.assertEqual(len(self.service.cache), 7)
        self.assertTrue(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        for share in (others[1], others[2], others[4], others[5]):
            self.assertEqual(self.service.cache[share.key], share)
        self.assertNotIn(first.key, self.service.cache)
        self.assertNotIn(first.key, self.service.counts)
        self.assertTrue(await self.service.forget_previews("1", "10", [first.preview_message_id]))

    async def test_removal_keeps_runtime_window_and_unrelated_cache(self):
        now = time.time()
        unloaded = event("99", occurred_at=now - 100 * 86400)
        await asyncio.to_thread(self.repository.insert, unloaded)
        first = event("100", occurred_at=now - 6 * 86400)
        second = event("101", occurred_at=now - 5 * 86400)
        unrelated = event("102", post_key="Other001", occurred_at=now - 90 * 86400)
        for share in (first, second, unrelated):
            await self.service.register(share)
        with patch("discord_bot.features.social_preview.duplicate_service.time.time", return_value=now + 20 * 86400):
            self.assertTrue(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        self.assertEqual(self.service.cache[first.key], second)
        self.assertEqual(self.service.counts[first.key], 1)
        self.assertEqual(self.service.cache[unrelated.key], unrelated)
        self.assertEqual(self.service.counts[unrelated.key], 1)

    async def test_removal_while_disabled_is_persistent_and_updates_cross_channel_match(self):
        first, other_channel = event(), event("101", channel_id="11")
        for share in (first, other_channel):
            await self.service.register(share)
        self.service.text_channel_ids = lambda guild: {"10", "11"}
        self.assertTrue(await self.service.configure_cross_channel(
            "1", enabled=True, channel_ids={"10", "11"}, updated_by="42"))
        self.assertTrue(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        self.assertEqual(await self.service.register(event("102")), DuplicateMatch(other_channel, 2))
        self.assertTrue(await self.service.configure("1", state="disabled", updated_by="42"))
        self.assertTrue(await self.service.forget_previews("1", "11", [other_channel.preview_message_id]))
        self.assertTrue(await self.service.configure("1", state="enabled", updated_by="42"))
        self.assertEqual(self.service.cache[first.key].source_message_id, "102")
        self.assertNotIn(other_channel.key, self.service.cache)

    async def test_removal_database_failures_degrade_without_false_success(self):
        first = event()
        await self.service.register(first)
        with patch.object(self.repository, "delete_previews", side_effect=OSError("synthetic failure")):
            self.assertFalse(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        self.assertFalse(self.service.ready)
        self.assertEqual(self.service.cache[first.key], first)
        self.assertTrue(await self.service.rebuild())
        with patch.object(self.repository, "remaining_shares", side_effect=OSError("synthetic failure")):
            self.assertFalse(await self.service.forget_previews("1", "10", [first.preview_message_id]))
        self.assertFalse(self.service.ready)
        self.assertTrue(await self.service.rebuild())
        self.assertNotIn(first.key, self.service.cache)
        self.assertNotIn(first.key, self.service.counts)

    async def test_registration_and_removal_share_the_service_lock(self):
        first, second = event(), event("101")
        await self.service.register(first)
        await asyncio.gather(
            self.service.forget_previews("1", "10", [first.preview_message_id]),
            self.service.register(second))
        self.assertEqual(self.service.cache[first.key], second)
        self.assertEqual(self.service.counts[first.key], 1)
        self.assertEqual(await self.service.register(event("102")), DuplicateMatch(second, 2))

    async def test_disable_env_override_and_restore_defaults(self):
        os.environ["SOCIAL_DUPLICATE_ENABLED"] = "0"
        await self.service.rebuild()
        self.assertIsNone(await self.service.register(event()))
        self.assertFalse(self.service.cache)
        self.assertTrue(await self.service.configure("1", state="enabled", days=3, updated_by="42"))
        await self.service.register(event())
        self.assertTrue(self.service.cache)
        self.assertTrue(await self.service.configure("1", state="disabled", updated_by="42"))
        self.assertFalse(self.service.cache)
        await self.service.register(event("101"))
        self.assertTrue(await self.service.configure("1", state="default", updated_by="42"))
        self.assertEqual(self.service.effective("1"), (False, 7))
        rows = await asyncio.to_thread(self.repository._execute, "SELECT COUNT(*) FROM social_share_events")
        self.assertEqual(rows[0][0], 1)

    async def test_change_one_guild_does_not_expire_another_guild_runtime_cache(self):
        old = event(guild_id="2", occurred_at=time.time() - 100 * 86400)
        await self.service.register(old)
        await self.service.configure("1", days=2, updated_by="42")
        self.assertEqual(self.service.cache[old.key], old)

    async def test_persisted_settings_survive_new_service(self):
        await self.service.configure("1", state="enabled", days=23, updated_by="42")
        await self.service.close()
        self.service = SocialDuplicateService(self.repository)
        await self.service.rebuild()
        self.assertEqual(self.service.effective("1"), (True, 23))

    async def test_each_guild_loads_its_own_window_and_shortening_rebuilds(self):
        old = event(occurred_at=time.time() - 10 * 86400)
        other = replace(old, guild_id="2")
        await asyncio.to_thread(self.repository.insert, old)
        await asyncio.to_thread(self.repository.insert, other)
        await self.service.configure("1", days=11, updated_by="42")
        await self.service.rebuild()
        self.assertIn(old.key, self.service.cache)
        self.assertNotIn(other.key, self.service.cache)
        await self.service.configure("1", days=2, updated_by="42")
        self.assertNotIn(old.key, self.service.cache)

    async def test_repository_load_uses_time_index_and_reconnects_after_closed_connection(self):
        rows = await asyncio.to_thread(self.repository._execute,
            "EXPLAIN QUERY PLAN SELECT * FROM social_share_events WHERE occurred_at >= ?", (0,))
        self.assertIn("idx_social_share_events_time", str(rows))
        await asyncio.to_thread(self.repository.conn.close)
        self.assertIsNone(await self.service.register(event()))
        self.assertFalse(self.service.ready)
        self.assertTrue(await self.service.rebuild())
        self.assertIsNone(await self.service.register(event("101")))

    async def test_database_failure_degrades_and_recovery_reloads_history(self):
        first = event()
        await self.service.register(first)
        with patch.object(self.repository, "insert", side_effect=OSError("synthetic failure")):
            self.assertIsNone(await self.service.register(event("101")))
        self.assertFalse(self.service.ready)
        self.assertIsNone(await self.service.register(event("102")))
        await self.service.rebuild()
        self.assertTrue(self.service.ready)
        self.assertEqual(await self.service.register(event("103")), DuplicateMatch(first, 2))

    async def test_share_counts_are_per_post_channel_guild_and_survive_restart(self):
        first = event()
        await self.service.register(first)
        second = await self.service.register(event("101"))
        third = await self.service.register(event("102"))
        self.assertEqual((second.share_count, third.share_count), (2, 3))
        self.assertEqual(second.share_count, 2)  # A match is a snapshot, not a live counter.
        for different in (event("103", post_key="Another001"), event("104", channel_id="11"),
                          event("105", guild_id="2"), event("106", platform="instagram")):
            self.assertIsNone(await self.service.register(different))
            self.assertEqual(self.service.counts[different.key], 1)
        await self.service.close()
        await self.service.rebuild()
        fourth = await self.service.register(event("107"))
        self.assertEqual(fourth, DuplicateMatch(first, 4))

    async def test_rebuild_counts_only_loaded_window_and_keeps_other_guild_counts(self):
        old = event("100", occurred_at=time.time() - 10 * 86400)
        recent = event("101", occurred_at=time.time() - 86400)
        for record in (old, recent, event("102", guild_id="2")):
            await self.service.register(record)
        await self.service.register(event("103", guild_id="2"))
        self.assertEqual(self.service.counts[old.key], 2)
        await self.service.configure("1", days=2, updated_by="42")
        self.assertEqual(self.service.counts[old.key], 1)
        self.assertEqual(self.service.counts[event(guild_id="2").key], 2)
        await self.service.configure("1", days=19, updated_by="42")
        self.assertEqual(self.service.counts[old.key], 2)
        self.assertEqual(await self.service.register(event("104")), DuplicateMatch(old, 3))

    async def test_failed_load_or_settings_write_never_reports_success(self):
        with patch.object(self.repository, "snapshot", side_effect=OSError("synthetic failure")):
            self.assertFalse(await self.service.rebuild())
        self.assertFalse(self.service.ready)
        await self.service.rebuild()
        with patch.object(self.repository, "configure", side_effect=OSError("synthetic failure")):
            self.assertFalse(await self.service.configure("1", days=3, updated_by="42"))
        self.assertFalse(self.service.ready)

    async def test_invalid_days_and_default_combination(self):
        for days in (0, -1, True, 2.5, 2147483648):
            with self.subTest(days=days), self.assertRaises(ValueError):
                await self.service.configure("1", days=days, updated_by="42")
        with self.assertRaises(ValueError):
            await self.service.configure("1", state="default", days=5, updated_by="42")
        os.environ["SOCIAL_DUPLICATE_WINDOW_DAYS"] = "invalid"
        self.assertTrue(await self.service.rebuild())
        self.assertEqual(self.service.default_days, 7)

    async def test_recovery_task_only_reloads_when_degraded(self):
        calls = 0

        async def sleep(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError

        with patch("discord_bot.features.social_preview.duplicate_service.asyncio.sleep", side_effect=sleep), \
                patch.object(self.service, "rebuild", return_value=True) as rebuild:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.recover()
            rebuild.assert_not_called()


class DuplicatePostgresTests(unittest.TestCase):
    def test_postgres_sql_placeholder_transaction_and_connection_reuse(self):
        cursor = Mock()
        cursor.rowcount = 1
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.cursor.return_value = cursor
        driver = types.SimpleNamespace(connect=Mock(return_value=connection))
        repository = SocialDuplicateRepository()
        with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": "postgresql://test.invalid/test"}), \
                patch.dict("sys.modules", {"psycopg2": driver}):
            self.assertTrue(repository.insert(event()))
            repository.configure("1", (True, 11), updated_by="42")
            repository.configure_cross_channel("1", enabled=True, channel_ids={"10", "11"}, updated_by="42")
            cursor.fetchall.return_value = [("1", 1, '["10", "11"]')]
            self.assertEqual(repository.cross_channel_settings(), {"1": (True, frozenset({"10", "11"}))})
            cursor.fetchall.return_value = [tuple(getattr(event(), field) for field in ShareEvent.__dataclass_fields__)]
            removed = repository.delete_previews("1", "10", ["1100", "1101"])
            self.assertEqual(len(removed), 1)
            sql, params = cursor.execute.call_args.args
            self.assertIn("preview_message_id IN (%s, %s)", sql)
            self.assertIn("RETURNING", sql)
            self.assertEqual(params, ("1", "10", "1100", "1101"))
            self.assertEqual(len(repository.delete_original_messages("1", "10", ["100", "101"])), 1)
            sql, params = cursor.execute.call_args.args
            self.assertIn("source_message_id IN (%s, %s)", sql)
            self.assertIn("platform IN ('x', 'youtube')", sql)
            self.assertNotIn("preview_message_id IN", sql)
            self.assertEqual(params, ("1", "10", "100", "101"))
            with patch.object(repository, "_execute") as execute:
                self.assertEqual(repository.delete_original_messages("1", "10", []), [])
                execute.assert_not_called()
            self.assertEqual(len(repository.remaining_shares(event().key, since=0)), 1)
            driver.connect.assert_called_once_with("postgresql://test.invalid/test", connect_timeout=5)
            self.assertEqual(repository.placeholder, "%s")
            for call in cursor.execute.call_args_list:
                self.assertNotIn("?", call.args[0])
            self.assertEqual(connection.commit.call_count, 7)
            repository.close()

    def test_failed_initialization_cools_down_and_recovers(self):
        repository = SocialDuplicateRepository(retry_interval_seconds=30)
        with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": ""}):
            with self.assertRaises(RuntimeError):
                repository.snapshot(enabled=True, days=7, now=time.time())
            with patch.object(repository, "_last_failure", time.monotonic()), \
                    self.assertRaisesRegex(RuntimeError, "cooling down"):
                repository.snapshot(enabled=True, days=7, now=time.time())
