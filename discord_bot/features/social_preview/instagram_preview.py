from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
import discord
from discord.errors import HTTPException, InteractionResponded, NotFound

from .instagram_fetch import InstagramPost, fetch_instagram_post, normalize_instagram_url
from .sender import cleanup_source_message, send_preview_as_author
from .text import extract_message_commentary

logger = logging.getLogger("discord_digest_bot")

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reels?|tv)/[A-Za-z0-9_\-]+/?"
    r"(?:\?[^\s<>()|]+)?(?:#[^\s<>()|]+)?",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
MAX_VIDEO_UPLOAD_BYTES = 20 * 1024 * 1024


@dataclass
class InstagramPreview:
    embed: Optional[discord.Embed]
    files: List[discord.File]
    extra_text: Optional[str] = None
    video_url: Optional[str] = None
    inline_video_url: Optional[str] = None
    native_embed_url: Optional[str] = None


def _delete_view_timeout_from_env() -> Optional[float]:
    raw = os.getenv("SOCIAL_PREVIEW_DELETE_TIMEOUT_SECONDS", "0").strip().lower()
    if raw in {"none", "off", "disable", "disabled"}:
        return None
    try:
        timeout = float(raw)
    except ValueError:
        return None
    if timeout <= 0:
        return None
    return timeout


DELETE_VIEW_TIMEOUT = _delete_view_timeout_from_env()


class DeleteInstagramPreviewView(discord.ui.View):
    def __init__(
        self,
        *,
        original_url: str,
        video_url: Optional[str] = None,
        timeout: Optional[float] = DELETE_VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.related_message_ids: List[int] = []
        self.add_item(discord.ui.Button(label="原連結", style=discord.ButtonStyle.link, url=original_url))
        if video_url:
            self.add_item(discord.ui.Button(label="影片直連", style=discord.ButtonStyle.link, url=video_url))

    @discord.ui.button(label="", style=discord.ButtonStyle.gray, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # pragma: no cover
        try:
            await interaction.response.send_message("預覽已刪除。", ephemeral=True)
        except InteractionResponded:
            pass

        try:
            await interaction.message.delete()
        except (NotFound, HTTPException):
            return

        channel = getattr(interaction.message, "channel", None)
        if channel and self.related_message_ids:
            for message_id in list(self.related_message_ids):
                try:
                    related = await channel.fetch_message(message_id)
                    await related.delete()
                except (NotFound, HTTPException, AttributeError):
                    continue

    async def on_timeout(self) -> None:  # pragma: no cover
        if self.message:
            try:
                await self.message.edit(view=None)
            except (NotFound, HTTPException):
                pass


def _sanitize_instagram_url(url: str) -> str:
    clean = (url or "").rstrip(").,>|")
    parsed = urlparse(clean)
    host = parsed.netloc.lower()
    if host == "instagram.com":
        host = "www.instagram.com"
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", host, path, "", "", ""))


def extract_instagram_urls(content: str) -> List[str]:
    if not content:
        return []

    seen: set[str] = set()
    output: List[str] = []
    for raw_url in INSTAGRAM_URL_RE.findall(content):
        try:
            clean_url = normalize_instagram_url(_sanitize_instagram_url(raw_url))
        except ValueError:
            continue
        if clean_url in seen:
            continue
        seen.add(clean_url)
        output.append(clean_url)
    return output


def _is_instagram_url_spoilered(content: str, url: str) -> bool:
    if not content:
        return False
    for block in SPOILER_BLOCK_RE.findall(content):
        if url in extract_instagram_urls(block):
            return True
    return False


def _spoiler_wrap(text: str) -> str:
    if not text:
        return text
    return f"||{text}||"


def _clone_files_as_spoiler(files: List[discord.File]) -> List[discord.File]:
    cloned: List[discord.File] = []
    for file in files:
        try:
            file.fp.seek(0)
            data = file.fp.read()
            file.fp.seek(0)
            filename = file.filename
            if filename.startswith("SPOILER_"):
                filename = filename[len("SPOILER_") :]
            cloned.append(
                discord.File(
                    io.BytesIO(data),
                    filename=filename,
                    spoiler=True,
                    description=file.description,
                )
            )
        except Exception:
            cloned.append(file)
    return cloned


def _extract_instagram_commentary(content: str, url: str) -> str:
    return extract_message_commentary(
        content,
        target_url=url,
        url_pattern=INSTAGRAM_URL_RE,
        sanitize_url=_sanitize_instagram_url,
    )


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout_sec: int = 20) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=timeout_sec) as response:
            if response.status == 200:
                return await response.read()
    except Exception:
        return None
    return None


def _format_link(label: str, url: str) -> str:
    return f"[{label}]({url})"


def _format_native_embed_link(url: str) -> str:
    if "/reels/" in urlparse(url).path.lower():
        return f"[.]({url})"
    return url


