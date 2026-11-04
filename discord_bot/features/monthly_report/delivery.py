"""Discord side of the monthly report: channel checks, sending, reconciliation and notices."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

import discord

from ..notifications.discord_forwarder import _resolve_int_env
from .service import DeliveryRejected, DeliveryUncertain

logger = logging.getLogger("discord_digest_bot")
SEND_TIMEOUT_SECONDS = 15
HISTORY_LIMIT = 100
NAME_LOOKUP_LIMIT = 25
REQUIRED_PERMISSIONS = ("view_channel", "send_messages", "read_message_history")


def missing_permissions(channel, member) -> list[str]:
    """Permission names the bot lacks in this channel; an unknown member lacks everything."""
    if member is None or not hasattr(channel, "permissions_for"):
        return list(REQUIRED_PERMISSIONS)
    permissions = channel.permissions_for(member)
    return [name for name in REQUIRED_PERMISSIONS if not getattr(permissions, name, False)]


def channel_problem(channel, guild_id) -> str | None:
    """Reason a channel cannot be a report destination for this guild, or None."""
    if channel is None:
        return "ChannelNotFound"
    guild = getattr(channel, "guild", None)
    if guild is None or str(getattr(guild, "id", "")) != str(guild_id):
        return "ChannelOutsideGuild"
    if not hasattr(channel, "send") or not hasattr(channel, "history"):
        return "ChannelNotText"
    if missing_permissions(channel, getattr(guild, "me", None)):
        return "MissingPermissions"
    return None


class DiscordReportTransport:
    def __init__(self, bot):
        self.bot = bot

    def has_guild(self, guild_id) -> bool:
        return self.bot.get_guild(int(guild_id)) is not None

    async def _channel(self, guild_id, channel_id):
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.NotFound:
                raise DeliveryRejected("ChannelNotFound") from None
            except discord.Forbidden:
                raise DeliveryRejected("Forbidden") from None
        problem = channel_problem(channel, guild_id)
        if problem:
            raise DeliveryRejected(problem)
        return channel

    async def send_page(self, guild_id, channel_id, content) -> str:
        channel = await self._channel(guild_id, channel_id)
        try:
            message = await asyncio.wait_for(channel.send(
                content, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none()),
                timeout=SEND_TIMEOUT_SECONDS)
        except discord.Forbidden:
            raise DeliveryRejected("Forbidden") from None
        except discord.NotFound:
            raise DeliveryRejected("ChannelNotFound") from None
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            if isinstance(status, int) and 400 <= status < 500:
                raise DeliveryRejected(f"HTTP{status}") from None
            raise DeliveryUncertain(type(exc).__name__) from None
        except (asyncio.TimeoutError, OSError) as exc:
            raise DeliveryUncertain(type(exc).__name__) from None
        except Exception as exc:
            if type(exc).__module__.startswith("aiohttp"):
                raise DeliveryUncertain(type(exc).__name__) from None
            raise
        return str(message.id)

    async def find_page(self, guild_id, channel_id, content, *, after) -> str | None:
        """Match the fixed page text written by this bot; errors mean "cannot confirm"."""
        channel = await self._channel(guild_id, channel_id)
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if bot_id is None:
            raise RuntimeError("Bot identity unavailable")
        since = datetime.fromtimestamp(max(after, 0), timezone.utc)
        expected = content.strip()
        async for message in channel.history(limit=HISTORY_LIMIT, after=since, oldest_first=True):
            if getattr(message.author, "id", None) == bot_id and (message.content or "").strip() == expected:
                return str(message.id)
        return None

    async def resolve_names(self, guild_id, user_ids) -> dict[str, str]:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return {}
        names, fetched = {}, 0
        for user_id in user_ids:
            if not str(user_id).isdecimal():
                continue
            member = guild.get_member(int(user_id))
            if member is None and fetched < NAME_LOOKUP_LIMIT:
                fetched += 1
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None
            name = getattr(member, "display_name", None) if member is not None else None
            if isinstance(name, str) and name.strip():
                names[str(user_id)] = name
        return names

    async def notify_admin(self, text) -> bool:
        channel_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_CHANNEL_ID")
        if channel_id is None:
            logger.warning("Monthly report notice skipped: notify channel not configured")
            return False
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        guild_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_GUILD_ID")
        if guild_id is not None and getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            logger.warning("Monthly report notice channel does not match configured guild; skipped.")
            return False
        await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
        return True
