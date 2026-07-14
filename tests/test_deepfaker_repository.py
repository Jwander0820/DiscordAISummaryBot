import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.db.deepfaker_repository import DeepFakerRepository
from discord_bot.db.schema import DEEPFAKER_EVENT_COLUMNS


def make_event_record() -> dict:
    return {
        "guild_id": "1001",
        "guild_name": "測試伺服器",
        "channel_id": "2002",
        "channel_name": "敏感頻道",
        "actor_user_id": "3003",
        "actor_username": "actor_account",
        "actor_global_name": "操作者",
        "actor_display_name": "操作者暱稱",
        "actor_is_bot": False,
        "target_user_id": "4004",
        "target_username": "target_bot",
        "target_global_name": None,
        "target_display_name": "目標機器人",
        "target_is_bot": True,
        "outcome_success": False,
        "failure_probability": 0.05,
        "random_roll": 0.01,
        "requested_content": "原始台詞",
        "webhook_content": "失敗後實際送出的台詞",
        "failure_notice": "抓到你了！",
        "failure_exposed_content": "揭露操作者的台詞",
        "delivery_status": "sent",
        "occurred_at": "2026-07-14T12:00:00+00:00",
    }


class DeepfakerRepositoryTests(unittest.TestCase):
    def test_insert_event_persists_complete_record_to_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "deepfaker.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = DeepFakerRepository()
                record = make_event_record()

                self.assertTrue(repository.insert_event(record))

                connection = sqlite3.connect(sqlite_path)
                try:
                    row = connection.execute(
                        f"SELECT {','.join(DEEPFAKER_EVENT_COLUMNS)} FROM deepfaker_events"
                    ).fetchone()
                    indexes = connection.execute("PRAGMA index_list('deepfaker_events')").fetchall()
                finally:
                    connection.close()
                    repository.conn.close()

                expected = tuple(
                    int(value) if column in {"actor_is_bot", "target_is_bot", "outcome_success"} else value
                    for column, value in ((column, record[column]) for column in DEEPFAKER_EVENT_COLUMNS)
                )
                self.assertEqual(row, expected)
                self.assertEqual(len(indexes), 3)

    def test_insert_event_rejects_incomplete_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "deepfaker.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = DeepFakerRepository()
                record = make_event_record()
                del record["target_user_id"]

                self.assertFalse(repository.insert_event(record))
                repository.conn.close()


if __name__ == "__main__":
    unittest.main()
