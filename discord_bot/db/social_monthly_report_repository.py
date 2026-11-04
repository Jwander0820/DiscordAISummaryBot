"""Monthly report settings, durable runs and one read-only consistent event snapshot."""
from __future__ import annotations

import json
import time

from .schema import CREATE_SOCIAL_MONTHLY_REPORT_TABLES_SQL
from .social_duplicate_repository import SocialDuplicateRepository

RUN_COLUMNS = (
    "guild_id", "report_month", "report_id", "period_start", "period_end", "channel_id", "status",
    "pages_json", "message_ids_json", "sending_page", "attempts", "claim_token", "lease_expires_at",
    "error_type", "created_at", "updated_at", "sent_at",
)
SHARE_COLUMNS = ("channel_id", "platform", "source_message_id", "user_id", "post_key", "canonical_url",
                 "occurred_at")
PATROL_COLUMNS = ("event_id", "channel_id", "user_id", "username", "global_name", "display_name",
                  "platform", "post_key", "first_source_message_id", "first_user_id",
                  "reference_channel_id", "occurred_at", "delivery_status")


class LostClaim(RuntimeError):
    """Another worker owns this run now; never overwrite its state."""


def _run(row):
    if row is None:
        return None
    run = dict(zip(RUN_COLUMNS, row))
    run["pages"] = json.loads(run["pages_json"]) if run["pages_json"] else None
    run["message_ids"] = json.loads(run["message_ids_json"]) if run["message_ids_json"] else None
    return run


