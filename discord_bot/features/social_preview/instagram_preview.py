"""Instagram first-party media rendered with the shared Components V2 cards."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Optional

import aiohttp
import discord

from .download import MAX_IMAGE_DOWNLOAD_BYTES, download_bytes_limited
from .media_gallery import prepare_gallery, select_media
from .instagram_fetch import fetch_instagram_post, instagram_shortcode, normalize_instagram_url
from .instagram_logging import elapsed_ms, log_event, new_trace_id
from .sender import cleanup_source_message, log_preview_link, send_preview_group
from .duplicate_patrol import send_registered_preview
from .text import extract_message_commentary
from .failure_notifications import (
    PLATFORM_DIAGNOSTICS, failure_reason, notify_preview_failure, public_failure_message, reply_preview_failure,
)

logger = logging.getLogger("discord_digest_bot")
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reels?|tv)/[A-Za-z0-9_\-]+/?"
    r"(?:\?[^\s<>()|]+)?(?:#[^\s<>()|]+)?", re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
ERROR_MESSAGES = PLATFORM_DIAGNOSTICS["Instagram"]
INSTAGRAM_INVALID_POST_MESSAGE = public_failure_message("Instagram")


def _delete_view_timeout_from_env():
    try:
        seconds = float(os.environ.get("SOCIAL_PREVIEW_DELETE_TIMEOUT_SECONDS", "0"))
        return seconds if seconds > 0 else None
    except ValueError:
        return None


DELETE_VIEW_TIMEOUT = _delete_view_timeout_from_env()


@dataclass
class InstagramPreview:
    # Internal metadata only; no Embed is sent to Discord.
    embed: discord.Embed
    files: list[discord.File] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    video_url: Optional[str] = None
    media_urls: Optional[list[str]] = None
    trace_id: Optional[str] = None
    fetch_source: str = "unknown"
    outcome: str = "content"
    preview_error: Optional[str] = None


def _sanitize_instagram_url(url):
    return normalize_instagram_url(url)


def extract_instagram_urls(content):
    urls = []
    for raw in INSTAGRAM_URL_RE.findall(content or ""):
        url = normalize_instagram_url(raw)
        if url not in urls:
            urls.append(url)
    return urls


def _is_instagram_url_spoilered(content, url):
    return any(url in extract_instagram_urls(block) for block in SPOILER_BLOCK_RE.findall(content or ""))


def _extract_instagram_commentary(content, url):
    return extract_message_commentary(content, target_url=url, url_pattern=INSTAGRAM_URL_RE,
                                      sanitize_url=normalize_instagram_url)


async def _download_bytes(session, url, *, max_bytes=MAX_IMAGE_DOWNLOAD_BYTES):
    return await download_bytes_limited(session, url, max_bytes=max_bytes, timeout_sec=20, media_type="image")


async def build_instagram_preview(url, *, reupload_image=True, spoiler=False, trace_id=None):
    started = perf_counter()
    trace_id = trace_id or new_trace_id()
    post = await asyncio.to_thread(fetch_instagram_post, url, trace_id=trace_id)
    selected = select_media(post.media)
    error = failure_reason("Instagram", post.preview_error)
    if not error and not (post.text or selected):
        error = "no_content"
    title = f"Instagram @{post.author_username}" if post.author_username else "Instagram"
    description = INSTAGRAM_INVALID_POST_MESSAGE if error else post.text or ""
    embed = discord.Embed(title=title, description=description, url=post.url)
    embed.set_author(name=post.author_name or (f"@{post.author_username}" if post.author_username else "Instagram"))
    preview = InstagramPreview(embed, trace_id=post.trace_id or trace_id, fetch_source=post.fetch_source)

    def finish():
        images = len(preview.files) + len(preview.image_urls)
        rendered = len(preview.media_urls or [])
        preview.outcome = "error_card" if error else "content"
        preview.preview_error = error
        log_event("preview_prepared", trace_id=preview.trace_id, shortcode=post.shortcode,
                  source=preview.fetch_source, result=error or "success", outcome=preview.outcome,
                  selected_count=len(selected), rendered_count=rendered, dropped_count=len(selected) - rendered,
                  image_count=images, video_count=max(0, rendered - images), has_text=bool(post.text),
                  spoiler=spoiler, elapsed_ms=elapsed_ms(started))
        return preview

    if error:
        return finish()
    async with aiohttp.ClientSession() as session:
        gallery = await prepare_gallery(session, selected, downloader=_download_bytes,
                                        prefix="instagram_image", spoiler=spoiler, reupload_images=reupload_image)
    preview.files, preview.image_urls = gallery.files, gallery.image_urls
    preview.video_url, preview.media_urls = gallery.video_url, gallery.media_urls
    if not gallery.media_urls and not post.text:
        error = "fetch_failed"
        embed.description = INSTAGRAM_INVALID_POST_MESSAGE
    return finish()


async def handle_instagram_in_message(message, *, bot_client=None):
    if message.author.bot:
        return False
    urls = extract_instagram_urls(message.content or "")
    if not urls:
        return False
    url = urls[0]
    phase_started, phase, preview = perf_counter(), "prepare", None
    trace_id = new_trace_id()
    spoiler = False
    try:
        log_preview_link(message, platform="Instagram", url=url)
        from .preview_view import build_social_preview_views

        spoiler = _is_instagram_url_spoilered(message.content or "", url)
        preview = await build_instagram_preview(url, spoiler=spoiler, trace_id=trace_id)
        await notify_preview_failure(message, platform="Instagram", url=url, reason=preview.preview_error,
                                     bot_client=bot_client, trace_id=preview.trace_id)
        views = build_social_preview_views(
            original_url=url, title=preview.embed.title or "Instagram", author=preview.embed.author.name or "Instagram",
            text=preview.embed.description or "", commentary=_extract_instagram_commentary(message.content or "", url),
            spoiler=spoiler, timeout=DELETE_VIEW_TIMEOUT, video_url=preview.video_url,
            image_urls=preview.image_urls, files=preview.files, media_urls=preview.media_urls,
        )
        phase, phase_started = "send", perf_counter()
        await send_registered_preview(message, views, send=send_preview_group, platform="instagram",
                                      url=preview.embed.url or url, preview_error=preview.preview_error)
        log_event("preview_delivery", trace_id=preview.trace_id, shortcode=instagram_shortcode(url),
                  source=preview.fetch_source, result="sent", outcome=preview.outcome, phase=phase,
                  page_count=len(views), elapsed_ms=elapsed_ms(phase_started))
        phase, phase_started = "cleanup", perf_counter()
        await cleanup_source_message(message, platform="Instagram", url=url)
        return True
    except Exception as exc:
        log_event("preview_delivery", trace_id=preview.trace_id if preview else trace_id,
                  shortcode=instagram_shortcode(url), source=preview.fetch_source if preview else "unknown",
                  result="failed", phase=phase, error_type=type(exc).__name__,
                  http_status=getattr(exc, "status", None), error_code=getattr(exc, "code", None),
                  elapsed_ms=elapsed_ms(phase_started))
        await notify_preview_failure(message, platform="Instagram", url=url, reason=f"{phase}_failed",
                                     bot_client=bot_client, exception=exc,
                                     trace_id=preview.trace_id if preview else trace_id)
        if phase == "cleanup":
            return True
        await reply_preview_failure(message, platform="Instagram", spoiler=spoiler)
        return False
