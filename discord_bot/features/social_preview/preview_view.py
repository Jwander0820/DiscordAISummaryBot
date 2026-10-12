"""Shared Components V2 cards for Threads, Facebook and Instagram previews."""
from __future__ import annotations

from typing import Optional, Sequence

import discord
from discord.errors import HTTPException, NotFound

from .sender import delete_preview_messages


MAX_TEXT_UNITS = 4000


def _escape_link_label(text: str) -> str:
    # Masked-link labels need their delimiters escaped. Applying the general
    # Markdown escaper also inserts visible backslashes before username `_`.
    return (text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            .replace("\r\n", " ").replace("\r", " ").replace("\n", " "))


def _text_units(text: str) -> int:
    # Stay within Discord's limit even when the text contains astral emoji.
    return len(text.encode("utf-16-le")) // 2


def _take_text(text: str, budget: int) -> tuple[str, str]:
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            return text[:index], text[index:]
    return text, ""


class SocialPreviewView(discord.ui.LayoutView):
    """One preview card or continuation; every page can delete the entire group."""

    def __init__(
        self,
        *,
        original_url: str,
        commentary: str = "",
        header: str = "",
        text: str = "",
        video_url: Optional[str] = None,
        media_urls: Sequence[str] = (),
        files: Sequence[discord.File] = (),
        spoiler: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.files = list(files)
        self.message: Optional[discord.Message] = None
        self.related_message_ids: list[int] = []

        if commentary:
            self.add_item(discord.ui.TextDisplay(commentary))

        card = None
        if header or text or media_urls:
            card = discord.ui.Container(spoiler=spoiler)
            if header:
                card.add_item(discord.ui.TextDisplay(header))
            if text:
                card.add_item(discord.ui.TextDisplay(text))
            if media_urls:
                card.add_item(discord.ui.MediaGallery(*(
                    discord.MediaGalleryItem(media=url, spoiler=spoiler) for url in media_urls
                )))
            self.add_item(card)

        controls = discord.ui.ActionRow()
        delete_button = discord.ui.Button(label="", style=discord.ButtonStyle.gray, emoji="🗑️")
        delete_button.callback = self._delete
        controls.add_item(delete_button)
        controls.add_item(discord.ui.Button(label="原連結", style=discord.ButtonStyle.link, url=original_url))
        if video_url and len(video_url) <= 512:
            controls.add_item(discord.ui.Button(label="影片直連", style=discord.ButtonStyle.link, url=video_url))

        # A spoiler must not force the user to reveal the post just to delete it.
        if card is not None and not spoiler:
            card.add_item(controls)
        else:
            self.add_item(controls)

    async def _delete(self, interaction: discord.Interaction) -> None:
        await delete_preview_messages(interaction, self.related_message_ids)

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button) and item.url is None:
                item.disabled = True
        if self.message is not None:
            try:
                # Removing the view would also remove the text and the video.
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())
            except (NotFound, HTTPException):
                pass


def build_social_preview_views(
    *,
    original_url: str,
    title: str,
    author: str,
    text: str,
    commentary: str,
    spoiler: bool,
    timeout: Optional[float],
    video_url: Optional[str] = None,
    image_urls: Sequence[str] = (),
    files: Sequence[discord.File] = (),
    media_urls: Optional[Sequence[str]] = None,
) -> list[SocialPreviewView]:
    """Split text without truncation and attach media to the main card only."""
    header = "\n".join(
        part for part in (
            f"**{discord.utils.escape_markdown(author)}**" if author else "",
            f"[{_escape_link_label(title)}](<{original_url}>)" if title else "",
        ) if part
    )
    body_budget = MAX_TEXT_UNITS - _text_units(header)
    if body_budget < 2:
        raise ValueError("Preview author and title exceed the component text budget")

    if media_urls is None:
        media_urls = list(image_urls) + [f"attachment://{file.filename}" for file in files]
        if video_url:
            media_urls.append(video_url)
    else:
        media_urls = list(media_urls)
    if len(media_urls) > 10:
        raise ValueError("A preview gallery can contain at most 10 media items")

    views = []
    # Put overflowing commentary before the main card, preserving its order.
    while _text_units(commentary) > body_budget - 2:
        part, commentary = _take_text(commentary, MAX_TEXT_UNITS)
        views.append(SocialPreviewView(original_url=original_url, commentary=part, timeout=timeout))

    part, rest = _take_text(text, body_budget - _text_units(commentary))
    views.append(SocialPreviewView(
        original_url=original_url, video_url=video_url, header=header, text=part,
        commentary=commentary, spoiler=spoiler, timeout=timeout, media_urls=media_urls, files=files,
    ))
    while rest:
        part, rest = _take_text(rest, body_budget)
        views.append(SocialPreviewView(
            original_url=original_url, header=header, text=part, spoiler=spoiler, timeout=timeout,
        ))
    return views
