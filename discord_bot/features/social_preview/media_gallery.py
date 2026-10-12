"""Ordered image/video galleries with bounded image downloads."""
from __future__ import annotations

from dataclasses import dataclass, field
import io
from urllib.parse import parse_qs, unquote, urlparse

import discord

from .download import MAX_IMAGE_DOWNLOAD_BYTES, is_trusted_media_url

MAX_PREVIEW_MEDIA = 4
MAX_GALLERY_BYTES = 20 * 1024 * 1024


@dataclass
class PreparedGallery:
    media_urls: list[str] = field(default_factory=list)
    files: list[discord.File] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    video_url: str | None = None
    media_spoilers: list[bool] = field(default_factory=list)


def select_media(items, limit=MAX_PREVIEW_MEDIA):
    selected, seen = [], set()
    for item in items:
        if item.type not in {"image", "video"} or not isinstance(item.url, str) or len(item.url) > 8192:
            continue
        if not is_trusted_media_url(item.url):
            continue
        if item.type == "video":
            parsed = urlparse(item.url)
            try:
                keys = {key.lower() for key in parse_qs(parsed.query, max_num_fields=100)}
            except ValueError:
                continue
            if not unquote(parsed.path).lower().endswith((".mp4", ".m4v", ".webm", ".mov")) or keys & {"bytestart", "byteend", "range"}:
                continue
        if item.url in seen:
            continue
        selected.append(item)
        seen.add(item.url)
        if len(selected) >= max(1, min(limit, MAX_PREVIEW_MEDIA)):
            break
    return selected


async def prepare_gallery(session, items, *, downloader, prefix, spoiler=False, reupload_images=True, limit=MAX_PREVIEW_MEDIA):
    """Freeze the first four slots, upload images only, preserve all remaining order."""
    result = PreparedGallery()
    remaining = MAX_GALLERY_BYTES
    try:
        for index, item in enumerate(select_media(items, limit), start=1):
            item_spoiler = bool(spoiler or getattr(item, "spoiler", False))
            if item.type == "video":
                result.media_urls.append(item.url)
                result.media_spoilers.append(item_spoiler)
                result.video_url = result.video_url or item.url
                continue
            if not reupload_images:
                result.media_urls.append(item.url)
                result.media_spoilers.append(item_spoiler)
                result.image_urls.append(item.url)
                continue
            if remaining <= 0:
                continue
            budget = min(MAX_IMAGE_DOWNLOAD_BYTES, remaining)
            data = await downloader(session, item.url, max_bytes=budget)
            if not data or len(data) > budget:
                continue
            remaining -= len(data)
            file = discord.File(io.BytesIO(data), filename=f"{prefix}_{index}.jpg", spoiler=item_spoiler)
            result.files.append(file)
            result.media_urls.append(f"attachment://{file.filename}")
            result.media_spoilers.append(item_spoiler)
        return result
    except BaseException:
        for file in result.files:
            file.close()
            file.fp.close()
        raise
