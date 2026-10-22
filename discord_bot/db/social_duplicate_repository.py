"""Persistent share history; cache recovery never deletes historical events."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

from .health import database_health
from .schema import CREATE_SOCIAL_DUPLICATE_TABLES_SQL

logger = logging.getLogger("discord_digest_bot")


@dataclass(frozen=True, slots=True)
class ShareEvent:
    guild_id: str
    channel_id: str
    platform: str
    source_message_id: str
    user_id: str
    post_key: str
    canonical_url: str
    preview_message_id: str  # Reference target: delivered preview, or original message for X.
    occurred_at: float

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.guild_id, self.channel_id, self.platform, self.post_key


class SocialDuplicateRepository:
    """One reused connection, serialized across worker calls; failures raise explicitly."""

    def __init__(self, *, retry_interval_seconds=30):
        self.conn = None
        self.backend = "sqlite"
        self.placeholder = "?"
        self.retry_interval_seconds = retry_interval_seconds
        self._last_failure = float("-inf")
        self._lock = threading.RLock()

    def _connect(self):
        if self.conn is not None:
            return
        if time.monotonic() - self._last_failure < self.retry_interval_seconds:
            raise RuntimeError("Social duplicate database cooling down")
        self.backend = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        try:
            if self.backend == "postgres":
                import psycopg2
                url = os.getenv("DATABASE_URL")
                if not url:
                    raise RuntimeError("PostgreSQL configuration missing")
                self.conn = psycopg2.connect(url, connect_timeout=5)
                self.placeholder = "%s"
            else:
                self.backend = "sqlite"
                self.conn = sqlite3.connect(os.getenv("SQLITE_PATH", "summaries.db"), check_same_thread=False)
                self.placeholder = "?"
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    for sql in CREATE_SOCIAL_DUPLICATE_TABLES_SQL:
                        cursor.execute(sql)
                    if self.backend == "postgres":
                        for name in ("taunt_text", "taunt_source", "reference_channel_id"):
                            cursor.execute(f"ALTER TABLE social_patrol_events ADD COLUMN IF NOT EXISTS {name} TEXT")
                    else:
                        cursor.execute("PRAGMA table_info(social_patrol_events)")
                        columns = {row[1] for row in cursor.fetchall()}
                        for name in ("taunt_text", "taunt_source", "reference_channel_id"):
                            if name not in columns:
                                cursor.execute(f"ALTER TABLE social_patrol_events ADD COLUMN {name} TEXT")
                finally:
                    cursor.close()
        except Exception:
            self.close()
            self._last_failure = time.monotonic()
            raise

    def _execute(self, sql, params=(), *, write=False):
        with self._lock:
            self._connect()
            cursor = None
            try:
                cursor = self.conn.cursor()
                cursor.execute(sql.replace("?", self.placeholder), params)
                result = cursor.rowcount if write else cursor.fetchall()
                # End read transactions too, particularly for PostgreSQL.
                self.conn.commit()
                return result
            except Exception:
                self.close()
                self._last_failure = time.monotonic()
                raise
            finally:
                try:
                    if cursor is not None:
                        cursor.close()
                except Exception:
                    pass

    def snapshot(self, *, enabled: bool, days: int, now: float, guild_id=None):
        with self._lock:
            settings = {row[0]: (bool(row[1]), int(row[2])) for row in self._execute(
                "SELECT guild_id, enabled, window_days FROM guild_social_duplicate_settings")}
            max_days = max([days] + [window for active, window in settings.values() if active])
            # Constant lower bound lets the time index exclude retained old history.
            guild_filter = " AND e.guild_id = ? " if guild_id is not None else " "
            params = (now - max_days * 86400.0, int(enabled), now, days)
            if guild_id is not None:
                params += (guild_id,)
            events = self._execute(
                "SELECT e.guild_id, e.channel_id, e.platform, e.source_message_id, e.user_id, "
                "e.post_key, e.canonical_url, e.preview_message_id, e.occurred_at "
                "FROM social_share_events e LEFT JOIN guild_social_duplicate_settings s ON e.guild_id = s.guild_id "
                "WHERE e.occurred_at >= ? AND COALESCE(s.enabled, ?) = 1 "
                "AND e.occurred_at >= ? - COALESCE(s.window_days, ?) * 86400.0 "
                + guild_filter + "ORDER BY e.occurred_at, e.source_message_id", params)
            return settings, [ShareEvent(*row) for row in events]

    def insert(self, event: ShareEvent) -> bool:
        return self._execute(
            "INSERT INTO social_share_events "
            "(guild_id, channel_id, platform, source_message_id, user_id, post_key, canonical_url, "
            "preview_message_id, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id, platform, source_message_id) DO NOTHING",
            (event.guild_id, event.channel_id, event.platform, event.source_message_id, event.user_id,
             event.post_key, event.canonical_url, event.preview_message_id, event.occurred_at), write=True) == 1

    def delete_previews(self, guild_id, channel_id, message_ids):
        """Remove only preview references confirmed deleted by the preview button."""
        message_ids = tuple(str(message_id) for message_id in message_ids)
        if not message_ids:
            return []
        placeholders = ", ".join("?" for _ in message_ids)
        rows = self._execute(
            "DELETE FROM social_share_events WHERE guild_id = ? AND channel_id = ? "
            "AND platform IN ('threads', 'instagram', 'facebook') "
            f"AND preview_message_id IN ({placeholders}) "
            "RETURNING guild_id, channel_id, platform, source_message_id, user_id, "
            "post_key, canonical_url, preview_message_id, occurred_at",
            (guild_id, channel_id, *message_ids))
        return [ShareEvent(*row) for row in rows]

    def remaining_shares(self, key, *, since):
        rows = self._execute(
            "SELECT guild_id, channel_id, platform, source_message_id, user_id, "
            "post_key, canonical_url, preview_message_id, occurred_at FROM social_share_events "
            "WHERE guild_id = ? AND channel_id = ? AND platform = ? AND post_key = ? "
            "AND occurred_at >= ? ORDER BY occurred_at, source_message_id",
            (*key, since))
        return [ShareEvent(*row) for row in rows]

    def cross_channel_settings(self):
        return {guild: (bool(enabled), frozenset(json.loads(channels)))
                for guild, enabled, channels in self._execute(
                    "SELECT guild_id, enabled, channel_ids_json FROM guild_social_cross_channel_settings")}

    def configure_cross_channel(self, guild_id, *, enabled, channel_ids, updated_by):
        self._execute(
            "INSERT INTO guild_social_cross_channel_settings "
            "(guild_id, enabled, channel_ids_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET enabled = excluded.enabled, "
            "channel_ids_json = excluded.channel_ids_json, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at",
            (guild_id, int(enabled), json.dumps(sorted(channel_ids)), updated_by, time.time()), write=True)

    def configure(self, guild_id: str, setting, *, updated_by: str):
        if setting is None:
            self._execute("DELETE FROM guild_social_duplicate_settings WHERE guild_id = ?", (guild_id,), write=True)
        else:
            enabled, days = setting
            self._execute(
                "INSERT INTO guild_social_duplicate_settings (guild_id, enabled, window_days, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET enabled = excluded.enabled, "
                "window_days = excluded.window_days, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (guild_id, int(enabled), days, updated_by, time.time()), write=True)

    def close(self):
        with self._lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None


def report_failure(repository: SocialDuplicateRepository, error: Exception, operation: str):
    # Called on the event loop: DatabaseHealth's asyncio queue has a single producer thread.
    logger.warning("Social duplicate DB unavailable: operation=%s error_type=%s", operation, type(error).__name__)
    database_health.failed("social_share_events", repository.backend, operation, error=error)
