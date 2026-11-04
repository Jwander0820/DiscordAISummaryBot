"""Monthly settlement, durable claim and delivery recovery; Discord is injected as a transport."""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from ...db.health import database_health
from ...db.social_monthly_report_repository import LostClaim, SocialMonthlyReportRepository
from .aggregation import PatrolRow, ShareRow, compute_report
from .periods import due_month, month_bounds, month_of, next_boundary, taipei_datetime
from .render import render_pages

logger = logging.getLogger("discord_digest_bot")
HEALTH_NAME = "social_monthly_report_runs"
# Outcomes that settle a guild's month for this process; anything else is retried later.
SETTLED = frozenset({"sent", "empty", "failed", "unknown", "disabled", "not_due", "absent"})


class DeliveryRejected(Exception):
    """Discord confirmed the page was not delivered (permissions, missing channel, bad request)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class DeliveryUncertain(Exception):
    """The send may or may not have reached Discord (timeout, 5xx, dropped connection)."""


class MonthlyReportService:
    def __init__(self, repository=None, *, clock=time.time, lease_seconds=600, send_attempts=3,
                 recheck_delay=5.0, retry_seconds=300):
        self.repository = repository or SocialMonthlyReportRepository()
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.send_attempts = send_attempts
        self.recheck_delay = recheck_delay
        self.retry_seconds = retry_seconds
        self._lock = asyncio.Lock()
        # Due month for which every enabled guild is settled; lets idle hourly wake-ups skip the DB.
        self._settled_month = None
        self._settled_guilds = set()

    async def _db(self, operation, *args, **kwargs):
        name = getattr(operation, "__name__", "db")
        phase = "read" if name in {"get_run", "latest_runs", "month_events", "get_settings",
                                   "enabled_guilds"} else "write"
        try:
            result = await asyncio.to_thread(operation, *args, **kwargs)
        except LostClaim:
            raise
        except Exception as exc:
            logger.warning("Monthly report DB operation failed: operation=%s error_type=%s",
                           name, type(exc).__name__)
            database_health.failed(HEALTH_NAME, getattr(self.repository, "backend", "unknown"), phase, error=exc)
            raise
        database_health.succeeded(HEALTH_NAME, getattr(self.repository, "backend", "unknown"), phase)
        return result

    # Settings -----------------------------------------------------------------
    @staticmethod
    def effective_channel(settings):
        return settings.get("channel_id") if settings else None

    async def settings(self, guild_id):
        now = self.clock()
        return await self._db(self.repository.ensure_settings, str(guild_id),
                              first_report_month=month_of(now), now=now)

    async def runs(self, guild_id, limit=3):
        return await self._db(self.repository.latest_runs, str(guild_id), limit)

    async def configure(self, guild_id, *, state=None, channel_id=None, updated_by):
        """Disabled by default; enabling needs a channel chosen by an administrator."""
        if state not in {None, "enabled", "disabled"}:
            raise ValueError("不支援的狀態。")
        current = await self.settings(guild_id)
        enabled, first_month = current["enabled"], current["first_report_month"]
        channel = current["channel_id"] if channel_id is None else str(channel_id)
        if state == "enabled" and not channel:
            raise ValueError("啟用前請同時指定月報頻道。")
        if state in {"enabled", "disabled"}:
            if state == "enabled" and not enabled:
                # Enabling starts at the next boundary; disabled months are not backfilled.
                first_month = month_of(self.clock())
            enabled = state == "enabled"
        self._settled_month = None
        return await self._db(self.repository.save_settings, str(guild_id), enabled=enabled,
                              channel_id=channel, first_report_month=first_month, updated_by=updated_by)

    # Reports ------------------------------------------------------------------
    async def build_report(self, guild_id, report_month, *, period_end=None):
        lo, hi = month_bounds(report_month)
        if period_end is not None:
            hi = min(hi, period_end)
        shares, patrols = await self._db(self.repository.month_events, str(guild_id), lo, hi)
        return compute_report(str(guild_id), report_month, [ShareRow(*row) for row in shares],
                              [PatrolRow(*row) for row in patrols], period_end=hi)

    async def _names(self, report, transport):
        missing = [user for user in report.member_ids() if user not in report.snapshot_names]
        if not missing:
            return {}
        try:
            return await asyncio.wait_for(transport.resolve_names(report.guild_id, missing), timeout=20)
        except Exception as exc:
            logger.warning("Monthly report name lookup failed: error_type=%s", type(exc).__name__)
            return {}

    async def tick(self, transport):
        """Process the most recent due month for every enabled guild; returns seconds to the next check."""
        now = self.clock()
        until_boundary = max(1.0, next_boundary(now) - now + 1.0)
        month = due_month(now)
        if self._settled_month == month:
            return until_boundary
        try:
            guilds = await self._db(self.repository.enabled_guilds, month)
        except Exception:
            return self.retry_seconds
        self._settled_guilds = {key for key in self._settled_guilds if key[1] == month}
        pending = False
        for guild_id in guilds:
            if (guild_id, month) in self._settled_guilds:
                continue
            try:
                outcome = await self.process_month(guild_id, month, transport, scheduled=True)
            except Exception as exc:
                # One guild's failure never blocks the others.
                logger.warning("Monthly report run failed: error_type=%s", type(exc).__name__)
                pending = True
                continue
            if outcome in SETTLED:
                self._settled_guilds.add((guild_id, month))
            else:
                pending = True
        if pending:
            return self.retry_seconds
        self._settled_month = month
        return until_boundary

    async def run_forever(self, transport, *, wait_ready=None):
        if wait_ready is not None:
            await wait_ready()
        while True:
            delay = await self.tick(transport)
            # Re-check at least hourly, robust to clock changes and long sleeps.
            await asyncio.sleep(min(max(delay, 1.0), 3600.0))

    async def process_month(self, guild_id, report_month, transport, *, scheduled=False):
        guild_id = str(guild_id)
        async with self._lock:
            settings = await self.settings(guild_id)
            if scheduled:
                if not settings["enabled"]:
                    return "disabled"
                if report_month < settings["first_report_month"]:
                    return "not_due"
                if not transport.has_guild(guild_id):
                    # The bot left this guild: skip quietly instead of a failed run every month.
                    logger.info("Monthly report skipped: bot is not in the enabled guild")
                    return "absent"
            existing = await self._db(self.repository.get_run, guild_id, report_month)
            if existing and existing["status"] != "pending":
                return existing["status"]
            now = self.clock()
            lo, hi = month_bounds(report_month)
            token = uuid4().hex
            run = await self._db(
                self.repository.claim_run, guild_id, report_month, token=token, report_id=uuid4().hex[:8],
                period_start=lo, period_end=hi, channel_id=self.effective_channel(settings),
                now=now, lease_seconds=self.lease_seconds)
            if run is None:
                # Another worker holds a live lease; check again later.
                return "busy"
            try:
                return await self._run_claimed(run, token, transport)
            except LostClaim:
                logger.warning("Monthly report claim lost: month=%s", report_month)
                return "busy"

    def _lease(self):
        return self.clock() + self.lease_seconds

    async def _run_claimed(self, run, token, transport):
        guild_id, month = run["guild_id"], run["report_month"]
        if run["pages"] is None:
            try:
                report = await self.build_report(guild_id, month)
            except Exception:
                await self._release(run, token)
                raise
            if report.is_empty:
                await self._db(self.repository.finish, guild_id, month, token, status="empty")
                logger.info("Monthly report month had no data: month=%s", month)
                return "empty"
            pages = render_pages(report, await self._names(report, transport))
            await self._db(self.repository.store_pages, guild_id, month, token, pages,
                           lease_expires_at=self._lease())
            run = {**run, "pages": pages, "message_ids": [None] * len(pages), "sending_page": None}
        return await self._deliver(run, token, transport)

    async def _release(self, run, token):
        try:
            await self._db(self.repository.release, run["guild_id"], run["report_month"], token)
        except Exception:
            pass

    async def _finish(self, run, token, transport, status, reason, ids):
        await self._db(self.repository.finish, run["guild_id"], run["report_month"], token,
                       status=status, error_type=reason)
        delivered = sum(1 for value in ids if value)
        hint = ("無法確認是否已送達，排程不會自動重送；請到目的地頻道確認後手動處理。"
                if status == "unknown" else
                "已確認未送達，排程不會自動重送；修正頻道或權限後請手動處理。")
        await self._notify(transport, run["guild_id"], run["report_month"], run["channel_id"],
                           status, reason, hint=hint, progress=f"{delivered}/{len(ids)}")
        return status

    async def _notify(self, transport, guild_id, month, channel_id, status, reason, *, hint, progress=None):
        lines = ["🟠 **社群出警月報需要處理**",
                 f"伺服器: `{guild_id}`｜月份: `{month}`" + (f"｜頻道: <#{channel_id}>" if channel_id else ""),
                 f"狀態: `{status}`｜原因: `{reason}`" + (f"｜已確認送達頁: `{progress}`" if progress else ""),
                 hint]
        try:
            await asyncio.wait_for(transport.notify_admin("\n".join(lines)), timeout=10)
        except Exception as exc:
            logger.warning("Monthly report admin notice failed: error_type=%s", type(exc).__name__)

    async def _deliver(self, run, token, transport):
        guild_id, month, channel = run["guild_id"], run["report_month"], run["channel_id"]
        pages, ids = run["pages"], list(run["message_ids"] or [None] * len(run["pages"]))
        if not channel:
            return await self._finish(run, token, transport, "failed", "NoDestination", ids)
        after = run["created_at"] - 60
        stale = run.get("sending_page")
        if stale is not None and 0 <= stale < len(ids) and not ids[stale]:
            # A previous process stopped between sending and recording this page.
            try:
                found = await transport.find_page(guild_id, channel, pages[stale], after=after)
            except Exception as exc:
                return await self._finish(run, token, transport, "unknown", f"Reconcile{type(exc).__name__}", ids)
            if not found:
                return await self._finish(run, token, transport, "unknown", "UnconfirmedAfterRestart", ids)
            ids[stale] = str(found)
            await self._db(self.repository.confirm_page, guild_id, month, token, ids)
        for index, page in enumerate(pages):
            if ids[index]:
                continue
            for attempt in range(1, self.send_attempts + 1):
                await self._db(self.repository.begin_page, guild_id, month, token, index,
                               lease_expires_at=self._lease())
                try:
                    message_id = await transport.send_page(guild_id, channel, page)
                except DeliveryRejected as exc:
                    return await self._finish(run, token, transport, "failed", exc.reason, ids)
                except (DeliveryUncertain, asyncio.TimeoutError):
                    await asyncio.sleep(self.recheck_delay)
                    try:
                        message_id = await transport.find_page(guild_id, channel, page, after=after)
                    except Exception as exc:
                        return await self._finish(run, token, transport, "unknown",
                                                  f"Reconcile{type(exc).__name__}", ids)
                    if not message_id:
                        # Confirmed absent shortly after a transient error: retry the same content.
                        if attempt < self.send_attempts:
                            continue
                        return await self._finish(run, token, transport, "failed", "RetryExhausted", ids)
                ids[index] = str(message_id)
                try:
                    await self._db(self.repository.confirm_page, guild_id, month, token, ids)
                except LostClaim:
                    raise
                except Exception:
                    # Sent but unrecorded: sending_page stays set, so recovery reconciles it.
                    await self._notify(transport, guild_id, month, channel, "pending", "RecordFailed",
                                       hint="訊息可能已送達但未寫回 DB；DB 恢復後會先比對頻道訊息，不直接重送。")
                    raise
                break
        await self._db(self.repository.finish, guild_id, month, token, status="sent")
        logger.info("Monthly report delivered: month=%s pages=%s", month, len(pages))
        return "sent"

    async def close(self):
        async with self._lock:
            await asyncio.to_thread(self.repository.close)


monthly_report_service = MonthlyReportService()


def describe_schedule(now: float) -> str:
    return taipei_datetime(next_boundary(now)).strftime("%Y/%m/%d %H:%M")
