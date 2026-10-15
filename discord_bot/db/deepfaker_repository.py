from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Optional

from .health import database_health
from .schema import (
    CREATE_DEEPFAKER_EVENT_INDEXES_SQL,
    DEEPFAKER_EVENT_COLUMNS,
    POSTGRES_CREATE_DEEPFAKER_EVENTS_SQL,
    SQLITE_CREATE_DEEPFAKER_EVENTS_SQL,
)

logger = logging.getLogger("discord_digest_bot")

try:
    import psycopg2
except ImportError:  # pragma: no cover - depends on runtime extras
    psycopg2 = None


class DeepFakerRepository:
    """Persist append-only DeepFaker events behind one small interface."""

    def __init__(self, *, retry_interval_seconds: float = 30.0) -> None:
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        self.placeholder = "?"
        self.conn: Optional[Any] = None
        self.cursor: Optional[Any] = None
        self._initialized = False
        self._last_init_attempt: Optional[float] = None
        self.retry_interval_seconds = retry_interval_seconds

    def init(self, *, force: bool = False) -> bool:
        if self._initialized and not force:
            return self._ready
        if force:
            self._close_connection()

        self._last_init_attempt = time.monotonic()
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        if self.db_type not in {"sqlite", "postgres"}:
            logger.error("不支援的 DB_TYPE: %s，DeepFaker 紀錄改用 sqlite", self.db_type)
            self.db_type = "sqlite"

        if self.db_type == "postgres":
            self._init_postgres()
        else:
            self._init_sqlite()
        self._initialized = True
        if self._ready:
            database_health.succeeded("deepfaker_events", self.db_type, "initialize")
        return self._ready

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 未安裝，無法初始化 DeepFaker 紀錄表")
            self._mark_unavailable(reason="DriverUnavailable")
            return
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("使用 PostgreSQL 但未設定 DATABASE_URL，停用 DeepFaker 紀錄寫入")
            self._mark_unavailable(reason="ConfigurationMissing")
            return

        try:
            self.conn = psycopg2.connect(database_url, connect_timeout=5)
            self.cursor = self.conn.cursor()
            self.cursor.execute(POSTGRES_CREATE_DEEPFAKER_EVENTS_SQL)
            for index_sql in CREATE_DEEPFAKER_EVENT_INDEXES_SQL:
                self.cursor.execute(index_sql)
            self.conn.commit()
            self.placeholder = "%s"
            logger.info("DeepFaker PostgreSQL 紀錄表初始化完成")
        except Exception as exc:
            logger.error("DeepFaker PostgreSQL 紀錄表初始化失敗: %s", exc, exc_info=True)
            self._mark_unavailable(error=exc)

    def _init_sqlite(self) -> None:
        sqlite_path = os.getenv("SQLITE_PATH", "summaries.db")
        try:
            self.conn = sqlite3.connect(sqlite_path)
            self.cursor = self.conn.cursor()
            self.cursor.execute(SQLITE_CREATE_DEEPFAKER_EVENTS_SQL)
            for index_sql in CREATE_DEEPFAKER_EVENT_INDEXES_SQL:
                self.cursor.execute(index_sql)
            self.conn.commit()
            self.placeholder = "?"
            logger.info("DeepFaker SQLite 紀錄表初始化完成 (%s)", sqlite_path)
        except Exception as exc:
            logger.error("DeepFaker SQLite 紀錄表初始化失敗: %s", exc, exc_info=True)
            self._mark_unavailable(error=exc)

    def close(self) -> None:
        self._close_connection()
        self._initialized = False
        self._last_init_attempt = None

    def _close_connection(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                logger.debug("關閉 DeepFaker DB 連線失敗", exc_info=True)
        self.conn = self.cursor = None

    def _ensure_ready(self) -> bool:
        if self.init():
            return True
        if (self._last_init_attempt is None
                or time.monotonic() - self._last_init_attempt >= self.retry_interval_seconds):
            return self.init(force=True)
        return False

    def _mark_unavailable(self, *, error: Exception | None = None,
                          reason: str = "DatabaseError", operation: str = "initialize") -> None:
        self.db_enabled = False
        self._last_init_attempt = time.monotonic()
        self._close_connection()
        database_health.failed("deepfaker_events", self.db_type, operation, error=error, reason=reason)

    def insert_event(self, record: dict[str, Any]) -> bool:
        if not self._ensure_ready():
            logger.warning("insert_event: DeepFaker DB unavailable，跳過寫入")
            return False

        unknown_columns = set(record) - set(DEEPFAKER_EVENT_COLUMNS)
        missing_columns = set(DEEPFAKER_EVENT_COLUMNS) - set(record)
        if unknown_columns or missing_columns:
            logger.error(
                "DeepFaker event 欄位不符 schema，missing=%s unknown=%s",
                sorted(missing_columns),
                sorted(unknown_columns),
            )
            return False

        columns = list(DEEPFAKER_EVENT_COLUMNS)
        values = [record[column] for column in columns]
        placeholders = ",".join([self.placeholder] * len(columns))
        sql = f"INSERT INTO deepfaker_events ({','.join(columns)}) VALUES ({placeholders});"
        try:
            self.cursor.execute(sql, values)
            self.conn.commit()
            database_health.succeeded("deepfaker_events", self.db_type, "write")
            return True
        except Exception as exc:
            logger.error("DeepFaker event 寫入失敗: %s", exc, exc_info=True)
            try:
                self.conn.rollback()
                usable = not getattr(self.conn, "closed", False)
            except Exception:
                usable = False
            if usable:
                database_health.failed("deepfaker_events", self.db_type, "write", error=exc, unavailable=False)
            else:
                self._mark_unavailable(error=exc, operation="write")
            # Do not replay an INSERT whose commit result may be unknown.
            return False

    @property
    def _ready(self) -> bool:
        return self.db_enabled and self.cursor is not None and self.conn is not None


deepfaker_repository = DeepFakerRepository()
