"""Offline event snapshots, SQL portability, and delivery outcome accounting."""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from discord_bot.db.schema import SOCIAL_PATROL_EVENT_COLUMNS
from discord_bot.db.social_duplicate_repository import ShareEvent, SocialDuplicateRepository
from discord_bot.db.social_patrol_repository import SocialPatrolRepository
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview.duplicate_service import DuplicateMatch, SocialDuplicateService
from discord_bot.features.social_preview.patrol_records import build_patrol_event, delivered, PatrolDelivery


def source(number=101):
    return SimpleNamespace(id=number, guild=SimpleNamespace(id=1, name="範例伺服器"),
        channel=SimpleNamespace(id=10, name="範例頻道"),
        author=SimpleNamespace(id=42, bot=False, name="example_user", global_name="範例名字", display_name="當時暱稱"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


def share(number=101, platform="threads"):
    return ShareEvent("1", "10", platform, str(number), "42", "Demo001",
                      "https://example.com/post/Demo001", str(number + 1000), 1.0)


def pending():
    return build_patrol_event(source(), share(), DuplicateMatch(share(100), 2))


class SnapshotTests(unittest.TestCase):
    def test_identity_snapshots_and_distinct_message_ids(self):
        message = source()
        record = build_patrol_event(message, share(), DuplicateMatch(share(100), 2))
        self.assertEqual(set(record), set(SOCIAL_PATROL_EVENT_COLUMNS))
        self.assertEqual(record["user_id"], "42")
        self.assertEqual(record["username"], "example_user")
        self.assertEqual(record["display_name"], "當時暱稱")
        self.assertEqual(record["guild_name"], "範例伺服器")
        self.assertEqual(record["channel_name"], "範例頻道")
        self.assertEqual(record["source_message_id"], "101")
        self.assertEqual(record["current_preview_message_id"], "1101")
        self.assertEqual(record["first_source_message_id"], "100")
        self.assertEqual(record["reference_message_id"], "1100")
        self.assertEqual(record["source_message_created_at"], message.created_at.timestamp())
        message.author.display_name = "後來暱稱"
        self.assertEqual(record["display_name"], "當時暱稱")

    def test_unavailable_names_and_source_timestamp_remain_null(self):
        message = source()
        message.author = SimpleNamespace(id=42)
        message.guild = SimpleNamespace(id=1)
        message.channel = SimpleNamespace(id=10)
        message.created_at = None
        record = build_patrol_event(message, share(), DuplicateMatch(share(100), 2))
        for name in ("username", "global_name", "display_name", "guild_name", "channel_name", "source_message_created_at"):
            self.assertIsNone(record[name])

    def test_delivery_ids_never_store_mock_representations(self):
        self.assertEqual(delivered("webhook", SimpleNamespace(id=900)), PatrolDelivery("webhook", "900"))
        self.assertIsNone(delivered("webhook", Mock()).message_id)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": ":memory:"})
        self.env.start()
        self.storage = SocialDuplicateRepository(retry_interval_seconds=0)
        self.repository = SocialPatrolRepository(self.storage)

    def tearDown(self):
        self.storage.close()
        self.env.stop()

    def test_sqlite_insert_dedup_finalize_and_immutable_snapshot(self):
        record = pending()
        self.assertTrue(self.repository.insert_event(record))
        self.assertFalse(self.repository.insert_event({**record, "event_id": "different", "display_name": "新名字"}))
        self.assertTrue(self.repository.finish_event(record["event_id"], status="sent", method="webhook",
                                                     message_id="900", completed_at=2.0))
        self.assertFalse(self.repository.finish_event(record["event_id"], status="failed", completed_at=3.0))
        rows = self.storage._execute("SELECT display_name, delivery_status, patrol_message_id FROM social_patrol_events")
        self.assertEqual(rows, [("當時暱稱", "sent", "900")])

    def test_new_table_does_not_change_existing_share_history(self):
        self.assertTrue(self.storage.insert(share()))
        self.assertTrue(self.repository.insert_event(pending()))
        self.assertEqual(self.storage._execute("SELECT COUNT(*) FROM social_share_events"), [(1,)])
        self.assertEqual(self.storage._execute("SELECT COUNT(*) FROM social_patrol_events"), [(1,)])

    def test_schema_and_outcome_validation(self):
        with self.assertRaises(ValueError):
            self.repository.insert_event({"user_id": "42"})
        with self.assertRaises(ValueError):
            self.repository.finish_event("event", status="invented", completed_at=1.0)
        with self.assertRaises(ValueError):
            self.repository.finish_event("event", status="sent", method="invented", completed_at=1.0)

    def test_postgres_schema_placeholders_and_commit(self):
        cursor = Mock(rowcount=1)
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.cursor.return_value = cursor
        driver = SimpleNamespace(connect=Mock(return_value=connection))
        with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": "postgresql://test.invalid/test"}), \
                patch.dict("sys.modules", {"psycopg2": driver}):
            record = {**pending(), "display_name": "O'Brien"}
            self.assertTrue(self.repository.insert_event(record))
            self.assertTrue(self.repository.finish_event(record["event_id"], status="sent", method="bot_reply",
                                                         message_id="900", completed_at=2.0))
        sqls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS social_patrol_events" in sql for sql in sqls))
        self.assertTrue(any("%s" in sql and "ON CONFLICT" in sql for sql in sqls))
        self.assertTrue(all("?" not in sql and "O'Brien" not in sql for sql in sqls))
        self.assertEqual(connection.commit.call_count, 2)
        driver.connect.assert_called_once()
        self.assertEqual(sum("ADD COLUMN IF NOT EXISTS taunt_" in sql for sql in sqls), 2)

    def test_legacy_sqlite_migration_is_idempotent_and_preserves_null_history(self):
        from discord_bot.db.schema import CREATE_SOCIAL_PATROL_TABLES_SQL
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.db")
            old = {k: v for k, v in pending().items() if k not in {"taunt_text", "taunt_source", "reference_channel_id"}}
            with sqlite3.connect(path) as conn:
                conn.execute(CREATE_SOCIAL_PATROL_TABLES_SQL[0].replace("taunt_text TEXT, taunt_source TEXT, reference_channel_id TEXT,", ""))
                conn.execute(f"INSERT INTO social_patrol_events ({', '.join(old)}) VALUES ({', '.join('?' for _ in old)})", tuple(old.values()))
            conn.close()
            self.storage.close()
            with patch.dict(os.environ, {"SQLITE_PATH": path}):
                self.assertEqual(self.storage._execute("SELECT display_name, taunt_text, taunt_source FROM social_patrol_events"),
                                 [("當時暱稱", None, None)])
                self.storage.close()
                self.assertEqual(self.storage._execute("SELECT COUNT(*) FROM social_patrol_events"), [(1,)])
                columns = [r[1] for r in self.storage._execute("PRAGMA table_info(social_patrol_events)")]
                self.assertEqual(columns.count("taunt_text"), 1)
                self.assertEqual(columns.count("reference_channel_id"), 1)
                self.assertEqual(self.storage._execute("SELECT reference_channel_id FROM social_patrol_events"), [(None,)])
                self.storage.close()


class AuditFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": ":memory:",
                                           "SOCIAL_DUPLICATE_ENABLED": "1"})
        self.env.start()
        self.storage = SocialDuplicateRepository(retry_interval_seconds=0)
        self.service = SocialDuplicateService(self.storage)
        await self.service.rebuild()
        self.service_patch = patch.object(patrol, "social_duplicate_service", self.service)
        self.service_patch.start()

    async def asyncTearDown(self):
        self.service_patch.stop()
        await self.service.close()
        self.env.stop()

    async def rows(self):
        return await asyncio.to_thread(self.storage._execute,
            "SELECT platform, delivery_status, delivery_method, patrol_message_id, error_type "
            "FROM social_patrol_events ORDER BY platform")

    async def test_all_platforms_only_duplicates_record_pending_before_send_then_sent(self):
        cases = [("threads", "https://www.threads.com/@demo/post/Demo001"),
                 ("instagram", "https://www.instagram.com/p/Demo001/"),
                 ("facebook", "https://www.facebook.com/demo/posts/123"),
                 ("x", "https://x.com/demo/status/123")]
        async def send(message, previous, **kwargs):
            record = await asyncio.to_thread(self.storage._execute,
                "SELECT delivery_status FROM social_patrol_events WHERE platform = ?", (previous.platform,))
            self.assertEqual(record, [("pending",)])
            return PatrolDelivery("webhook", "900")
        with patch.object(patrol, "_announce", AsyncMock(side_effect=send)) as announce:
            for platform, url in cases:
                for number in (100, 101, 101):
                    if platform == "x":
                        await patrol.record_original_share(source(number), platform=platform, url=url)
                    else:
                        await patrol.record_delivered_preview(source(number), platform=platform, url=url,
                            sent_messages=[SimpleNamespace(id=number + 1000)])
            self.assertEqual(announce.await_count, 4)
        rows = await self.rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row[1:] == ("sent", "webhook", "900", None) for row in rows))

    async def invoke(self, announce):
        with patch.object(patrol, "_announce", announce):
            await patrol._announce_recorded(source(), share(), DuplicateMatch(share(100), 2))

    async def test_failed_delivery_retains_snapshot_and_only_safe_error_type(self):
        with self.assertRaises(RuntimeError):
            await self.invoke(AsyncMock(side_effect=RuntimeError("secret-body-and-token")))
        rows = await self.rows()
        self.assertEqual(rows[0][1:], ("failed", None, None, "RuntimeError"))

    async def test_timeout_and_cancellation_are_unknown(self):
        for error in (asyncio.TimeoutError, asyncio.CancelledError):
            with self.subTest(error=error):
                await asyncio.to_thread(self.storage._execute, "DELETE FROM social_patrol_events", write=True)
                with self.assertRaises(error):
                    await self.invoke(AsyncMock(side_effect=error()))
                self.assertEqual((await self.rows())[0][1], "unknown")

    async def test_insert_failure_does_not_suppress_discord_send(self):
        announce = AsyncMock(return_value=PatrolDelivery("webhook", "900"))
        with patch.object(self.service.patrol_repository, "insert_event", side_effect=OSError("secret")), \
                patch.object(self.service.patrol_repository, "finish_event") as finish:
            await self.invoke(announce)
            announce.assert_awaited_once()
            finish.assert_not_called()
        self.assertEqual(await self.rows(), [])

    async def test_finish_failure_leaves_pending_without_replaying_send(self):
        announce = AsyncMock(return_value=PatrolDelivery("bot_notice", "901"))
        with patch.object(self.service.patrol_repository, "finish_event", side_effect=OSError("secret")):
            await self.invoke(announce)
        announce.assert_awaited_once()
        self.assertEqual((await self.rows())[0][1], "pending")

    async def test_bot_fallback_delivery_can_be_counted_separately(self):
        await self.invoke(AsyncMock(return_value=PatrolDelivery("bot_reply", "901")))
        self.assertEqual((await self.rows())[0][1:4], ("sent", "bot_reply", "901"))

    async def test_generation_finishes_before_send_and_actual_taunt_is_saved(self):
        from discord_bot.features.social_preview.patrol_taunts import PatrolTaunt
        order = []
        async def generate(count, name):
            order.append("generate")
            await asyncio.sleep(0)
            self.assertEqual(name, "當時暱稱")
            return PatrolTaunt("第 8 次了！範例生成吐槽。", "gemini")
        async def send(*args, **kwargs):
            self.assertEqual(order, ["generate"])
            self.assertEqual(kwargs["taunt_text"], "第 8 次了！範例生成吐槽。")
            order.append("send")
            return PatrolDelivery("webhook", "900")
        with patch.object(patrol, "choose_taunt", side_effect=generate), patch.object(patrol, "_announce", side_effect=send):
            await patrol._announce_recorded(source(), share(), DuplicateMatch(share(100), 8))
        self.assertEqual(order, ["generate", "send"])
        rows = await asyncio.to_thread(self.storage._execute, "SELECT taunt_text, taunt_source FROM social_patrol_events")
        self.assertEqual(rows, [("第 8 次了！範例生成吐槽。", "gemini")])
