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


@lru_cache(maxsize=1)
def _avatar_bytes() -> bytes:
    return PATROL_AVATAR_PATH.read_bytes()


async def send_patrol_webhook(message, previous, *, content: str, mentions) -> bool:
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
    try:
        hooks = await parent.webhooks()
        hook = next((item for item in hooks
                     if item.name == PATROL_WEBHOOK_NAME and item.type is discord.WebhookType.incoming
                     and item.user is not None and item.user.id == me.id), None)
        if hook is None:
            hook = await parent.create_webhook(name=PATROL_WEBHOOK_NAME, avatar=_avatar_bytes(),
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
