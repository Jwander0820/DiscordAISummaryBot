import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from decimal import Decimal
from contextlib import closing
from uuid import UUID

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
                    "--table", "history",
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
                        "--table", "history",
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


class AllTableExportTests(unittest.TestCase):
    def source(self, fail=False):
        source = MagicMock()
        metadata = MagicMock()
        metadata.fetchall.return_value = [('public', 'guild_settings'), ('audit', 'odd"table')]
        cursors = []
        for columns, batches in [
            (['guild_id', 'settings', 'amount'], [[('42', {'enabled': True}, Decimal('1.234567890123456789'))], []]),
            (['key'], [[],]),
        ]:
            cursor = MagicMock()
            cursor.description = [(column,) for column in columns]
            cursor.fetchmany.side_effect = batches
            cursors.append(cursor)
        if fail:
            cursors[1].execute.side_effect = RuntimeError('private-error')
        contexts = []
        for cursor in [metadata, *cursors]:
            context = MagicMock()
            context.__enter__.return_value = cursor
            contexts.append(context)
        source.cursor.side_effect = contexts
        return source

    def test_all_schemas_empty_tables_json_decimal_and_readonly_snapshot(self):
        source = self.source()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'snapshot.db'
            with patch('psycopg2.connect', return_value=source):
                counts = sync_tool.export_all_tables('private-test', output)
            self.assertEqual(counts, {'guild_settings': 1, 'audit.odd"table': 0})
            with closing(sqlite3.connect(output)) as conn:
                self.assertEqual(conn.execute('SELECT * FROM guild_settings').fetchone(),
                                 ('42', '{"enabled": true}', '1.234567890123456789'))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM "audit.odd""table"').fetchone(), (0,))
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
        source.set_session.assert_called_once_with(isolation_level='REPEATABLE READ', readonly=True, autocommit=False)
        source.close.assert_called_once()

    def test_mid_export_failure_never_publishes_partial_file(self):
        source = self.source(fail=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'snapshot.db'
            with patch('psycopg2.connect', return_value=source):
                with self.assertRaises(RuntimeError):
                    sync_tool.export_all_tables('private-test', output)
            self.assertEqual(list(Path(directory).iterdir()), [])
        source.close.assert_called_once()

    def test_existing_output_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'snapshot.db'
            output.write_bytes(b'existing-backup')
            with patch('psycopg2.connect') as connect:
                with self.assertRaises(ValueError):
                    sync_tool.export_all_tables('private-test', output)
                connect.assert_not_called()
            self.assertEqual(output.read_bytes(), b'existing-backup')

    def test_dry_run_does_not_create_directory_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'absent' / 'snapshot.db'
            with patch('psycopg2.connect', return_value=self.source()):
                counts = sync_tool.export_all_tables('private-test', output, dry_run=True)
            self.assertEqual(counts['guild_settings'], 1)
            self.assertFalse(output.parent.exists())

    def test_default_cli_routes_to_all_tables(self):
        with patch.object(sync_tool, 'load_local_env'), patch.object(
            sync_tool, 'export_all_tables', return_value={'new_table': 2}
        ) as export:
            self.assertEqual(sync_tool.main(['--database-url', 'private-test', '--dry-run']), 0)
        self.assertEqual(export.call_args.kwargs, {'dry_run': True, 'limit': None})

    def test_extra_values_are_sqlite_compatible(self):
        self.assertEqual(sync_tool._coerce_sqlite_value(memoryview(b'abc')), b'abc')
        self.assertEqual(sync_tool._coerce_sqlite_value(2**80), str(2**80))
        value = UUID('00000000-0000-0000-0000-000000000001')
        self.assertEqual(sync_tool._coerce_sqlite_value(value), str(value))

    def test_configured_existing_default_gets_new_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'previous.db'
            output.write_bytes(b'existing')
            with patch.dict('os.environ', {'LOCAL_BACKUP_SQLITE_PATH': str(output)}):
                args = sync_tool.parse_args([])
                self.assertNotEqual(Path(args.output), output)
                self.assertEqual(Path(args.output).parent, output.parent)
                explicit = sync_tool.parse_args(['--output', str(output)])
                self.assertEqual(Path(explicit.output), output)

    def test_all_incremental_is_rejected_before_connecting(self):
        with patch.object(sync_tool, 'load_local_env'), patch.object(sync_tool, 'export_all_tables') as export:
            self.assertEqual(sync_tool.main(['--database-url', 'private-test', '--mode', 'incremental']), 2)
        export.assert_not_called()

    def test_raw_export_and_legacy_errors_do_not_leak_secrets(self):
        for table, helper in [('all', 'export_all_tables'), ('history', 'fetch_table')]:
            with self.subTest(table=table):
                error_output = io.StringIO()
                with patch.object(sync_tool, 'load_local_env'), patch.object(
                    sync_tool, helper, side_effect=ValueError('fixture-password-and-host')
                ), patch('sys.stderr', error_output):
                    result = sync_tool.main(['--database-url', 'private-test', '--table', table, '--dry-run'])
                self.assertEqual(result, 1)
                self.assertNotIn('fixture-password-and-host', error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
