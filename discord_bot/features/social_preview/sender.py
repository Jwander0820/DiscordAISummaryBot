from __future__ import annotations

import io
import logging
from typing import List, Optional, Sequence

import discord
from discord.errors import Forbidden, HTTPException, InteractionResponded, NotFound

logger = logging.getLogger("discord_digest_bot")
PREVIEW_WEBHOOK_NAME = "digest-preview-relay"


def log_preview_link(message, *, platform: str, url: str) -> None:
    """One consistent activity line for every supported social preview."""
    author = getattr(message.author, "nick", None) or _author_display_name(message.author)
    channel = getattr(message.channel, "name", None) or "私人訊息"
    # Display names are user-controlled; keep the activity record on one line.
    author, channel = (str(value).replace("\r", " ").replace("\n", " ") for value in (author, channel))
    logger.info("%s 在 %s 貼了 %s url %s", author, channel, platform, url)


def _author_display_name(author: discord.abc.User) -> str:
    """Pick the most user-friendly display name available for webhook relays."""
    return (
        getattr(author, "display_name", None)
        or getattr(author, "global_name", None)
        or getattr(author, "name", None)
        or "Unknown User"
    )


def _clone_files(files: Sequence[discord.File]) -> List[discord.File]:
    """Clone file buffers so webhook fallback can resend the same attachments safely."""
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
    view: Optional[discord.ui.View | discord.ui.LayoutView],
) -> Optional[discord.Message]:
    """Try to send the preview via webhook so it appears as the original author."""
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

    interactive_v2 = view is not None and view.has_components_v2() and view.is_dispatchable()
    if interactive_v2:
        # discord.py exposes the creator as user, but not the API's
        # application_id. A webhook created by this bot belongs to its app
        # and can deliver interactive V2 components using the client's state.
        bot_id = getattr(me, "id", None)
        webhook = next((
            hook for hook in hooks
            if hook.name == PREVIEW_WEBHOOK_NAME
            and hook.type is discord.WebhookType.incoming
            and hook.user is not None
            and hook.user.id == bot_id
        ), None)
    else:
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
    view: Optional[discord.ui.View | discord.ui.LayoutView] = None,
) -> discord.Message:
    """Relay previews as the Discord author, falling back to the bot if unavailable."""
    channel = message.channel
    source_files = list(files or [])

    if view is not None and view.has_components_v2():
        if content is not None or embed is not None:
            raise ValueError("Components V2 previews must put all text in the view")

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


async def send_preview_group(message: discord.Message, views: Sequence) -> None:
    """Send every V2 page, link their delete buttons, and roll back partial groups."""
    sent_messages = []
    try:
        for view in views:
            kwargs = {"view": view}
            if view.files:
                kwargs["files"] = list(view.files)
            sent = await send_preview_as_author(message, **kwargs)
            view.message = sent
            sent_messages.append(sent)
            for sent_view in views:
                if sent_view.message is not None:
                    sent_view.related_message_ids = [
                        item.id for item in sent_messages if item.id != sent_view.message.id
                    ]
    except Exception:
        for view in views:
            view.stop()
        for sent in sent_messages:
            try:
                await sent.delete()
            except (NotFound, HTTPException):
                logger.warning("無法清理未完整送出的社群 V2 預覽", exc_info=True)
        raise
    finally:
        for view in views:
            for file in view.files:
                file.close()
            # Interactive views may live until restart; retain only CDN/attachment
            # references after sending, not the in-memory image buffers.
            view.files.clear()


async def delete_preview_messages(
    interaction: discord.Interaction, related_message_ids: Sequence[int]
) -> None:
    """Delete a social preview and its linked continuation messages."""
    try:
        await interaction.response.send_message("預覽已刪除。", ephemeral=True)
    except InteractionResponded:
        pass

    channel = getattr(interaction.message, "channel", None)
    deleter = getattr(interaction.user, "global_name", None)
    try:
        await interaction.message.delete()
    except (NotFound, HTTPException):
        logger.info("社群預覽刪除失敗：訊息不存在或無法刪除 %s 想刪除 %s 內的預覽", deleter, channel)
        return

    if channel:
        for message_id in list(related_message_ids):
            try:
                related = await channel.fetch_message(message_id)
                await related.delete()
            except (NotFound, HTTPException, AttributeError):
                continue
    logger.info("社群預覽已刪除 %s 刪除了 %s 內的預覽", deleter, channel)


async def cleanup_source_message(message: discord.Message, *, platform: str, url: str) -> None:
    """Delete the original source message, or suppress embeds if delete is not allowed."""
    try:
        await message.delete()
        return
    except (Forbidden, HTTPException, NotFound):
        pass

    try:
        await message.edit(suppress=True)
    except (Forbidden, HTTPException, NotFound):
        logger.warning("無法刪除或收合 %s 原始連結訊息：%s", platform, url)
