from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Optional

from .health import database_health
from .schema import POSTGRES_CREATE_SUMMARIES_SQL, SQLITE_CREATE_SUMMARIES_SQL

logger = logging.getLogger("discord_digest_bot")

try:
    import psycopg2
except ImportError:  # pragma: no cover - depends on runtime extras
    psycopg2 = None


class SummaryRepository:
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
        """初始化 repository。

        目前只處理 `summaries` 表，啟動後會盡量重用同一個連線。
        """
        if self._initialized and not force:
            return self._ready
        if force:
            self._close_connection()

        self._last_init_attempt = time.monotonic()
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
        if self._ready:
            database_health.succeeded("summaries", self.db_type, "initialize")
        return self._ready

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 未安裝，無法使用 PostgreSQL，已停用 DB 寫入")
            self._mark_unavailable(reason="DriverUnavailable")
            return

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("使用 PostgreSQL 時，必須設定 DATABASE_URL，停用 DB 寫入")
            self._mark_unavailable(reason="ConfigurationMissing")
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
            self._mark_unavailable(error=exc)

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
                logger.debug("關閉 summaries DB 連線失敗", exc_info=True)
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
        database_health.failed("summaries", self.db_type, operation, error=error, reason=reason)

    @property
    def _ready(self) -> bool:
        return self.db_enabled and self.cursor is not None and self.conn is not None

    def insert_summary(self, record: dict) -> bool:
        """寫入一筆 summaries record。"""
        if not self._ensure_ready():
            logger.warning("insert_summary: DB_DISABLED，跳過寫入")
            return False

        cols = list(record.keys())
        vals = list(record.values())
        phs = ",".join([self.placeholder] * len(cols))
        sql = f"INSERT INTO summaries ({','.join(cols)}) VALUES ({phs});"

        try:
            self.cursor.execute(sql, vals)
            self.conn.commit()
            logger.info("✅ summaries 寫入成功")
            database_health.succeeded("summaries", self.db_type, "write")
            return True
        except Exception as exc:
            logger.error("❌ summaries 寫入失敗: %s", exc, exc_info=True)
            try:
                self.conn.rollback()
                usable = not getattr(self.conn, "closed", False)
            except Exception:
                usable = False
            if usable:
                database_health.failed("summaries", self.db_type, "write", error=exc, unavailable=False)
            else:
                self._mark_unavailable(error=exc, operation="write")
            # The failed INSERT is never replayed, including uncertain commit outcomes.
            return False


summary_repository = SummaryRepository()
