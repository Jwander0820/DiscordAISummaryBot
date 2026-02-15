from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup

logger = logging.getLogger("discord_digest_bot")

FACEBOOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:facebook\.com|fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class FacebookPreview:
    embed: discord.Embed
    files: List[discord.File]


def extract_facebook_urls(content: str) -> List[str]:
    if not content:
        return []

    matches = FACEBOOK_URL_RE.findall(content)
    seen = set()
    output = []
    for url in matches:
        clean_url = url.rstrip(").,>")
        parsed = urlparse(clean_url)
        if parsed.netloc.lower().startswith("l.facebook.com"):
            continue
        if clean_url not in seen:
            seen.add(clean_url)
            output.append(clean_url)
    return output


async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout_sec: int = 20) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=timeout_sec) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None


def _meta_content(soup: BeautifulSoup, key: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"property": key})
    if tag and tag.get("content"):
        return tag["content"].strip()
    tag = soup.find("meta", attrs={"name": key})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


async def build_facebook_preview(url: str, *, reupload_image: bool = True) -> FacebookPreview:
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        async with session.get(url, timeout=25, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Facebook 頁面讀取失敗（HTTP {resp.status}）")
            final_url = str(resp.url)
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        title = _meta_content(soup, "og:title") or "Facebook 貼文"
        description = _meta_content(soup, "og:description") or ""
        image_url = _meta_content(soup, "og:image")

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            url=final_url,
        )
        embed.set_author(name="Facebook")

        files: List[discord.File] = []
        if image_url:
            if reupload_image:
                image_data = await _download_bytes(session, image_url)
                if image_data:
                    file_name = "facebook_preview.jpg"
                    files.append(discord.File(io.BytesIO(image_data), filename=file_name))
                    embed.set_image(url=f"attachment://{file_name}")
                else:
                    embed.set_image(url=image_url)
            else:
                embed.set_image(url=image_url)

    return FacebookPreview(embed=embed, files=files)


async def handle_facebook_in_message(message: discord.Message) -> bool:
    if message.author.bot:
        return False

    urls = extract_facebook_urls(message.content or "")
    if not urls:
        return False

    url = urls[0]
    try:
        logger.info(
            "%s 在 %s 貼了 Facebook url %s",
            message.author.nick or message.author.global_name,
            message.channel.name,
            url,
        )
        preview = await build_facebook_preview(url, reupload_image=True)
        reply_kwargs = {
            "embed": preview.embed,
            "mention_author": False,
        }
        if preview.files:
            reply_kwargs["files"] = preview.files

        await message.reply(**reply_kwargs)
        return True
    except Exception as e:
        logger.error("Facebook 預覽失敗: %s", e, exc_info=True)
        await message.reply(f"Facebook 預覽失敗：{e}", mention_author=False)
        return False
