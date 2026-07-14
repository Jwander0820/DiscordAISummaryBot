import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from discord_bot.db.schema import DEEPFAKER_EVENT_COLUMNS, SUMMARY_COLUMNS


def load_sync_tool_module():
    module_path = Path(__file__).resolve().parents[1] / "tools" / "sync_postgres_summaries_to_sqlite.py"
    spec = importlib.util.spec_from_file_location("sync_postgres_summaries_to_sqlite", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync_tool = load_sync_tool_module()


class SyncPostgresSummariesToolTests(unittest.TestCase):
    def test_write_summaries_to_sqlite_creates_expected_table_and_rows(self):
        row = (
            1,
            "general",
            "alice",
            "解答之書",
            "問題",
            "prompt",
            "summary",
            "2026-04-10T10:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "backup.db"

            written = sync_tool.write_summaries_to_sqlite([row], sqlite_path, mode="upsert")

            self.assertEqual(written, 1)
            conn = sqlite3.connect(sqlite_path)
            try:
                selected = conn.execute(
                    f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM summaries"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(selected, row)

    def test_write_all_tables_preserves_deepfaker_channel_name_and_bot_target(self):
        summary_row = (
            1,
            "general",
            "alice",
            "deepfaker",
            "",
            "prompt",
            "summary",
            "2026-07-14T12:00:00+00:00",
        )
        deepfaker_row = (
            7,
            "1001",
            "測試伺服器",
            "2002",
            "敏感頻道原名",
            "3003",
            "actor_account",
            "操作者",
            "操作者暱稱",
            False,
            "4004",
            "target_bot",
            None,
            "目標機器人",
            True,
            False,
            0.05,
            0.01,
            "原始台詞",
            "失敗後實際送出的內容",
            "抓到你了！",
            "揭露操作者的台詞",
            "sent",
            "2026-07-14T12:00:00+00:00",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "backup.db"
            written = sync_tool.write_tables_to_sqlite(
                {
                    "summaries": [summary_row],
                    "deepfaker_events": [deepfaker_row],
                },
                sqlite_path,
                mode="upsert",
            )

            conn = sqlite3.connect(sqlite_path)
            try:
                selected = conn.execute(
                    f"SELECT id, {', '.join(DEEPFAKER_EVENT_COLUMNS)} FROM deepfaker_events"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(written, {"summaries": 1, "deepfaker_events": 1})
            self.assertEqual(selected, deepfaker_row)
            self.assertEqual(selected[4], "敏感頻道原名")
            self.assertEqual(selected[14], 1)

    def test_main_dry_run_uses_database_url_from_argument(self):
        fake_rows = [
            (
                2,
                "general",
                "bob",
                "測試d-mail",
                "",
                "prompt",
                "summary",
                "2026-04-10T11:00:00+08:00",
            )
        ]

        with patch.object(sync_tool, "load_local_env") as load_local_env, patch.object(
            sync_tool, "fetch_table", return_value=fake_rows
        ) as fetch_table, patch.object(sync_tool, "write_tables_to_sqlite") as write_tables:
            exit_code = sync_tool.main(
                [
                    "--database-url",
                    "postgresql://demo",
                    "--output",
                    "dummy.db",
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 0)
        load_local_env.assert_called_once()
        self.assertEqual(
            fetch_table.call_args_list,
            [
                call("postgresql://demo", "summaries", min_id=None, limit=None),
                call("postgresql://demo", "deepfaker_events", min_id=None, limit=None),
            ],
        )
        write_tables.assert_not_called()

    def test_incremental_mode_uses_each_tables_own_local_max_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "backup.db"
            sync_tool.write_tables_to_sqlite(
                {
                    "summaries": [
                        (10, "general", "alice", "cmd", "", "", "", "2026-07-14T12:00:00+00:00")
                    ],
                    "deepfaker_events": [
                        (
                            3, "1", "guild", "2", "channel", "3", "actor", None, "Actor", False,
                            "4", "target", None, "Target", True, True, 0.05, 0.8, "text", "text",
                            None, None, "sent", "2026-07-14T12:00:00+00:00",
                        )
                    ],
                },
                sqlite_path,
                mode="upsert",
            )

            with patch.object(sync_tool, "load_local_env"), patch.object(
                sync_tool, "fetch_table", return_value=[]
            ) as fetch_table:
                exit_code = sync_tool.main(
                    [
                        "--database-url",
                        "postgresql://demo",
                        "--output",
                        str(sqlite_path),
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                fetch_table.call_args_list,
                [
                    call("postgresql://demo", "summaries", min_id=10, limit=None),
                    call("postgresql://demo", "deepfaker_events", min_id=3, limit=None),
                ],
            )


if __name__ == "__main__":
    unittest.main()
