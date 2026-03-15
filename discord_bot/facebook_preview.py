from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.errors import HTTPException, InteractionResponded, NotFound
from .preview_sender import cleanup_source_message, send_preview_as_author

logger = logging.getLogger("discord_digest_bot")


def _delete_view_timeout_from_env() -> Optional[float]:
    # >0: 秒數；<=0 或 none/off: 無逾時（同一個 bot 進程內不會自動消失）
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

FACEBOOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|mbasic\.)?(?:facebook\.com|fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

MAX_VIDEO_UPLOAD_BYTES = 20 * 1024 * 1024


@dataclass
class FacebookPreview:
    embed: discord.Embed
    files: List[discord.File]
    extra_text: Optional[str] = None


class DeleteFacebookPreviewView(discord.ui.View):
    def __init__(self, *, original_url: str, timeout: Optional[float] = DELETE_VIEW_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.related_message_ids: List[int] = []
        self.add_item(discord.ui.Button(label="原連結", style=discord.ButtonStyle.link, url=original_url))

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


def extract_facebook_urls(content: str) -> List[str]:
    if not content:
        return []

    matches = FACEBOOK_URL_RE.findall(content)
    seen = set()
    output = []
    for url in matches:
        # 連結包在 ||spoiler|| 時，regex 可能把結尾 || 一起吃進來
        clean_url = url.rstrip(").,>|")
        parsed = urlparse(clean_url)
        if parsed.netloc.lower().startswith("l.facebook.com"):
            continue
        if clean_url not in seen:
            seen.add(clean_url)
            output.append(clean_url)
    return output


def _is_facebook_url_spoilered(content: str, url: str) -> bool:
    if not content:
        return False
    for block in SPOILER_BLOCK_RE.findall(content):
        if url in extract_facebook_urls(block):
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
                filename = filename[len("SPOILER_"):]
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


def _build_candidate_urls(url: str) -> List[str]:
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")

    hosts = ["www.facebook.com", "m.facebook.com", "mbasic.facebook.com"]
    if parsed.netloc.lower() == "fb.watch":
        return [url]

    candidates = [url]
    for host in hosts:
        candidates.append(urlunparse((parsed.scheme, host, parsed.path, "", parsed.query, "")))

    seen = set()
    uniq = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            uniq.append(candidate)
    return uniq


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout_sec: int = 20) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=timeout_sec) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None


