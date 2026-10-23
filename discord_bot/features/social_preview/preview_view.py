"""Shared Components V2 cards for Threads, Facebook and Instagram previews."""
from __future__ import annotations

from typing import Optional, Sequence

import discord
from discord.errors import HTTPException, NotFound

from .sender import delete_preview_messages
from .text import TextSpan, coalesce_text_spans


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


def _take_escaped_text(text: str, budget: int) -> tuple[str, str]:
    """Split prepared text without breaking an escape before the closing mask."""
    part, rest = _take_text(text, budget)
    trailing_slashes = len(part) - len(part.rstrip("\\"))
    if trailing_slashes % 2:
        part, rest = part[:-1], part[-1:] + rest
    return part, rest


def _take_body(text: str, budget: int, spoiler: bool) -> tuple[str, str]:
    if not spoiler:
        return _take_text(text, budget)
    part, rest = _take_escaped_text(text, budget)
    return (f"||{part}||" if part else ""), rest


def _span_body_pages(spans: Sequence[TextSpan], first_budget: int, page_budget: int) -> list[str]:
    """Close native masks on every page, counting escapes and mask delimiters."""
    pages, parts = [], []
    remaining = first_budget
    for span in coalesce_text_spans(spans):
        text = discord.utils.escape_markdown(span.text, ignore_links=False)
        while text:
            capacity = remaining - (4 if span.spoiler else 0)
            if capacity < 2:
                pages.append("".join(parts))
                parts, remaining = [], page_budget
                continue
            part, text = _take_escaped_text(text, capacity)
            rendered = f"||{part}||" if span.spoiler else part
            parts.append(rendered)
            remaining -= _text_units(rendered)
    if parts or not pages:
        pages.append("".join(parts))
    return pages


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
        media_spoilers: Optional[Sequence[bool]] = None,
        files: Sequence[discord.File] = (),
        spoiler: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.files = list(files)
        self.message: Optional[discord.Message] = None
        self.related_message_ids: list[int] = []
        self.delivery_complete = None
        self.on_deleted = None
        if media_spoilers is not None and len(media_spoilers) != len(media_urls):
            raise ValueError("Media spoiler flags must match the gallery items")

        if commentary:
            self.add_item(discord.ui.TextDisplay(commentary))

        card = None
        if header or text or media_urls:
            card = discord.ui.Container()
            if header:
                card.add_item(discord.ui.TextDisplay(header))
            if text:
                card.add_item(discord.ui.TextDisplay(text))
            if media_urls:
                card.add_item(discord.ui.MediaGallery(*(
                    discord.MediaGalleryItem(media=url, spoiler=spoiler or bool(media_spoilers and media_spoilers[index]))
                    for index, url in enumerate(media_urls)
                )))
            self.add_item(card)

        controls = discord.ui.ActionRow()
        delete_button = discord.ui.Button(label="", style=discord.ButtonStyle.gray, emoji="🗑️")
        delete_button.callback = self._delete
        controls.add_item(delete_button)
        controls.add_item(discord.ui.Button(label="原連結", style=discord.ButtonStyle.link, url=original_url))
        if video_url and len(video_url) <= 512:
            controls.add_item(discord.ui.Button(label="影片直連", style=discord.ButtonStyle.link, url=video_url))

        # Only the body and media are masked; the header and controls stay visible.
        if card is not None:
            card.add_item(controls)
        else:
            self.add_item(controls)

    async def _delete(self, interaction: discord.Interaction) -> None:
        if self.delivery_complete is not None and not self.delivery_complete.is_set():
            await interaction.response.defer(ephemeral=True)
            await self.delivery_complete.wait()
        await delete_preview_messages(interaction, self.related_message_ids, on_deleted=self.on_deleted)

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
    media_spoilers: Optional[Sequence[bool]] = None,
    text_spans: Optional[Sequence[TextSpan]] = None,
) -> list[SocialPreviewView]:
    """Split text without truncation and attach media to the main card only."""
    header = "\n".join(
        part for part in (
            f"**{discord.utils.escape_markdown(author)}**" if author else "",
            f"[{_escape_link_label(title)}](<{original_url}>)" if title else "",
        ) if part
    )
    # Force one body mask, regardless of existing spoiler/code Markdown. Escape
    # before splitting so each page can independently close its spoiler safely.
    if text_spans is not None:
        text = "".join(span.text for span in text_spans)
    if spoiler:
        text = discord.utils.escape_markdown(text, ignore_links=False)
    body_budget = MAX_TEXT_UNITS - _text_units(header) - (4 if spoiler else 0)
    native_spans = text_spans if text_spans is not None and not spoiler else None
    minimum_body = 6 if native_spans is not None else 2
    if body_budget < minimum_body:
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
    while _text_units(commentary) > body_budget - minimum_body:
        part, commentary = _take_text(commentary, MAX_TEXT_UNITS)
        views.append(SocialPreviewView(original_url=original_url, commentary=part, timeout=timeout))

    first_budget = body_budget - _text_units(commentary)
    if native_spans is not None:
        bodies = _span_body_pages(native_spans, first_budget, body_budget)
    else:
        part, rest = _take_body(text, first_budget, spoiler)
        bodies = [part]
        while rest:
            part, rest = _take_body(rest, body_budget, spoiler)
            bodies.append(part)
    views.append(SocialPreviewView(
        original_url=original_url, video_url=video_url, header=header, text=bodies[0],
        commentary=commentary, spoiler=spoiler, timeout=timeout, media_urls=media_urls, files=files, media_spoilers=media_spoilers,
    ))
    for part in bodies[1:]:
        views.append(SocialPreviewView(
            original_url=original_url, header=header, text=part, spoiler=spoiler, timeout=timeout,
        ))
    return views
