"""Fixed patrol identity backed by an image bundled in the application image."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import discord

logger = logging.getLogger("discord_digest_bot")
PATROL_NAME = "不看訊息警察"
PATROL_WEBHOOK_NAME = "digest-duplicate-patrol"
PATROL_AVATAR_PATH = Path(__file__).resolve().parents[2] / "assets" / "social_preview" / "alarm_dog.jpg"
REPEAT_WEBHOOK_NAME = "digest-duplicate-patrol-repeat"
REPEAT_AVATAR_PATH = PATROL_AVATAR_PATH.with_name("message_police.jpg")
REPEAT_AVATAR_FROM_SHARE_COUNT = 3


@lru_cache(maxsize=2)
def _avatar_bytes(path=PATROL_AVATAR_PATH) -> bytes:
    return path.read_bytes()


async def send_patrol_webhook(message, previous, *, content: str, mentions, share_count=2) -> bool:
    """Use a dedicated bot-owned webhook; let the caller provide bot fallback."""
    channel = message.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    parent = channel.parent if thread is not None else channel
    if not isinstance(parent, discord.TextChannel):
        return False
    guild = message.guild
    me = getattr(guild, "me", None)
    if me is None or not parent.permissions_for(me).manage_webhooks:
        return False
    repeated = share_count >= REPEAT_AVATAR_FROM_SHARE_COUNT
    webhook_name = REPEAT_WEBHOOK_NAME if repeated else PATROL_WEBHOOK_NAME
    avatar_path = REPEAT_AVATAR_PATH if repeated else PATROL_AVATAR_PATH
    try:
        hooks = await parent.webhooks()
        hook = next((item for item in hooks
                     if item.name == webhook_name and item.type is discord.WebhookType.incoming
                     and item.user is not None and item.user.id == me.id), None)
        if hook is None:
            hook = await parent.create_webhook(name=webhook_name, avatar=_avatar_bytes(avatar_path),
                                                reason="Social duplicate patrol identity")
        # Incoming webhooks cannot create native replies. Keep the old preview reachable.
        jump_url = f"https://discord.com/channels/{previous.guild_id}/{previous.channel_id}/{previous.preview_message_id}"
        kwargs = dict(username=PATROL_NAME, allowed_mentions=mentions, wait=True, suppress_embeds=True)
        if thread is not None:
            kwargs["thread"] = thread
        await hook.send(content + f"\n[查看原始預覽]({jump_url})", **kwargs)
        return True
    except Exception as exc:
        # Do not log the webhook object, token, URL, payload or raw API error.
        logger.warning("Social duplicate webhook unavailable: error_type=%s", type(exc).__name__)
        return False
