"""Cancellation barriers and independently owned audit connection regression tests."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from discord_bot.db.social_duplicate_repository import SocialDuplicateRepository
from discord_bot.features.social_preview.duplicate_service import SocialDuplicateService
from tests.test_social_duplicate_service import event


class ConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_insert_and_delete_hold_lock_until_worker_finishes(self):
        for operation in ("insert", "delete"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(directory) / "test.db")}):
                    repo = SocialDuplicateRepository()
                    service = SocialDuplicateService(repo)
                    await service.rebuild()
                    share = event(platform="youtube")
                    if operation == "delete":
                        await service.register(share)
                    started, release = threading.Event(), threading.Event()
                    original = repo.insert if operation == "insert" else repo.delete_original_messages

                    def blocked(*args):
                        started.set()
                        if not release.wait(5):
                            raise RuntimeError("test barrier timed out")
                        return original(*args)

                    method = "insert" if operation == "insert" else "delete_original_messages"
                    try:
                        with patch.object(repo, method, side_effect=blocked):
                            task = asyncio.create_task(service.register(share) if operation == "insert" else
                                service.forget_original_messages("1", "10", [share.source_message_id]))
                            self.assertTrue(await asyncio.to_thread(started.wait, 3))
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(service.ready)
                            recovery = asyncio.create_task(service.rebuild())
                            await asyncio.sleep(0)
                            self.assertFalse(recovery.done())
                            task.cancel()  # repeated cancellation must not release the lock
                            await asyncio.sleep(0)
                            self.assertFalse(recovery.done())
                            release.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            self.assertTrue(await recovery)
                            self.assertEqual(len(service.cache), 1 if operation == "insert" else 0)
                    finally:
                        release.set()
                        await service.close()

    async def test_audit_sql_failure_does_not_close_history_connection_or_start_its_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(directory) / "test.db")}):
                service = SocialDuplicateService()
                try:
                    await service.rebuild()
                    await service.register(event())
                    connection = service.repository.conn
                    audit = service.patrol_repository.storage
                    await asyncio.to_thread(audit.execute, "SELECT 1")
                    self.assertIsNot(connection, audit.conn)
                    with self.assertRaises(Exception):
                        await asyncio.to_thread(audit.execute, "SELECT missing FROM missing_table")
                    self.assertIs(service.repository.conn, connection)
                    match = await service.register(event("101"))
                    self.assertEqual(match.share_count, 2)
                    self.assertTrue(service.ready)
                finally:
                    await service.close()
                self.assertIsNone(service.patrol_repository.storage.conn)
