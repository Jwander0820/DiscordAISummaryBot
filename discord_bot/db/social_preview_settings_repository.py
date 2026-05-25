from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .schema import (
    POSTGRES_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL,
    SQLITE_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL,
)

logger = logging.getLogger("discord_digest_bot")

try:
    import psycopg2
except ImportError:  # pragma: no cover - depends on runtime extras
    psycopg2 = None


class SocialPreviewSettingsRepository:
    """Persist guild-level social preview platform overrides."""

    def __init__(self) -> None:
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        self.placeholder = "?"
        self.conn: Optional[Any] = None
        self.cursor: Optional[Any] = None
        self._initialized = False

    def init(self) -> None:
        if self._initialized:
            return

        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True

        if self.db_type not in {"sqlite", "postgres"}:
            logger.error("不支援的 DB_TYPE: %s，Social Preview 設定改用 sqlite", self.db_type)
            self.db_type = "sqlite"

        if self.db_type == "postgres":
            self._init_postgres()
        else:
            self._init_sqlite()

        self._initialized = True

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 未安裝，無法初始化 Social Preview 設定表")
            self.db_enabled = False
            return

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("使用 PostgreSQL 但未設定 DATABASE_URL，停用 Social Preview 設定寫入")
            self.db_enabled = False
            return

        try:
            self.conn = psycopg2.connect(database_url, connect_timeout=5)
            self.cursor = self.conn.cursor()
            self.cursor.execute(POSTGRES_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL)
            self.conn.commit()
            self.placeholder = "%s"
            logger.info("Social Preview guild settings PostgreSQL 初始化完成")
        except Exception as exc:
            logger.error("Social Preview guild settings PostgreSQL 初始化失敗: %s", exc, exc_info=True)
            self.db_enabled = False

    def _init_sqlite(self) -> None:
        sqlite_path = os.getenv("SQLITE_PATH", "summaries.db")
        try:
            self.conn = sqlite3.connect(sqlite_path)
            self.cursor = self.conn.cursor()
            self.cursor.execute(SQLITE_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL)
            self.conn.commit()
            self.placeholder = "?"
            logger.info("Social Preview guild settings SQLite 初始化完成 (%s)", sqlite_path)
        except Exception as exc:
            logger.error("Social Preview guild settings SQLite 初始化失敗: %s", exc, exc_info=True)
            self.db_enabled = False

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self.cursor = None
        self._initialized = False

    def get_setting(self, guild_id: str, platform: str) -> Optional[bool]:
        self.init()
        if not self._ready:
            return None

        sql = (
            "SELECT enabled FROM guild_social_preview_settings "
            f"WHERE guild_id = {self.placeholder} AND platform = {self.placeholder};"
        )
        self.cursor.execute(sql, (str(guild_id), platform))
        row = self.cursor.fetchone()
        if row is None:
            return None
        return bool(row[0])

    def set_setting(self, guild_id: str, platform: str, enabled: bool, *, updated_by: Optional[str] = None) -> None:
        self.init()
        if not self._ready:
            logger.warning("set_setting: Social Preview settings DB unavailable")
            return

        enabled_value: Any = bool(enabled) if self.db_type == "postgres" else int(bool(enabled))
        updated_at = datetime.now(timezone.utc).isoformat()
        sql = (
            "INSERT INTO guild_social_preview_settings "
            "(guild_id, platform, enabled, updated_by, updated_at) "
            f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}) "
            "ON CONFLICT(guild_id, platform) DO UPDATE SET "
            "enabled = excluded.enabled, "
            "updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at;"
        )
        self.cursor.execute(sql, (str(guild_id), platform, enabled_value, updated_by, updated_at))
        self.conn.commit()

    def clear_setting(self, guild_id: str, platform: str) -> None:
        self.init()
        if not self._ready:
            logger.warning("clear_setting: Social Preview settings DB unavailable")
            return

        sql = (
            "DELETE FROM guild_social_preview_settings "
            f"WHERE guild_id = {self.placeholder} AND platform = {self.placeholder};"
        )
        self.cursor.execute(sql, (str(guild_id), platform))
        self.conn.commit()

    def list_guild_settings(self, guild_id: str) -> dict[str, bool]:
        self.init()
        if not self._ready:
            return {}

        sql = f"SELECT platform, enabled FROM guild_social_preview_settings WHERE guild_id = {self.placeholder};"
        self.cursor.execute(sql, (str(guild_id),))
        return {str(platform): bool(enabled) for platform, enabled in self.cursor.fetchall()}

    @property
    def _ready(self) -> bool:
        return self.db_enabled and self.cursor is not None and self.conn is not None


social_preview_settings_repository = SocialPreviewSettingsRepository()
