import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discord_bot.db.schema import SUMMARY_COLUMNS


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
            with sqlite3.connect(sqlite_path) as conn:
                selected = conn.execute(
                    f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM summaries"
                ).fetchone()

            self.assertEqual(selected, row)

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
            sync_tool, "fetch_summaries", return_value=fake_rows
        ) as fetch_summaries, patch.object(sync_tool, "write_summaries_to_sqlite") as write_summaries:
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
        fetch_summaries.assert_called_once_with("postgresql://demo", min_id=None, limit=None)
        write_summaries.assert_not_called()


if __name__ == "__main__":
    unittest.main()
