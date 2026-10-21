"""Components V2 patrol cards with per-message CDN avatars and GIF media."""
from __future__ import annotations

import logging

import discord
from .patrol_records import PatrolDelivery, delivered
from .patrol_taunts import COUNT_TAUNTS, fixed_count_comment

logger = logging.getLogger("discord_digest_bot")
PATROL_NAME = "不看訊息警察"
PATROL_WEBHOOK_NAME = "digest-duplicate-patrol"
PATROL_AVATAR_URL = "https://cdn.jwander.net/assets/images/alarm-dog.png"
REPEAT_WEBHOOK_NAME = "digest-duplicate-patrol-repeat"
REPEAT_AVATAR_URL = "https://cdn.jwander.net/assets/images/message-police.jpg"
PATROL_GIF_URL = "https://cdn.jwander.net/assets/gifs/alarm-dog-crop.gif"
REPEAT_AVATAR_FROM_SHARE_COUNT = 3


def _count_comment(share_count: int) -> str:
    return fixed_count_comment(share_count)


def build_patrol_view(content: str, jump_url: str, *, share_count=2, taunt_text=None) -> discord.ui.LayoutView:
    """No user-controlled headings, media URLs, or interactive callbacks."""
    section = discord.ui.Section(
        discord.ui.TextDisplay(f"**{PATROL_NAME}**"),
        discord.ui.TextDisplay(content),
        discord.ui.TextDisplay(f"⬆️ [查看原始連結]({jump_url})" +
                               (_count_comment(share_count) if taunt_text is None else (" " + taunt_text if taunt_text else ""))),
        accessory=discord.ui.Thumbnail(PATROL_GIF_URL, description="警報狗 GIF"),
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(section, accent_colour=0xFF6600))
    return view


async def send_patrol_webhook(message, previous, *, content: str, mentions, share_count=2,
                             taunt_text=None) -> PatrolDelivery | bool:
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
    avatar_url = REPEAT_AVATAR_URL if repeated else PATROL_AVATAR_URL
    try:
        hooks = await parent.webhooks()
        hook = next((item for item in hooks
                     if item.name == webhook_name and item.type is discord.WebhookType.incoming
                     and item.user is not None and item.user.id == me.id), None)
        if hook is None:
            hook = await parent.create_webhook(
                name=webhook_name, reason="Social duplicate patrol identity")
        # Incoming webhooks cannot create native replies. Link the original share reference.
        jump_url = f"https://discord.com/channels/{previous.guild_id}/{previous.channel_id}/{previous.preview_message_id}"
        kwargs = dict(username=PATROL_NAME, avatar_url=avatar_url, allowed_mentions=mentions, wait=True,
                      view=build_patrol_view(content, jump_url, share_count=share_count, taunt_text=taunt_text))
        if thread is not None:
            kwargs["thread"] = thread
        result = await hook.send(**kwargs)
        return delivered("webhook", result)
    except Exception as exc:
        # Do not log the webhook object, token, URL, payload or raw API error.
        logger.warning("Social duplicate webhook unavailable: error_type=%s", type(exc).__name__)
        return False
