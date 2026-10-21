"""Optional share patrol; never modify originals or undo delivered previews."""
from __future__ import annotations

import asyncio
import logging
import time

import discord

from ...db.social_duplicate_repository import ShareEvent
from .duplicate_identity import post_identity
from .duplicate_service import social_duplicate_service
from .duplicate_webhook import send_patrol_webhook

logger = logging.getLogger("discord_digest_bot")


async def _announce(message, previous, *, share_count=2):
    mentions = discord.AllowedMentions(everyone=False, users=[discord.Object(id=message.author.id)],
                                       roles=False, replied_user=False)
    content = f"🚨 逼逼！<@{message.author.id}>！你已經被不看訊息警察逮捕了，逼波逼波逼波。"
    if await send_patrol_webhook(message, previous, content=content, mentions=mentions, share_count=share_count):
        return
    reference = discord.MessageReference(message_id=int(previous.preview_message_id),
                                         channel_id=int(previous.channel_id), guild_id=int(previous.guild_id))
    try:
        await message.channel.send(content, reference=reference, allowed_mentions=mentions, mention_author=False)
    except discord.NotFound:
        await message.channel.send(content + "（先前的訊息已不存在。）", allowed_mentions=mentions)
    except discord.HTTPException as exc:
        # UNKNOWN_MESSAGE_REFERENCE is not always returned as a 404.
        if getattr(exc, "code", None) != 10008 and getattr(exc, "code", None) != 50035:
            raise
        await message.channel.send(content + "（目前無法回覆先前的訊息。）", allowed_mentions=mentions)


async def record_delivered_preview(message, *, platform, url, sent_messages, preview_error=None):
    if preview_error or not getattr(message, "guild", None) or not sent_messages:
        return
    await _record_share(message, platform=platform, url=url, reference_message=sent_messages[0])


async def record_original_share(message, *, platform, url):
    """Record a human's original message without preview, deletion, edit or suppression."""
    if (not getattr(message, "guild", None) or message.author.bot
            or getattr(message, "webhook_id", None) is not None):
        return
    await _record_share(message, platform=platform, url=url, reference_message=message)


async def _record_share(message, *, platform, url, reference_message):
    try:
        identity = post_identity(platform, url)
        if not identity:
            return
        key, canonical = identity
        event = ShareEvent(str(message.guild.id), str(message.channel.id), platform, str(message.id),
                           str(message.author.id), key, canonical, str(reference_message.id), time.time())
        match = await social_duplicate_service.register(event)
        if match:
            await asyncio.wait_for(_announce(message, match.previous, share_count=match.share_count), timeout=5)
    except Exception as exc:
        # No URL, user text, credentials or raw API exception in diagnostic logs.
        logger.warning("Social duplicate patrol failed: error_type=%s", type(exc).__name__)
