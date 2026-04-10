import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.db.repository import SummaryRepository


class SummaryRepositoryTests(unittest.TestCase):
    def test_init_falls_back_to_sqlite_for_invalid_db_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "fallback.db")
            with patch.dict(os.environ, {"DB_TYPE": "mystery", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = SummaryRepository()
                repository.init()

                self.assertEqual(repository.db_type, "sqlite")
                self.assertTrue(repository.db_enabled)
                self.assertIsNotNone(repository.conn)
                repository.conn.close()

    def test_insert_summary_persists_record_to_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "summaries.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = SummaryRepository()
                record = {
                    "channel_id": "general",
                    "user_id": "alice",
                    "command": "解答之書",
                    "question": "問題",
                    "prompt": "prompt",
                    "summary": "summary",
                    "call_time": "2026-04-10T10:00:00+08:00",
                }

                repository.insert_summary(record)

                with sqlite3.connect(sqlite_path) as conn:
                    row = conn.execute(
                        "SELECT channel_id, user_id, command, question, prompt, summary, call_time FROM summaries"
                    ).fetchone()

                self.assertEqual(row, tuple(record.values()))
                repository.conn.close()

    def test_postgres_without_database_url_disables_writes(self):
        with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": ""}, clear=False):
            repository = SummaryRepository()
            repository.init()

            self.assertFalse(repository.db_enabled)
            self.assertIsNone(repository.conn)


if __name__ == "__main__":
    unittest.main()