def _meta_contents(soup: BeautifulSoup, key: str) -> List[str]:
    values: List[str] = []
    for tag in soup.find_all("meta", attrs={"property": key}):
        content = tag.get("content")
        if content and content.strip():
            values.append(content.strip())
    for tag in soup.find_all("meta", attrs={"name": key}):
        content = tag.get("content")
        if content and content.strip():
            values.append(content.strip())

    seen = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def _extract_og_data(html: str) -> Tuple[str, str, List[str], List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = (_meta_contents(soup, "og:title") or ["Facebook 貼文"])[0]
    description = (_meta_contents(soup, "og:description") or [""])[0]
    image_urls = _meta_contents(soup, "og:image")

    video_urls = []
    for key in ("og:video", "og:video:url", "og:video:secure_url"):
        video_urls.extend(_meta_contents(soup, key))
    # fallback: 有些頁面只放 twitter player
    video_urls.extend(_meta_contents(soup, "twitter:player:stream"))

    seen_video = set()
    unique_video_urls = []
    for url in video_urls:
        if url not in seen_video:
            seen_video.add(url)
            unique_video_urls.append(url)

    return title, description, image_urls, unique_video_urls


async def _fetch_html_with_aiohttp(url: str) -> Tuple[str, str, List[str], List[str], Optional[str], int]:
    last_error = "Facebook 頁面讀取失敗"
    last_status = 0

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        for candidate in _build_candidate_urls(url):
            try:
                async with session.get(candidate, timeout=25, allow_redirects=True) as resp:
                    last_status = resp.status
                    html = await resp.text(errors="ignore")
                    title, description, image_urls, video_urls = _extract_og_data(html)

                    if title or description or image_urls or video_urls:
                        return title, description, image_urls, video_urls, str(resp.url), resp.status

                    if resp.status < 400:
                        return title, description, image_urls, video_urls, str(resp.url), resp.status

                    last_error = f"Facebook 頁面讀取失敗（HTTP {resp.status}）"
            except Exception as exc:
                last_error = f"Facebook 頁面讀取失敗（{exc}）"

    raise RuntimeError(last_error if last_status else "Facebook 頁面讀取失敗（無有效回應）")


def _fetch_og_with_playwright_sync(url: str) -> Tuple[str, str, List[str], List[str], str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"], locale="zh-TW")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)

        html = page.content()
        title, description, image_urls, video_urls = _extract_og_data(html)
        final_url = page.url

        context.close()
        browser.close()
        return title, description, image_urls, video_urls, final_url


async def _fetch_og_data(url: str) -> Tuple[str, str, List[str], List[str], str]:
    try:
        title, description, image_urls, video_urls, final_url, status = await _fetch_html_with_aiohttp(url)
        if status >= 400 and not (title or description or image_urls or video_urls):
            raise RuntimeError(f"HTTP {status} 且無可用 Open Graph")
        return title, description, image_urls, video_urls, final_url or url
    except Exception as http_error:
        logger.warning("aiohttp 抓 Facebook Open Graph 失敗，改用 Playwright: %s", http_error)

    try:
        title, description, image_urls, video_urls, final_url = await asyncio.to_thread(_fetch_og_with_playwright_sync, url)
        return title, description, image_urls, video_urls, final_url or url
    except Exception as playwright_error:
        logger.warning("Playwright 擷取 Facebook Open Graph 失敗: %s", playwright_error)
        return "Facebook 貼文", "", [], [], url


async def build_facebook_preview(
    url: str,
    *,
    reupload_image: bool = True,
    max_images: int = 4,
    allow_video_upload: bool = True,
    spoiler: bool = False,
) -> FacebookPreview:
    title, description, image_urls, video_urls, final_url = await _fetch_og_data(url)

    embed = discord.Embed(
        title=title[:256],
        description=description[:4096],
        url=final_url,
    )
    embed.set_author(name="Facebook")
    if spoiler and embed.description:
        embed.description = _spoiler_wrap(embed.description)

    files: List[discord.File] = []
    extra_lines: List[str] = []

    selected_image_urls = image_urls[:max(1, max_images)]
    if selected_image_urls:
        if reupload_image:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
                for index, image_url in enumerate(selected_image_urls, start=1):
                    image_data = await _download_bytes(session, image_url)
                    if not image_data:
                        continue
                    file_name = f"facebook_preview_{index}.jpg"
                    files.append(discord.File(io.BytesIO(image_data), filename=file_name, spoiler=spoiler))

            if files:
                embed.set_image(url=f"attachment://{files[0].filename}")
            else:
                if not spoiler:
                    embed.set_image(url=selected_image_urls[0])
        else:
            if not spoiler:
                embed.set_image(url=selected_image_urls[0])

    if video_urls:
        mp4_urls = [u for u in video_urls if u.lower().endswith(".mp4")]
        if allow_video_upload and mp4_urls:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
                video_data = await _download_bytes(session, mp4_urls[0], timeout_sec=40)
            if video_data and len(video_data) <= MAX_VIDEO_UPLOAD_BYTES:
                files.append(discord.File(io.BytesIO(video_data), filename="facebook_preview_video.mp4", spoiler=spoiler))
            else:
                extra_lines.append(f"影片連結：{mp4_urls[0]}")
        else:
            extra_lines.append(f"影片連結：{video_urls[0]}")

    extra_text = "\n".join(extra_lines) if extra_lines else None
    if spoiler and extra_text:
        extra_text = _spoiler_wrap(extra_text)
    return FacebookPreview(embed=embed, files=files, extra_text=extra_text)


async def handle_facebook_in_message(message: discord.Message) -> bool:
    if message.author.bot:
        return False

    urls = extract_facebook_urls(message.content or "")
    if not urls:
        return False

    url = urls[0]
    try:
        use_spoiler = _is_facebook_url_spoilered(message.content or "", url)
        logger.info(
            "%s 在 %s 貼了 Facebook url %s",
            message.author.nick or message.author.global_name,
            message.channel.name,
            url,
        )

        if use_spoiler:
            preview = await build_facebook_preview(
                url,
                reupload_image=True,
                max_images=4,
                allow_video_upload=True,
                spoiler=False,
            )

            if preview.embed.description:
                preview.embed.description = _spoiler_wrap(preview.embed.description)
            else:
                fallback_text = preview.embed.title or "Facebook 貼文"
                preview.embed.description = _spoiler_wrap(fallback_text)
            preview.embed.set_image(url=None)
            spoiler_content = _spoiler_wrap(preview.extra_text) if preview.extra_text else None

            view = DeleteFacebookPreviewView(original_url=url)
            sent = await send_preview_as_author(
                message,
                content=spoiler_content,
                embed=preview.embed,
                view=view,
            )
            view.message = sent

            if preview.files:
                spoiler_files = _clone_files_as_spoiler(preview.files)
                attachment_message = await send_preview_as_author(message, files=spoiler_files)
                view.related_message_ids.append(attachment_message.id)

            await cleanup_source_message(message, platform="Facebook", url=url)
            return True

        preview = await build_facebook_preview(
            url,
            reupload_image=True,
            max_images=4,
            allow_video_upload=True,
            spoiler=False,
        )
        view = DeleteFacebookPreviewView(original_url=url)
        reply_kwargs = {
            "embed": preview.embed,
            "mention_author": False,
            "view": view,
        }
        if preview.files:
            reply_kwargs["files"] = preview.files
        if preview.extra_text:
            reply_kwargs["content"] = preview.extra_text

        sent = await send_preview_as_author(
            message,
            content=reply_kwargs.get("content"),
            embed=reply_kwargs.get("embed"),
            files=reply_kwargs.get("files"),
            view=reply_kwargs.get("view"),
        )
        view.message = sent

        await cleanup_source_message(message, platform="Facebook", url=url)

        return True
    except Exception as exc:
        logger.error("Facebook 預覽失敗: %s", exc, exc_info=True)
        await message.reply(f"Facebook 預覽失敗：{exc}", mention_author=False)
        return False
