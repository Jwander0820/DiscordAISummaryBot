from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Optional

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

    def __init__(self) -> None:
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        self.placeholder = "?"
        self.conn: Optional[Any] = None
        self.cursor: Optional[Any] = None
        self._initialized = False

    def init(self) -> bool:
        if self._initialized:
            return self._ready

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
        return self._ready

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 未安裝，無法初始化 DeepFaker 紀錄表")
            self.db_enabled = False
            return
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("使用 PostgreSQL 但未設定 DATABASE_URL，停用 DeepFaker 紀錄寫入")
            self.db_enabled = False
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
            self.db_enabled = False

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
            self.db_enabled = False

    def insert_event(self, record: dict[str, Any]) -> bool:
        if not self.init():
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
            return True
        except Exception as exc:
            logger.error("DeepFaker event 寫入失敗: %s", exc, exc_info=True)
            try:
                self.conn.rollback()
            except Exception:
                logger.debug("DeepFaker DB rollback 失敗", exc_info=True)
            return False

    @property
    def _ready(self) -> bool:
        return self.db_enabled and self.cursor is not None and self.conn is not None


deepfaker_repository = DeepFakerRepository()
