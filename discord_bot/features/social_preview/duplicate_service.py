"""Same-channel patrol: persistent events and a rebuildable in-memory index."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from ...db.health import database_health
from ...db.social_duplicate_repository import ShareEvent, SocialDuplicateRepository, report_failure

logger = logging.getLogger("discord_digest_bot")


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    previous: ShareEvent
    share_count: int


class SocialDuplicateService:
    def __init__(self, repository=None):
        self.repository = repository or SocialDuplicateRepository()
        self.ready = False
        self.settings = {}
        self.cache = {}
        self.counts = {}
        self._lock = asyncio.Lock()
        self.default_enabled = True
        self.default_days = 7

    def _read_defaults(self):
        self.default_enabled = os.getenv("SOCIAL_DUPLICATE_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on", "enabled"}
        try:
            self.default_days = int(os.getenv("SOCIAL_DUPLICATE_WINDOW_DAYS", "7"))
            self.validate_days(self.default_days)
        except ValueError:
            self.default_days = 7
            logger.warning("Invalid SOCIAL_DUPLICATE_WINDOW_DAYS; using 7")

    @staticmethod
    def validate_days(days):
        # INTEGER in PostgreSQL, plus a safe range for cutoff arithmetic.
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 2147483647:
            raise ValueError("天數需為 1～2147483647 的整數。")

    def effective(self, guild_id):
        return self.settings.get(str(guild_id), (self.default_enabled, self.default_days))

    async def _load(self, guild_id=None):
        self.ready = False
        self._read_defaults()
        try:
            settings, events = await asyncio.to_thread(
                self.repository.snapshot, enabled=self.default_enabled, days=self.default_days,
                now=time.time(), guild_id=guild_id)
        except Exception as exc:
            report_failure(self.repository, exc, "read")
            return False
        cache = {}
        counts = {}
        for event in events:
            cache.setdefault(event.key, event)
            counts[event.key] = counts.get(event.key, 0) + 1
        if guild_id is not None:
            cache = {**{key: value for key, value in self.cache.items() if key[0] != guild_id},
                     **{key: value for key, value in cache.items() if key[0] == guild_id}}
            counts = {**{key: value for key, value in self.counts.items() if key[0] != guild_id},
                      **{key: value for key, value in counts.items() if key[0] == guild_id}}
        self.settings, self.cache, self.counts = settings, cache, counts
        self.ready = True
        database_health.succeeded("social_share_events", self.repository.backend, "read")
        logger.info("Social duplicate cache ready: entries=%s", len(cache))
        return True

    async def rebuild(self):
        async with self._lock:
            return await self._load()

    async def recover(self):
        while True:
            await asyncio.sleep(30)
            if not self.ready:
                await self.rebuild()

    async def configure(self, guild_id, *, state=None, days=None, updated_by):
        if days is not None:
            self.validate_days(days)
        if state not in {None, "enabled", "disabled", "default"}:
            raise ValueError("不支援的狀態。")
        if state == "default" and days is not None:
            raise ValueError("恢復預設時請不要同時指定天數。")
        async with self._lock:
            if not self.ready and not await self._load():
                return False
            enabled, current_days = self.effective(guild_id)
            setting = None if state == "default" else (
                enabled if state is None else state == "enabled", current_days if days is None else days)
            try:
                await asyncio.to_thread(self.repository.configure, str(guild_id), setting, updated_by=str(updated_by))
            except Exception as exc:
                self.ready = False
                report_failure(self.repository, exc, "write")
                return False
            return await self._load(str(guild_id))

    async def register(self, event: ShareEvent):
        async with self._lock:
            if not self.ready or not self.effective(event.guild_id)[0]:
                return None
            previous = self.cache.get(event.key)
            try:
                inserted = await asyncio.to_thread(self.repository.insert, event)
            except Exception as exc:
                self.ready = False
                report_failure(self.repository, exc, "write")
                return None
            if not inserted:
                return None
            self.cache.setdefault(event.key, event)
            self.counts[event.key] = self.counts.get(event.key, 0) + 1
            # Re-delivery of the same source must never accuse itself.
            if previous and previous.source_message_id != event.source_message_id:
                return DuplicateMatch(previous, self.counts[event.key])
            return None

    async def close(self):
        async with self._lock:
            self.ready = False
            await asyncio.to_thread(self.repository.close)


social_duplicate_service = SocialDuplicateService()
