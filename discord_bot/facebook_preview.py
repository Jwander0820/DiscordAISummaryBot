from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import aiohttp
import discord
from bs4 import BeautifulSoup

logger = logging.getLogger("discord_digest_bot")

FACEBOOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|mbasic\.)?(?:facebook\.com|fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)

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


def _meta_content(soup: BeautifulSoup, key: str) -> Optional[str]:
    tag = soup.find("meta", attrs={"property": key})
    if tag and tag.get("content"):
        return tag["content"].strip()

    tag = soup.find("meta", attrs={"name": key})
    if tag and tag.get("content"):
        return tag["content"].strip()

    return None


def _extract_og_data(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = _meta_content(soup, "og:title")
    description = _meta_content(soup, "og:description")
    image = _meta_content(soup, "og:image")
    return title, description, image


async def _fetch_html_with_aiohttp(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], int]:
    last_error = "Facebook 頁面讀取失敗"
    last_status = 0

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        for candidate in _build_candidate_urls(url):
            try:
                async with session.get(candidate, timeout=25, allow_redirects=True) as resp:
                    last_status = resp.status
                    html = await resp.text(errors="ignore")
                    title, description, image = _extract_og_data(html)

                    if title or description or image:
                        return title, description, image, str(resp.url), resp.status

                    if resp.status < 400:
                        return title, description, image, str(resp.url), resp.status

                    last_error = f"Facebook 頁面讀取失敗（HTTP {resp.status}）"
            except Exception as exc:
                last_error = f"Facebook 頁面讀取失敗（{exc}）"

    raise RuntimeError(last_error if last_status else "Facebook 頁面讀取失敗（無有效回應）")


def _fetch_og_with_playwright_sync(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"], locale="zh-TW")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)

        def read_meta(selector: str) -> Optional[str]:
            node = page.query_selector(selector)
            if node:
                content = node.get_attribute("content")
                if content:
                    return content.strip()
            return None

        title = read_meta('meta[property="og:title"]') or read_meta('meta[name="og:title"]')
        description = read_meta('meta[property="og:description"]') or read_meta('meta[name="og:description"]')
        image = read_meta('meta[property="og:image"]') or read_meta('meta[name="og:image"]')
        final_url = page.url

        context.close()
        browser.close()
        return title, description, image, final_url


async def _fetch_og_data(url: str) -> Tuple[str, str, Optional[str], str]:
    try:
        title, description, image, final_url, status = await _fetch_html_with_aiohttp(url)
        if status >= 400 and not (title or description or image):
            raise RuntimeError(f"HTTP {status} 且無可用 Open Graph")

        return (title or "Facebook 貼文", description or "", image, final_url or url)
    except Exception as http_error:
        logger.warning("aiohttp 抓 Facebook Open Graph 失敗，改用 Playwright: %s", http_error)

    try:
        title, description, image, final_url = await asyncio.to_thread(_fetch_og_with_playwright_sync, url)
        return (title or "Facebook 貼文", description or "", image, final_url or url)
    except Exception as playwright_error:
        logger.warning("Playwright 擷取 Facebook Open Graph 失敗: %s", playwright_error)
        return ("Facebook 貼文", "", None, url)


async def build_facebook_preview(url: str, *, reupload_image: bool = True) -> FacebookPreview:
    title, description, image_url, final_url = await _fetch_og_data(url)

    embed = discord.Embed(
        title=title[:256],
        description=description[:4096],
        url=final_url,
    )
    embed.set_author(name="Facebook")

    files: List[discord.File] = []
    if image_url:
        if reupload_image:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
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
    except Exception as exc:
        logger.error("Facebook 預覽失敗: %s", exc, exc_info=True)
        await message.reply(f"Facebook 預覽失敗：{exc}", mention_author=False)
        return False
