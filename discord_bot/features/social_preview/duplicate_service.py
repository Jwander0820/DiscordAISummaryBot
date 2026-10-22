"""Share patrol with explicit cross-channel scopes and rebuildable history."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from ...db.health import database_health
from ...db.social_duplicate_repository import ShareEvent, SocialDuplicateRepository, report_failure
from ...db.social_patrol_repository import SocialPatrolRepository

logger = logging.getLogger("discord_digest_bot")


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    previous: ShareEvent
    share_count: int


class SocialDuplicateService:
    def __init__(self, repository=None):
        self.repository = repository or SocialDuplicateRepository()
        self.patrol_repository = SocialPatrolRepository(self.repository)
        self.ready = False
        self.settings = {}
        self.cross_settings = {}
        # Discord cog supplies current text-channel IDs; no Discord dependency here.
        self.text_channel_ids = None
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

    def cross_effective(self, guild_id):
        return self.cross_settings.get(str(guild_id), (False, frozenset()))

    def cross_channels(self, guild_id):
        enabled, channels = self.cross_effective(guild_id)
        if not enabled:
            return frozenset()
        valid = frozenset(self.text_channel_ids(str(guild_id))) if self.text_channel_ids else frozenset()
        channels = channels & valid
        return channels if len(channels) >= 2 else frozenset()

    async def _load(self, guild_id=None):
        self.ready = False
        self._read_defaults()
        try:
            settings, events = await asyncio.to_thread(
                self.repository.snapshot, enabled=self.default_enabled, days=self.default_days,
                now=time.time(), guild_id=guild_id)
            cross_settings = await asyncio.to_thread(self.repository.cross_channel_settings)
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
        self.cross_settings = cross_settings
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

    async def configure_cross_channel(self, guild_id, *, enabled, channel_ids, updated_by, expected=None):
        guild_id = str(guild_id)
        channels = frozenset(str(channel) for channel in channel_ids)
        if not isinstance(enabled, bool) or any(not channel.isdecimal() for channel in channels):
            raise ValueError("跨頻道設定格式不正確。")
        async with self._lock:
            if not self.ready and not await self._load():
                return False
            if expected is not None and expected != self.cross_effective(guild_id):
                raise ValueError("設定已被其他操作更新，請重新開啟面板。")
            valid = set(self.text_channel_ids(guild_id)) if self.text_channel_ids else set()
            if not channels <= valid:
                raise ValueError("清單包含已刪除或非一般文字頻道，請移除後儲存。")
            try:
                await asyncio.to_thread(self.repository.configure_cross_channel, guild_id,
                                       enabled=enabled, channel_ids=channels, updated_by=str(updated_by))
            except Exception as exc:
                self.ready = False
                report_failure(self.repository, exc, "write")
                return False
            return await self._load(guild_id)

    async def register(self, event: ShareEvent):
        async with self._lock:
            if not self.ready or not self.effective(event.guild_id)[0]:
                return None
            channels = self.cross_channels(event.guild_id)
            keys = [(event.guild_id, channel, event.platform, event.post_key) for channel in channels] \
                if event.channel_id in channels else [event.key]
            candidates = [self.cache[key] for key in keys if key in self.cache]
            previous = min(candidates, key=lambda item: (item.occurred_at, int(item.source_message_id))) if candidates else None
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
                return DuplicateMatch(previous, sum(self.counts.get(key, 0) for key in keys))
            return None

    async def forget_previews(self, guild_id, channel_id, message_ids):
        """Synchronize explicit preview button deletions."""
        return await self._forget_messages(self.repository.delete_previews, guild_id, channel_id, message_ids)

    async def forget_original_messages(self, guild_id, channel_id, message_ids):
        """Synchronize raw deletions of X/YouTube original messages."""
        return await self._forget_messages(
            self.repository.delete_original_messages, guild_id, channel_id, message_ids, log_originals=True)

    async def _forget_messages(self, delete, guild_id, channel_id, message_ids, *, log_originals=False):
        """Only refresh affected keys, keeping unrelated runtime windows intact."""
        async with self._lock:
            try:
                deleted = await asyncio.to_thread(
                    delete, str(guild_id), str(channel_id), message_ids)
                updates = {}
                for key in {event.key for event in deleted}:
                    first = self.cache.get(key)
                    if first is not None:
                        # Keep the window loaded at startup/configuration, including
                        # shares that have aged beyond it during this process.
                        updates[key] = await asyncio.to_thread(
                            self.repository.remaining_shares, key, since=first.occurred_at)
            except Exception as exc:
                self.ready = False
                report_failure(self.repository, exc, "write")
                return False
            for key, remaining in updates.items():
                if remaining:
                    self.cache[key] = remaining[0]
                    self.counts[key] = len(remaining)
                else:
                    self.cache.pop(key, None)
                    self.counts.pop(key, None)
            database_health.succeeded("social_share_events", self.repository.backend, "write")
            if log_originals:
                for event in deleted:
                    label = {"x": "X", "youtube": "YouTube"}[event.platform]
                    fields = [str(value).replace("\r", " ").replace("\n", " ") for value in (
                        event.guild_id, event.channel_id, event.source_message_id, event.canonical_url)]
                    logger.info(
                        "%s 連結已刪除，分享紀錄與快取已更新：guild_id=%s channel_id=%s message_id=%s url=%s",
                        label, *fields)
            return True

    async def close(self):
        async with self._lock:
            self.ready = False
            await asyncio.to_thread(self.repository.close)


social_duplicate_service = SocialDuplicateService()
