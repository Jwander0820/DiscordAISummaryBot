"""Optional share patrol; never modify originals or undo delivered previews."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import discord

from ...db.social_duplicate_repository import ShareEvent
from ...db.health import database_health
from .duplicate_identity import post_identity
from .duplicate_service import social_duplicate_service
from .duplicate_webhook import send_patrol_webhook
from .patrol_records import build_patrol_event, delivered

logger = logging.getLogger("discord_digest_bot")


async def _announce(message, previous, *, share_count=2):
    mentions = discord.AllowedMentions(everyone=False, users=[discord.Object(id=message.author.id)],
                                       roles=False, replied_user=False)
    content = f"🚨 逼逼！<@{message.author.id}>！你已經被不看訊息警察逮捕了，逼波逼波逼波。"
    delivery = await send_patrol_webhook(message, previous, content=content, mentions=mentions, share_count=share_count)
    if delivery:
        return delivery
    reference = discord.MessageReference(message_id=int(previous.preview_message_id),
                                         channel_id=int(previous.channel_id), guild_id=int(previous.guild_id))
    try:
        result = await message.channel.send(content, reference=reference, allowed_mentions=mentions, mention_author=False)
        return delivered("bot_reply", result)
    except discord.NotFound:
        result = await message.channel.send(content + "（先前的訊息已不存在。）", allowed_mentions=mentions)
        return delivered("bot_notice", result)
    except discord.HTTPException as exc:
        # UNKNOWN_MESSAGE_REFERENCE is not always returned as a 404.
        if getattr(exc, "code", None) != 10008 and getattr(exc, "code", None) != 50035:
            raise
        result = await message.channel.send(content + "（目前無法回覆先前的訊息。）", allowed_mentions=mentions)
        return delivered("bot_notice", result)


async def _write_patrol(operation, *args, **kwargs):
    """Audit failures must not suppress a previously valid patrol announcement."""
    repository = getattr(social_duplicate_service, "patrol_repository", None)
    try:
        result = await asyncio.to_thread(getattr(repository, operation), *args, **kwargs)
        if not result:
            return False
        database_health.succeeded("social_patrol_events", repository.backend, "write")
        return True
    except Exception as exc:
        logger.warning("Social patrol audit write failed: operation=%s error_type=%s", operation, type(exc).__name__)
        database_health.failed("social_patrol_events", getattr(repository, "backend", "unknown"), "write", error=exc)
        return False


async def _announce_recorded(message, event, match):
    record = build_patrol_event(message, event, match)
    saved = await _write_patrol("insert_event", record)
    outcome = {"status": "unknown", "method": None, "message_id": None, "error_type": None}
    try:
        delivery = await asyncio.wait_for(_announce(message, match.previous, share_count=match.share_count), timeout=5)
        outcome.update(status="sent", method=delivery.method, message_id=delivery.message_id)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        outcome["error_type"] = type(exc).__name__
        raise
    except Exception as exc:
        outcome.update(status="failed", error_type=re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64])
        raise
    finally:
        if saved:
            await _write_patrol("finish_event", record["event_id"], completed_at=time.time(), **outcome)


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
            await _announce_recorded(message, event, match)
    except Exception as exc:
        # No URL, user text, credentials or raw API exception in diagnostic logs.
        logger.warning("Social duplicate patrol failed: error_type=%s", type(exc).__name__)
