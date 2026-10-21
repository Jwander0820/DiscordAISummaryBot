# threads_preview.py
from __future__ import annotations
"""
將 Threads 貼文轉為 Discord 預覽的工具函式。

主要功能：
- extract_threads_urls: 從訊息字串擷取 Threads 貼文 URLs
- build_threads_preview: 取得單一貼文的文字與媒體資料
- handle_threads_in_message: Discord on_message 幫手，偵測 & 回覆預覽

依賴：
- 你專案內的 threads_fetch.fetch_threads_post(url)
- discord.py, aiohttp

文字與媒體資料交給共用 Components V2 view 呈現。
"""

import re
import asyncio
import os
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

import aiohttp
import discord

# 你已有的抓取器
from .threads_fetch import fetch_threads_post
from .download import (
    MAX_IMAGE_DOWNLOAD_BYTES,
    download_bytes_limited,
)
from .sender import cleanup_source_message, log_preview_link, send_preview_group
from .duplicate_patrol import record_delivered_preview
from .media_gallery import prepare_gallery
from .text import TextSpan, extract_message_commentary
from .failure_notifications import failure_reason, notify_preview_failure, public_failure_message, reply_preview_failure


THREADS_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/(?:@[^\s/]+/post/[A-Za-z0-9_\-]+|share/[A-Za-z0-9_\-]+)/*(?:\?[^\s<>()|]+)?(?:#[^\s<>()|]+)?",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
THREADS_INVALID_POST_MESSAGE = public_failure_message("Threads")

# --- 小工具資料結構 ---

load_dotenv()


def _delete_view_timeout_from_env() -> Optional[float]:
    """讀取預覽刪除按鈕的逾時秒數設定。"""
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

@dataclass
class PreviewResult:
    """
    建立 V2 所需的預覽資料。
    :param embed: 沿用的內部文字資料容器，不直接傳送 Embed
    :param files: discord.File 附件清單（最多四張圖片）
    """
    embed: discord.Embed
    files: List[discord.File]
    video_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    media_urls: Optional[List[str]] = None
    media_spoilers: Optional[List[bool]] = None
    text_spans: Optional[List[TextSpan]] = None
    preview_error: Optional[str] = None


# --- URL 擷取 ---

def _sanitize_threads_url(url: str) -> str:
    """清掉 Threads 分享網址的 tracking query 與尾端標點。"""
    # 連結包在 ||spoiler|| 時，regex 可能把結尾 || 一起吃進來
    clean = url.rstrip(").,>|")
    parsed = urlparse(clean)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))

def extract_threads_urls(content: str) -> List[str]:
    """
    從原始訊息內容中擷取 Threads 貼文 URL 清單。

    這裡允許抓到 `?xmt=...` 等 query，但回傳前會正規化成乾淨 URL，方便後續 preview 與留言抽取共用。
    :param content: 訊息字串
    :return: list[str]
    """
    if not content:
        return []
    urls = THREADS_URL_RE.findall(content)
    # 正規化去重
    seen, out = set(), []
    for u in urls:
        u = _sanitize_threads_url(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _is_threads_url_spoilered(content: str, url: str) -> bool:
    """判斷目標 Threads URL 是否被 Discord spoiler 包住。"""
    if not content:
        return False
    for block in SPOILER_BLOCK_RE.findall(content):
        if url in extract_threads_urls(block):
            return True
    return False


def _extract_threads_commentary(content: str, url: str) -> str:
    """移除目標 Threads URL 後保留使用者評論文字。"""
    return extract_message_commentary(
        content,
        target_url=url,
        url_pattern=THREADS_URL_RE,
        sanitize_url=_sanitize_threads_url,
    )


# --- 下載媒體 ---

async def _download_bytes(
    session: aiohttp.ClientSession,
    url: str,
    timeout_sec: int = 20,
    *,
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
    media_type: str = "image",
) -> Optional[bytes]:
    """
    下載二進位內容（圖片/影片），失敗回 None。
    """
    return await download_bytes_limited(
        session,
        url,
        max_bytes=max_bytes,
        timeout_sec=timeout_sec,
        media_type=media_type,
    )


# --- 建立預覽 ---

async def build_threads_preview(
    url: str,
    *,
    reupload_image: bool = True,
    spoiler: bool = False,
) -> PreviewResult:
    """
    取得單一 Threads URL 的文字資料、圖片附件與影片直連。
    :param url: Threads 貼文 URL
    :param reupload_image: 是否將前四個媒體中的圖片下載後上傳附件
    :return: PreviewResult
    """
    # fetch_threads_post() 仍是同步 parser，這裡轉到 thread 避免卡住 bot event loop。
    post = await asyncio.to_thread(fetch_threads_post, url)

    error = failure_reason("Threads", post.preview_error or ("no_content" if not (post.text or post.media) else None))
    if error:
        embed = discord.Embed(title="Threads", description=THREADS_INVALID_POST_MESSAGE, url=url)
        embed.set_author(name="Threads")
        return PreviewResult(embed=embed, files=[], preview_error=error)

    # Embed 僅作為內部資料容器；Discord 訊息由共用 V2 view 組裝。
    title = f"Threads @{post.author_username or ''}".strip() or "Threads 貼文"
    description = post.text or ""
    embed = discord.Embed(title=title, description=description, url=post.url)

    # 作者顯示
    author_display = post.author_name
    if not author_display or author_display.lower() == "threads":
        # 若抓到的 author_name 是預設 "Threads" 或空，就用 @username 當顯示
        author_display = f"@{post.author_username}" if post.author_username else "Threads"
    embed.set_author(name=author_display)

    async with aiohttp.ClientSession() as session:
        gallery = await prepare_gallery(session, post.media or [], downloader=_download_bytes,
                                        prefix="threads_image", spoiler=spoiler, reupload_images=reupload_image)
    if gallery.files:
        embed.set_image(url=f"attachment://{gallery.files[0].filename}")
    if not gallery.media_urls and not post.text:
        embed.description = THREADS_INVALID_POST_MESSAGE
        return PreviewResult(embed=embed, files=[], preview_error="media_unavailable")

    return PreviewResult(
        embed=embed,
        files=gallery.files,
        video_url=gallery.video_url,
        image_urls=gallery.image_urls,
        media_urls=gallery.media_urls,
        media_spoilers=gallery.media_spoilers,
        text_spans=post.text_spans,
    )


async def _send_threads_preview(
    message: discord.Message, preview: PreviewResult, *, original_url: str, commentary: str, spoiler: bool
) -> None:
    from .preview_view import build_social_preview_views

    image_url = preview.embed.image.url
    views = build_social_preview_views(
        original_url=preview.embed.url or original_url,
        video_url=preview.video_url,
        title=preview.embed.title or "Threads",
        author=preview.embed.author.name or "Threads",
        text=preview.embed.description or "",
        commentary=commentary,
        spoiler=spoiler,
        timeout=DELETE_VIEW_TIMEOUT,
        image_urls=preview.image_urls or ([image_url] if image_url and not preview.files else []),
        files=preview.files,
        media_urls=preview.media_urls,
        media_spoilers=preview.media_spoilers,
        text_spans=preview.text_spans,
    )
    sent_messages = await send_preview_group(message, views)
    await record_delivered_preview(message, platform="threads", url=preview.embed.url or original_url,
                                   sent_messages=sent_messages, preview_error=preview.preview_error)


# --- Discord 事件整合（範例） ---

async def handle_threads_in_message(
    message: discord.Message, *, bot_client: Optional[discord.Client] = None
) -> bool:
    """
    Threads 訊息處理主流程。

    負責：
    1. 抽出第一個 Threads URL
    2. 保留使用者原本的評論
    3. 送出代發 preview
    4. 清理原始訊息
    :return: 是否有處理（找到並回覆）
    """
    if message.author.bot:
        return False

    urls = extract_threads_urls(message.content or "")
    if not urls:
        return False

    # 限制一次只處理第一個（多個可自行迴圈）
    url = urls[0]
    phase, use_spoiler = "prepare", False
    try:
        log_preview_link(message, platform="Threads", url=url)
        use_spoiler = _is_threads_url_spoilered(message.content or "", url)
        user_commentary = _extract_threads_commentary(message.content or "", url)

        preview = await build_threads_preview(url, reupload_image=True, spoiler=use_spoiler)
        await notify_preview_failure(message, platform="Threads", url=url,
                                     reason=preview.preview_error, bot_client=bot_client)
        phase = "send"
        await _send_threads_preview(
            message, preview, original_url=url, commentary=user_commentary, spoiler=use_spoiler,
        )
        phase = "cleanup"
        await cleanup_source_message(message, platform="Threads", url=url)
        return True
    except Exception as e:
        await notify_preview_failure(message, platform="Threads", url=url, reason=f"{phase}_failed",
                                     bot_client=bot_client, exception=e)
        if phase == "cleanup":
            return True
        await reply_preview_failure(message, platform="Threads", spoiler=use_spoiler)
        return False
