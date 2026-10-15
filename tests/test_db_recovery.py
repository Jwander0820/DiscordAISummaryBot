import importlib
import os
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from discord_bot.db.health import DatabaseHealth
from tests.test_deepfaker_repository import make_event_record


CASES = (
    ("discord_bot.db.repository", "SummaryRepository", "insert_summary", "summaries"),
    ("discord_bot.db.deepfaker_repository", "DeepFakerRepository", "insert_event", "deepfaker_events"),
)


def record_for(table):
    if table == "deepfaker_events":
        return make_event_record()
    return {"channel_id": "1001", "user_id": "2001", "command": "synthetic", "question": "question",
            "prompt": "prompt", "summary": "answer", "call_time": "2026-10-15T00:00:00+00:00"}


class DatabaseRecoveryTests(unittest.TestCase):
    def test_settings_query_failure_and_recovery_emit_health_events(self):
        module = importlib.import_module("discord_bot.db.social_preview_settings_repository")
        health = DatabaseHealth()
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": os.path.join(temp, "test.db")}), \
             patch.object(module, "database_health", health), \
             patch.object(module.time, "monotonic", return_value=100.0) as clock:
            repository = module.SocialPreviewSettingsRepository()
            try:
                self.assertTrue(repository.set_setting("1001", "threads", False))
                repository.conn.close()
                clock.return_value = 200.0
                self.assertIsNone(repository.get_setting("1001", "threads"))
                failure = health.events.get_nowait()
                self.assertEqual((failure.operation, failure.recovered), ("read", False))
                clock.return_value = 229.0
                self.assertFalse(repository.is_available())
                clock.return_value = 230.0
                self.assertIs(repository.get_setting("1001", "threads"), False)
                self.assertTrue(health.events.get_nowait().recovered)
            finally:
                repository.close()

    def test_startup_failure_retries_on_demand_after_cooldown_and_reuses_connection(self):
        for module_name, class_name, method, table in CASES:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as temp:
                module = importlib.import_module(module_name)
                path = os.path.join(temp, "test.db")
                real_connect = sqlite3.connect
                health = DatabaseHealth()
                with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": path}), \
                     patch.object(module, "database_health", health), \
                     patch.object(module.time, "monotonic", return_value=100.0) as clock, \
                     patch.object(module.sqlite3, "connect", side_effect=[sqlite3.OperationalError("offline"),
                                                                          real_connect(path)]) as connect:
                    repository = getattr(module, class_name)()
                    try:
                        self.assertFalse(repository.init())
                        clock.return_value = 129.0
                        self.assertFalse(getattr(repository, method)(record_for(table)))
                        self.assertEqual(connect.call_count, 1)
                        clock.return_value = 130.0
                        self.assertTrue(getattr(repository, method)(record_for(table)))
                        working_connection = repository.conn
                        self.assertTrue(getattr(repository, method)(record_for(table)))
                        self.assertIs(repository.conn, working_connection)
                        self.assertEqual(connect.call_count, 2)
                        self.assertEqual(repository.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 2)
                        events = [health.events.get_nowait(), health.events.get_nowait()]
                        self.assertEqual([event.recovered for event in events], [False, True])
                    finally:
                        repository.close()

    def test_closed_connection_is_replaced_only_on_later_demand(self):
        for module_name, class_name, method, table in CASES:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as temp:
                module = importlib.import_module(module_name)
                with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": os.path.join(temp, "test.db")}), \
                     patch.object(module, "database_health", DatabaseHealth()), \
                     patch.object(module.time, "monotonic", return_value=100.0) as clock:
                    repository = getattr(module, class_name)()
                    try:
                        self.assertTrue(repository.init())
                        old_connection = repository.conn
                        old_connection.close()
                        clock.return_value = 200.0
                        self.assertFalse(getattr(repository, method)(record_for(table)))
                        self.assertIsNone(repository.conn)
                        clock.return_value = 229.0
                        self.assertFalse(getattr(repository, method)(record_for(table)))
                        clock.return_value = 230.0
                        self.assertTrue(getattr(repository, method)(record_for(table)))
                        self.assertIsNot(repository.conn, old_connection)
                        self.assertEqual(repository.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)
                    finally:
                        repository.close()

    def test_statement_error_rolls_back_transaction_before_next_write(self):
        class TransactionConnection:
            closed = False

            def __init__(self):
                self.aborted = False
                self.first = True
                self.rollbacks = 0
                self.commits = 0

            def execute(self, sql, values):
                if self.first:
                    self.first = False
                    self.aborted = True
                    raise RuntimeError("synthetic statement failure")
                if self.aborted:
                    raise RuntimeError("transaction still aborted")

            def rollback(self):
                self.rollbacks += 1
                self.aborted = False

            def commit(self):
                self.commits += 1

        for module_name, class_name, method, table in CASES:
            with self.subTest(table=table):
                module = importlib.import_module(module_name)
                connection = TransactionConnection()
                repository = getattr(module, class_name)()
                repository.conn = repository.cursor = connection
                repository._initialized = True
                health = DatabaseHealth()
                with patch.object(module, "database_health", health):
                    self.assertFalse(getattr(repository, method)(record_for(table)))
                    self.assertIs(repository.conn, connection)
                    self.assertTrue(getattr(repository, method)(record_for(table)))
                self.assertEqual((connection.rollbacks, connection.commits), (1, 1))
                self.assertFalse(health.events.get_nowait().unavailable)
                self.assertTrue(health.events.get_nowait().recovered)

    def test_uncertain_commit_is_not_replayed(self):
        for module_name, class_name, method, table in CASES:
            with self.subTest(table=table):
                module = importlib.import_module(module_name)
                connection = Mock(closed=True)
                connection.commit.side_effect = RuntimeError("connection dropped during commit")
                connection.rollback.side_effect = RuntimeError("closed")
                repository = getattr(module, class_name)()
                repository.conn = connection
                repository.cursor = connection.cursor.return_value
                repository._initialized = True
                with patch.object(module, "database_health", DatabaseHealth()):
                    self.assertFalse(getattr(repository, method)(record_for(table)))
                repository_cursor = connection.cursor.return_value
                repository_cursor.execute.assert_called_once()
                connection.commit.assert_called_once()
                connection.close.assert_called_once()
                self.assertFalse(repository.db_enabled)
                self.assertIsNone(repository.conn)

    def test_postgres_initialization_failure_closes_partial_connection(self):
        for module_name, class_name, _method, table in CASES:
            with self.subTest(table=table):
                module = importlib.import_module(module_name)
                connection = Mock()
                connection.cursor.return_value.execute.side_effect = RuntimeError("synthetic create error")
                driver = types.SimpleNamespace(connect=Mock(return_value=connection))
                with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": "synthetic-test-dsn"}), \
                     patch.object(module, "psycopg2", driver), \
                     patch.object(module, "database_health", DatabaseHealth()):
                    repository = getattr(module, class_name)()
                    self.assertFalse(repository.init())
                    connection.close.assert_called_once()
                    self.assertIsNone(repository.conn)
                    self.assertIsNone(repository.cursor)


if __name__ == "__main__":
    unittest.main()
