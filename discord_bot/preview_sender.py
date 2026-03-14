from __future__ import annotations

import io
import logging
from typing import List, Optional, Sequence

import discord
from discord.errors import Forbidden, HTTPException, NotFound

logger = logging.getLogger("discord_digest_bot")
PREVIEW_WEBHOOK_NAME = "digest-preview-relay"


def _author_display_name(author: discord.abc.User) -> str:
    return (
        getattr(author, "display_name", None)
        or getattr(author, "global_name", None)
        or getattr(author, "name", None)
        or "Unknown User"
    )


def _clone_files(files: Sequence[discord.File]) -> List[discord.File]:
    clones: List[discord.File] = []
    for file in files:
        file.fp.seek(0)
        data = file.fp.read()
        file.fp.seek(0)
        clones.append(
            discord.File(
                io.BytesIO(data),
                filename=file.filename,
                spoiler=file.spoiler,
                description=file.description,
            )
        )
    return clones


async def _send_via_webhook(
    message: discord.Message,
    *,
    content: Optional[str],
    embed: Optional[discord.Embed],
    files: List[discord.File],
    view: Optional[discord.ui.View],
) -> Optional[discord.Message]:
    channel = message.channel
    guild = message.guild
    if guild is None:
        return None

    webhook_channel: Optional[discord.TextChannel] = None
    thread = None
    if isinstance(channel, discord.Thread):
        webhook_channel = channel.parent
        thread = channel
    elif isinstance(channel, discord.TextChannel):
        webhook_channel = channel

    if webhook_channel is None:
        return None

    me = guild.me
    if me is not None and not webhook_channel.permissions_for(me).manage_webhooks:
        return None

    try:
        hooks = await webhook_channel.webhooks()
    except (Forbidden, HTTPException):
        return None

    webhook = discord.utils.get(hooks, name=PREVIEW_WEBHOOK_NAME)
    if webhook is None:
        try:
            webhook = await webhook_channel.create_webhook(name=PREVIEW_WEBHOOK_NAME, reason="Relay social preview")
        except (Forbidden, HTTPException):
            return None

    send_kwargs = {
        "content": content,
        "embed": embed,
        "files": files if files else None,
        "view": view,
        "wait": True,
        "username": _author_display_name(message.author),
        "avatar_url": message.author.display_avatar.url if message.author.display_avatar else None,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    send_kwargs = {k: v for k, v in send_kwargs.items() if v is not None}
    if thread is not None:
        send_kwargs["thread"] = thread

    sent = await webhook.send(**send_kwargs)
    return sent


async def send_preview_as_author(
    message: discord.Message,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    files: Optional[Sequence[discord.File]] = None,
    view: Optional[discord.ui.View] = None,
) -> discord.Message:
    channel = message.channel
    source_files = list(files or [])

    fallback_files = source_files
    if source_files:
        try:
            fallback_files = _clone_files(source_files)
        except Exception:
            fallback_files = source_files

    try:
        webhook_sent = await _send_via_webhook(
            message,
            content=content,
            embed=embed,
            files=source_files,
            view=view,
        )
        if webhook_sent is not None:
            return webhook_sent
    except Exception as exc:
        logger.warning("Webhook 代發預覽失敗，改用 bot 發送：%s", exc)

    send_kwargs = {
        "content": content,
        "embed": embed,
        "files": fallback_files if fallback_files else None,
        "view": view,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    send_kwargs = {k: v for k, v in send_kwargs.items() if v is not None}
    return await channel.send(**send_kwargs)


async def cleanup_source_message(message: discord.Message, *, platform: str, url: str) -> None:
    try:
        await message.delete()
        return
    except (Forbidden, HTTPException, NotFound):
        pass

    try:
        await message.edit(suppress=True)
    except (Forbidden, HTTPException, NotFound):
        logger.warning("無法刪除或收合 %s 原始連結訊息：%s", platform, url)
