from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Optional

from .schema import POSTGRES_CREATE_SUMMARIES_SQL, SQLITE_CREATE_SUMMARIES_SQL

logger = logging.getLogger("discord_digest_bot")

try:
    import psycopg2
except ImportError:  # pragma: no cover - depends on runtime extras
    psycopg2 = None


class SummaryRepository:
    def __init__(self) -> None:
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        self.placeholder = "?"
        self.conn: Optional[Any] = None
        self.cursor: Optional[Any] = None
        self._initialized = False

    def init(self) -> None:
        """初始化 repository。

        目前只處理 `summaries` 表，啟動後會盡量重用同一個連線。
        """
        if self._initialized:
            return

        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True

        if self.db_type not in {"sqlite", "postgres"}:
            logger.error("不支援的 DB_TYPE: %s，預設改用 sqlite", self.db_type)
            self.db_type = "sqlite"

        if self.db_type == "postgres":
            self._init_postgres()
        else:
            self._init_sqlite()

        self._initialized = True

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 未安裝，無法使用 PostgreSQL，已停用 DB 寫入")
            self.db_enabled = False
            return

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("使用 PostgreSQL 時，必須設定 DATABASE_URL，停用 DB 寫入")
            self.db_enabled = False
            return

        try:
            self.conn = psycopg2.connect(database_url, connect_timeout=5)
            self.cursor = self.conn.cursor()
            self.cursor.execute(POSTGRES_CREATE_SUMMARIES_SQL)
            self.conn.commit()
            self.placeholder = "%s"
            logger.info("✅ PostgreSQL 連線及建表成功")
        except Exception as exc:
            logger.error("❌ PostgreSQL 初始化失敗，已停用 DB 寫入: %s", exc, exc_info=True)
            self.db_enabled = False

    def _init_sqlite(self) -> None:
        sqlite_path = os.getenv("SQLITE_PATH", "summaries.db")
        try:
            self.conn = sqlite3.connect(sqlite_path)
            self.cursor = self.conn.cursor()
            self.cursor.execute(SQLITE_CREATE_SUMMARIES_SQL)
            self.conn.commit()
            self.placeholder = "?"
            logger.info("✅ SQLite 連線及建表成功 (%s)", sqlite_path)
        except Exception as exc:
            logger.error("❌ SQLite 初始化失敗，已停用 DB 寫入: %s", exc, exc_info=True)
            self.db_enabled = False

    def insert_summary(self, record: dict) -> None:
        """寫入一筆 summaries record。"""
        self.init()
        if not self.db_enabled:
            logger.warning("insert_summary: DB_DISABLED，跳過寫入")
            return
        if not self.cursor or not self.conn:
            logger.warning("insert_summary: DB 尚未完成初始化，跳過寫入")
            return

        cols = list(record.keys())
        vals = list(record.values())
        phs = ",".join([self.placeholder] * len(cols))
        sql = f"INSERT INTO summaries ({','.join(cols)}) VALUES ({phs});"

        try:
            self.cursor.execute(sql, vals)
            self.conn.commit()
            logger.info("✅ summaries 寫入成功")
        except Exception as exc:
            logger.error("❌ summaries 寫入失敗: %s", exc, exc_info=True)


summary_repository = SummaryRepository()