async def build_instagram_preview(
    url: str,
    *,
    reupload_image: bool = True,
    allow_video_upload: bool = False,
    spoiler: bool = False,
) -> InstagramPreview:
    post: InstagramPost = await asyncio.to_thread(fetch_instagram_post, url)
    if post.native_embed_url:
        return InstagramPreview(
            embed=None,
            files=[],
            native_embed_url=post.native_embed_url,
        )
    if not _has_meaningful_preview(post):
        raise RuntimeError("Instagram did not return public preview metadata")

    username = post.author_username or ""
    title = f"Instagram @{username}".strip() if username else "Instagram"
    description = (_preview_description(post))[:4096]
    embed = discord.Embed(title=title[:256], description=description, url=post.url)

    author_display = post.author_name or (f"@{post.author_username}" if post.author_username else "Instagram")
    embed.set_author(name=author_display)
    if spoiler:
        embed.description = _spoiler_wrap(description)

    image_urls = [media.url for media in post.media if media.type == "image"]
    files: List[discord.File] = []

    if image_urls:
        selected_image_url = image_urls[0]
        if reupload_image:
            async with aiohttp.ClientSession() as session:
                for image_url in image_urls:
                    image_data = await _download_bytes(session, image_url)
                    if image_data:
                        selected_image_url = image_url
                        file = discord.File(io.BytesIO(image_data), filename="instagram_image.jpg", spoiler=spoiler)
                        files.append(file)
                        embed.set_image(url=f"attachment://{file.filename}")
                        break
                    logger.info("Instagram image candidate unavailable: %s", image_url)
            if not files and not spoiler:
                embed.set_image(url=selected_image_url)
        elif not spoiler:
            embed.set_image(url=selected_image_url)

    video_urls = [media.url for media in post.media if media.type == "video"]
    primary_video_url: Optional[str] = None
    inline_video_url: Optional[str] = None
    extra_lines: list[str] = []

    for index, extra_image_url in enumerate(image_urls[1:], start=2):
        extra_lines.append(_format_link(f"圖片 {index}", extra_image_url))

    if video_urls:
        mp4_urls = [value for value in video_urls if urlparse(value).path.lower().endswith(".mp4")]
        primary_video_url = mp4_urls[0] if mp4_urls else video_urls[0]

        if allow_video_upload and mp4_urls:
            async with aiohttp.ClientSession() as session:
                video_data = await _download_bytes(session, mp4_urls[0], timeout_sec=40)
            if video_data and len(video_data) <= MAX_VIDEO_UPLOAD_BYTES:
                files.append(discord.File(io.BytesIO(video_data), filename="instagram_video.mp4", spoiler=spoiler))
                inline_video_url = None
            else:
                inline_video_url = primary_video_url
        else:
            inline_video_url = primary_video_url

        for index, extra_url in enumerate(video_urls[1:], start=2):
            extra_lines.append(_format_link(f"影片 {index}", extra_url))

    extra_text = "\n".join(extra_lines) if extra_lines else None
    if spoiler and extra_text:
        extra_text = _spoiler_wrap(extra_text)

    return InstagramPreview(
        embed=embed,
        files=files,
        extra_text=extra_text,
        video_url=primary_video_url,
        inline_video_url=inline_video_url,
    )


def _has_meaningful_preview(post: InstagramPost) -> bool:
    return bool(post.text or post.media or post.author_username)


def _preview_description(post: InstagramPost) -> str:
    if post.text:
        return post.text
    if post.preview_level == "media_fallback":
        return "Instagram 圖片預覽（未取得貼文文字）"
    return "Instagram preview"


async def handle_instagram_in_message(message: discord.Message) -> bool:
    if message.author.bot:
        return False

    urls = extract_instagram_urls(message.content or "")
    if not urls:
        return False

    url = urls[0]
    try:
        author_name = getattr(message.author, "nick", None) or getattr(message.author, "global_name", None)
        channel_name = getattr(message.channel, "name", None)
        logger.info("%s posted Instagram url %s in %s", author_name, url, channel_name)

        use_spoiler = _is_instagram_url_spoilered(message.content or "", url)
        user_commentary = _extract_instagram_commentary(message.content or "", url)

        if use_spoiler:
            content_lines = []
            if user_commentary:
                content_lines.append(user_commentary)
            content_lines.append(_spoiler_wrap(url))
            view = DeleteInstagramPreviewView(original_url=url)
            sent = await send_preview_as_author(
                message,
                content="\n".join(content_lines),
                view=view,
            )
            view.message = sent
            await cleanup_source_message(message, platform="Instagram", url=url)
            return True

        preview = await build_instagram_preview(
            url,
            reupload_image=True,
            allow_video_upload=False,
            spoiler=False,
        )
        content_lines = []
        if user_commentary:
            content_lines.append(user_commentary)
        if preview.native_embed_url:
            content_lines.append(_format_native_embed_link(preview.native_embed_url))
        if preview.inline_video_url:
            content_lines.append(preview.inline_video_url)
        if preview.extra_text:
            content_lines.append(preview.extra_text)

        view = DeleteInstagramPreviewView(original_url=url, video_url=preview.video_url)
        sent = await send_preview_as_author(
            message,
            content="\n".join(content_lines) if content_lines else None,
            embed=preview.embed,
            files=preview.files,
            view=view,
        )
        view.message = sent

        await cleanup_source_message(message, platform="Instagram", url=url)
        return True
    except Exception as exc:
        logger.error("Instagram preview failed: %s", exc, exc_info=True)
        if hasattr(message, "reply"):
            await message.reply(
                "Instagram preview failed: this content may require login or may not be public.",
                mention_author=False,
            )
        return False