class SocialMonthlyReportRepository(SocialDuplicateRepository):
    """Own connection; also ensures the source event tables exist for fresh databases."""

    def _initialize_schema(self, cursor):
        super()._initialize_schema(cursor)
        for sql in CREATE_SOCIAL_MONTHLY_REPORT_TABLES_SQL:
            cursor.execute(sql)

    # Settings -----------------------------------------------------------------
    def get_settings(self, guild_id):
        rows = self._execute(
            "SELECT guild_id, enabled, channel_id, first_report_month, updated_by, updated_at "
            "FROM guild_social_monthly_report_settings WHERE guild_id = ?", (str(guild_id),))
        if not rows:
            return None
        guild, enabled, channel, first_month, updated_by, updated_at = rows[0]
        return {"guild_id": guild, "enabled": bool(enabled), "channel_id": channel,
                "first_report_month": first_month, "updated_by": updated_by, "updated_at": updated_at}

    def ensure_settings(self, guild_id, *, first_report_month, now=None):
        """Disabled until an administrator enables it; existing rows are untouched."""
        self._execute(
            "INSERT INTO guild_social_monthly_report_settings "
            "(guild_id, enabled, channel_id, first_report_month, updated_by, updated_at) "
            "VALUES (?, 0, NULL, ?, NULL, ?) ON CONFLICT (guild_id) DO NOTHING",
            (str(guild_id), first_report_month, time.time() if now is None else now), write=True)
        return self.get_settings(guild_id)

    def save_settings(self, guild_id, *, enabled, channel_id, first_report_month, updated_by):
        self._execute(
            "INSERT INTO guild_social_monthly_report_settings "
            "(guild_id, enabled, channel_id, first_report_month, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET enabled = excluded.enabled, "
            "channel_id = excluded.channel_id, first_report_month = excluded.first_report_month, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (str(guild_id), int(bool(enabled)), None if channel_id is None else str(channel_id),
             first_report_month, None if updated_by is None else str(updated_by), time.time()), write=True)
        return self.get_settings(guild_id)

    def enabled_guilds(self, report_month):
        """Guilds an administrator enabled whose first report month has arrived."""
        rows = self._execute(
            "SELECT guild_id FROM guild_social_monthly_report_settings "
            "WHERE enabled = 1 AND first_report_month <= ? ORDER BY guild_id", (report_month,))
        return [str(row[0]) for row in rows]

    # Runs ---------------------------------------------------------------------
    def get_run(self, guild_id, report_month):
        rows = self._execute(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM social_monthly_report_runs "
            "WHERE guild_id = ? AND report_month = ?", (str(guild_id), report_month))
        return _run(rows[0]) if rows else None

    def latest_runs(self, guild_id, limit=3):
        rows = self._execute(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM social_monthly_report_runs WHERE guild_id = ? "
            "ORDER BY report_month DESC LIMIT ?", (str(guild_id), int(limit)))
        return [_run(row) for row in rows]

    def claim_run(self, guild_id, report_month, *, token, report_id, period_start, period_end,
                  channel_id, now, lease_seconds):
        """Atomically create, or take over an expired pending run. Returns the run or None."""
        guild_id = str(guild_id)
        expires = now + lease_seconds
        inserted = self._execute(
            "INSERT INTO social_monthly_report_runs (guild_id, report_month, report_id, period_start, "
            "period_end, channel_id, status, attempts, claim_token, lease_expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, report_month) DO NOTHING",
            (guild_id, report_month, report_id, period_start, period_end,
             None if channel_id is None else str(channel_id), token, expires, now, now), write=True) == 1
        if not inserted:
            taken = self._execute(
                "UPDATE social_monthly_report_runs SET claim_token = ?, lease_expires_at = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE guild_id = ? AND report_month = ? "
                "AND status = 'pending' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
                (token, expires, now, guild_id, report_month, now), write=True) == 1
            if not taken:
                return None
        run = self.get_run(guild_id, report_month)
        return run if run and run["claim_token"] == token else None

    def _guarded(self, guild_id, report_month, token, assignments, params):
        changed = self._execute(
            f"UPDATE social_monthly_report_runs SET {assignments}, updated_at = ? "
            "WHERE guild_id = ? AND report_month = ? AND claim_token = ? AND status = 'pending'",
            (*params, time.time(), str(guild_id), report_month, token), write=True)
        if changed != 1:
            raise LostClaim("Monthly report claim lost")

    def store_pages(self, guild_id, report_month, token, pages, *, lease_expires_at):
        self._guarded(guild_id, report_month, token,
                      "pages_json = ?, message_ids_json = ?, sending_page = NULL, lease_expires_at = ?",
                      (json.dumps(pages, ensure_ascii=False), json.dumps([None] * len(pages)), lease_expires_at))

    def begin_page(self, guild_id, report_month, token, index, *, lease_expires_at):
        self._guarded(guild_id, report_month, token, "sending_page = ?, lease_expires_at = ?",
                      (int(index), lease_expires_at))

    def confirm_page(self, guild_id, report_month, token, message_ids):
        self._guarded(guild_id, report_month, token, "message_ids_json = ?, sending_page = NULL",
                      (json.dumps(message_ids),))

    def finish(self, guild_id, report_month, token, *, status, error_type=None, now=None):
        if status not in {"sent", "empty", "failed", "unknown"}:
            raise ValueError("Invalid monthly report outcome")
        now = time.time() if now is None else now
        self._guarded(guild_id, report_month, token,
                      "status = ?, error_type = ?, claim_token = NULL, lease_expires_at = NULL, sent_at = ?",
                      (status, error_type, now if status == "sent" else None))

    def release(self, guild_id, report_month, token):
        """Give up a claim early after a transient failure, keeping pending content."""
        self._guarded(guild_id, report_month, token, "claim_token = NULL, lease_expires_at = NULL", ())

    # Read-only snapshot ---------------------------------------------------------
    def month_events(self, guild_id, period_start, period_end):
        """Both event tables from one read-only, repeatable snapshot of this guild only."""
        with self._lock:
            self._connect()
            cursor = None
            try:
                cursor = self.conn.cursor()
                if self.backend == "postgres":
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                else:
                    cursor.execute("BEGIN")
                params = (str(guild_id), period_start, period_end)
                cursor.execute(
                    (f"SELECT {', '.join(SHARE_COLUMNS)} FROM social_share_events "
                     "WHERE guild_id = ? AND occurred_at >= ? AND occurred_at < ? "
                     "ORDER BY occurred_at, source_message_id").replace("?", self.placeholder), params)
                shares = cursor.fetchall()
                cursor.execute(
                    (f"SELECT {', '.join(PATROL_COLUMNS)} FROM social_patrol_events "
                     "WHERE guild_id = ? AND occurred_at >= ? AND occurred_at < ? "
                     "ORDER BY occurred_at, event_id").replace("?", self.placeholder), params)
                patrols = cursor.fetchall()
                self.conn.rollback()
                return shares, patrols
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
