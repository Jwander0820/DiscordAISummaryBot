from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from .download import (
    MAX_IMAGE_DOWNLOAD_BYTES,
    download_bytes_limited,
)
from .media_gallery import prepare_gallery
from .sender import cleanup_source_message, log_preview_link, send_preview_group
from .text import extract_message_commentary
from .facebook_fetch import (
    MAX_FACEBOOK_HTML_BYTES, FacebookMedia, FacebookPostData, direct_facebook_mp4, extract_facebook_post,
    facebook_post_identity, is_facebook_video_page,
)

logger = logging.getLogger("discord_digest_bot")


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

FACEBOOK_URL_RE = re.compile(
    r"https?://(?:(?:[a-z0-9-]+\.)*facebook\.com|(?:www\.)?fb\.com|(?:www\.)?fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
FACEBOOK_INVALID_POST_MESSAGE = "Facebook預覽失敗 爛meta"

FACEBOOK_CONTENT_HOSTS = ("www.facebook.com", "m.facebook.com", "mbasic.facebook.com")
FACEBOOK_REDIRECT_ONLY_HOSTS = {"l.facebook.com", "lm.facebook.com", "fb.watch", "www.fb.watch"}
FACEBOOK_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
FACEBOOK_MAX_REDIRECTS = 8
FACEBOOK_ACCESS_WALL_PATHS = ("/login", "/checkpoint", "/recover", "/unsupportedbrowser")

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
    video_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    media_urls: Optional[List[str]] = None


class FacebookPreviewUnavailable(RuntimeError):
    """Facebook 未提供可用的公開貼文資料。"""

    def __init__(self, message: str, *, target_url: str | None = None):
        super().__init__(message)
        self.target_url = target_url


def extract_facebook_urls(content: str) -> List[str]:
    """從訊息中擷取 Facebook/fb.watch 連結並去重。"""
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
    """判斷指定 Facebook URL 是否被 Discord spoiler 包住。"""
    if not content:
        return False
    for block in SPOILER_BLOCK_RE.findall(content):
        if url in extract_facebook_urls(block):
            return True
    return False


def _extract_facebook_commentary(content: str, url: str) -> str:
    """抽掉目標 URL 後，保留使用者原本的前後評論文字。"""
    return extract_message_commentary(
        content,
        target_url=url,
        url_pattern=FACEBOOK_URL_RE,
        sanitize_url=lambda raw_url: raw_url.rstrip(").,>|"),
    )


def _build_candidate_urls(url: str) -> List[str]:
    """建立 Facebook 多 host 版本，提升 Open Graph 抓取成功率。"""
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")

    host = (parsed.hostname or "").lower()
    if host in FACEBOOK_REDIRECT_ONLY_HOSTS or host == "fb.com" or host.endswith(".fb.com"):
        return [url]

    candidates = [url]
    for candidate_host in FACEBOOK_CONTENT_HOSTS:
        candidates.append(urlunparse((parsed.scheme, candidate_host, parsed.path, "", parsed.query, "")))

    seen = set()
    uniq = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            uniq.append(candidate)
    return uniq


def _is_supported_facebook_url(url: str) -> bool:
    """限制 redirect 只能停留在 Facebook 自有 HTTP(S) 網域。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host in {"fb.watch", "www.fb.watch", "fb.com", "www.fb.com"}
    )


def _is_facebook_access_wall(url: str, html: str) -> bool:
    """辨識明確的登入／checkpoint 頁面，不把通用 metadata 當貼文。"""
    path = urlparse(url).path.lower()
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in FACEBOOK_ACCESS_WALL_PATHS):
        return True

    html_lower = html.lower()
    return any(
        marker in html_lower
        for marker in (
            'id="login_form"',
            "id='login_form'",
            'name="login"',
            "name='login'",
            "you must log in to continue",
        )
    )


async def _fetch_candidate_following_redirects(
    session: aiohttp.ClientSession,
    url: str,
) -> Tuple[str, int, str]:
    """逐段跟隨受限的 Facebook redirect，回傳最終 URL、狀態與 HTML。"""
    current_url = url
    visited = set()

    for _ in range(FACEBOOK_MAX_REDIRECTS + 1):
        if current_url in visited:
            raise FacebookPreviewUnavailable("Facebook 重新導向形成循環")
        if not _is_supported_facebook_url(current_url):
            raise FacebookPreviewUnavailable("Facebook 重新導向指向非 Facebook 網域")
        visited.add(current_url)

        async with session.get(current_url, timeout=25, allow_redirects=False) as resp:
            status = resp.status
            response_url = str(resp.url)
            if status not in FACEBOOK_REDIRECT_STATUSES:
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if len(body) + len(chunk) > MAX_FACEBOOK_HTML_BYTES:
                        raise FacebookPreviewUnavailable("Facebook 頁面超過解析大小上限")
                    body.extend(chunk)
                return response_url, status, body.decode("utf-8", errors="replace")

            location = resp.headers.get("Location")
            if not location:
                raise FacebookPreviewUnavailable("Facebook 重新導向缺少 Location")

            next_url = urljoin(response_url, location)
            if not _is_supported_facebook_url(next_url):
                raise FacebookPreviewUnavailable("Facebook 重新導向指向非 Facebook 網域")
            current_url = next_url

    raise FacebookPreviewUnavailable("Facebook 重新導向次數超過上限")


async def _download_bytes(
    session: aiohttp.ClientSession,
    url: str,
    timeout_sec: int = 20,
    *,
    max_bytes: int = MAX_IMAGE_DOWNLOAD_BYTES,
    media_type: str = "image",
) -> Optional[bytes]:
    """下載圖片或影片位元組，失敗時回傳 `None`。"""
    return await download_bytes_limited(
        session,
        url,
        max_bytes=max_bytes,
        timeout_sec=timeout_sec,
        media_type=media_type,
    )


def _meta_contents(soup: BeautifulSoup, key: str) -> List[str]:
    """收集指定 meta key 的內容，支援 property/name 並去重。"""
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


def _extract_page_data(html: str, page_url: str = "") -> FacebookPostData:
    """從 HTML 抽出 Facebook Open Graph 標題、描述、圖片與影片網址。"""
    soup = BeautifulSoup(html, "html.parser")
    title = (_meta_contents(soup, "og:title") or [""])[0]
    description = (_meta_contents(soup, "og:description") or [""])[0]
    image_urls = _meta_contents(soup, "og:image")

    video_urls = []
    for key in ("og:video", "og:video:url", "og:video:secure_url"):
        video_urls.extend(_meta_contents(soup, key))
    # fallback: 有些頁面只放 twitter player
    video_urls.extend(_meta_contents(soup, "twitter:player:stream"))

    seen_video = set()
    unique_video_urls = []
    structured = extract_facebook_post(html, page_url)
    for raw_url in structured.video_urls + video_urls:
        url = direct_facebook_mp4(raw_url)
        if not url:
            continue
        if url not in seen_video:
            seen_video.add(url)
            unique_video_urls.append(url)

    media = structured.media
    if unique_video_urls and not structured.video_urls and is_facebook_video_page(structured.url or page_url):
        media = [FacebookMedia("video", unique_video_urls[0])]
    return FacebookPostData(url=structured.url or page_url, title=title or structured.title,
                            description=description or structured.description,
                            image_urls=image_urls or structured.image_urls, video_urls=unique_video_urls,
                            media=media)


def _extract_og_data(html: str, page_url: str = "") -> Tuple[str, str, List[str], List[str]]:
    post = _extract_page_data(html, page_url)
    return post.title, post.description, post.image_urls, post.video_urls


async def _fetch_html_with_aiohttp(url: str) -> FacebookPostData:
    """展開受限 redirect 後抓 OG 資料，登入牆與外部目的地直接失敗。"""
    last_error = "Facebook 頁面讀取失敗"
    last_status = 0
    cookie_target = None

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        candidates = _build_candidate_urls(url)
        attempted = set()
        index = 0
        while index < len(candidates):
            candidate = candidates[index]
            index += 1
            if candidate in attempted:
                continue
            attempted.add(candidate)

            try:
                final_url, status, html = await _fetch_candidate_following_redirects(session, candidate)
                if facebook_post_identity(final_url):
                    cookie_target = final_url
                attempted.add(final_url)
                last_status = status
                if _is_facebook_access_wall(final_url, html):
                    last_error = "Facebook 頁面要求登入或存取限制"
                    continue

                post = _extract_page_data(html, final_url)
                post.status = status
                if status < 400 and (post.title or post.has_content):
                    return post

                resolved_candidates = [
                    resolved_candidate
                    for resolved_candidate in _build_candidate_urls(final_url)
                    if resolved_candidate not in attempted and resolved_candidate not in candidates
                ]
                if resolved_candidates:
                    candidates[index:index] = resolved_candidates

                if status >= 400:
                    last_error = f"Facebook 頁面讀取失敗（HTTP {status}）"
                else:
                    last_error = "Facebook 頁面未提供可用的 Open Graph metadata"
            except FacebookPreviewUnavailable:
                raise
            except Exception as exc:
                last_error = f"Facebook 頁面讀取失敗（{exc}）"

    raise FacebookPreviewUnavailable(last_error if last_status else "Facebook 頁面讀取失敗（無有效回應）",
                                     target_url=cookie_target)


async def _fetch_og_data(url: str) -> FacebookPostData:
    """匿名優先，必要時單次 Cookie 備援；只接受公開的目標內容。"""
    anonymous = None
    last_error = None
    cookie_target = url
    try:
        anonymous = await _fetch_html_with_aiohttp(url)
        if anonymous.status >= 400 and not (anonymous.title or anonymous.has_content):
            raise FacebookPreviewUnavailable(f"HTTP {anonymous.status} 且無可用 Open Graph")
    except Exception as http_error:
        anonymous = None
        logger.warning("aiohttp 擷取 Facebook Open Graph 失敗: %s", http_error)
        last_error = http_error
        cookie_target = getattr(http_error, "target_url", None) or url

    needs_video = is_facebook_video_page(url) or bool(anonymous and is_facebook_video_page(anonymous.url))
    needs_cookie = (anonymous is None or not anonymous.has_content
                    or (needs_video and not anonymous.video_urls))
    cookie = os.environ.get("FACEBOOK_COOKIE", "")
    if needs_cookie and cookie.strip():
        from .facebook_cookie import try_facebook_cookie
        authenticated = await try_facebook_cookie(anonymous.url if anonymous else cookie_target, cookie)
        if authenticated.has_content:
            if anonymous:
                authenticated.title = anonymous.title or authenticated.title
                authenticated.description = anonymous.description or authenticated.description
                if not authenticated.ordered_media:
                    authenticated.media = anonymous.ordered_media
                authenticated.url = anonymous.url
            return authenticated
        if anonymous is None:
            status = authenticated.debug.get("cookie_status")
            status_text = f"HTTP {status}" if isinstance(status, int) else "無 HTTP 回應"
            last_error = FacebookPreviewUnavailable(
                f"Facebook Cookie 備援失敗（{authenticated.reason}; {status_text}）",
                target_url=authenticated.url,
            )
    if anonymous:
        return anonymous
    raise last_error or FacebookPreviewUnavailable("Facebook 頁面未提供可用的公開內容")


async def build_facebook_preview(
    url: str,
    *,
    reupload_image: bool = True,
    max_images: int = 4,
    spoiler: bool = False,
) -> FacebookPreview:
    """取得 V2 所需文字、圖片附件與官方影片直連。"""
    post = await _fetch_og_data(url)

    embed = discord.Embed(
        title=(post.title or "Facebook 貼文")[:256],
        description=post.description,
        url=post.url,
    )
    embed.set_author(name="Facebook")

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        gallery = await prepare_gallery(session, post.ordered_media, downloader=_download_bytes,
                                        prefix="facebook_preview", spoiler=spoiler,
                                        reupload_images=reupload_image, limit=max_images)
    if gallery.files:
        embed.set_image(url=f"attachment://{gallery.files[0].filename}")

    return FacebookPreview(
        embed=embed, files=gallery.files, image_urls=gallery.image_urls,
        video_url=gallery.video_url, media_urls=gallery.media_urls,
    )


async def _send_facebook_preview(
    message: discord.Message, preview: FacebookPreview, *, original_url: str, commentary: str, spoiler: bool
) -> None:
    from .preview_view import build_social_preview_views

    title = preview.embed.title or "Facebook"
    text = preview.embed.description or ""
    if spoiler and title not in {"Facebook", "Facebook 貼文"}:
        # OG titles can contain the post itself, unlike a platform/author label.
        text = "\n".join(part for part in (title, text) if part)
        title = "Facebook 貼文"
    views = build_social_preview_views(
        original_url=original_url,
        title=title,
        author=preview.embed.author.name or "Facebook",
        text=text,
        commentary=commentary,
        spoiler=spoiler,
        timeout=DELETE_VIEW_TIMEOUT,
        video_url=preview.video_url,
        image_urls=preview.image_urls or [],
        files=preview.files,
        media_urls=preview.media_urls,
    )
    await send_preview_group(message, views)


async def handle_facebook_in_message(message: discord.Message) -> bool:
    """Facebook 訊息處理主流程，負責留言保留、preview 代發與原文清理。"""
    if message.author.bot:
        return False

    urls = extract_facebook_urls(message.content or "")
    if not urls:
        return False

    url = urls[0]
    use_spoiler = _is_facebook_url_spoilered(message.content or "", url)
    user_commentary = _extract_facebook_commentary(message.content or "", url)
    try:
        log_preview_link(message, platform="Facebook", url=url)

        preview = await build_facebook_preview(
            url, reupload_image=True, max_images=4, spoiler=use_spoiler,
        )
    except Exception as exc:
        logger.error("Facebook 預覽失敗: %s", exc, exc_info=True)
        embed = discord.Embed(title="Facebook", description=FACEBOOK_INVALID_POST_MESSAGE, url=url)
        embed.set_author(name="Facebook")
        preview = FacebookPreview(embed=embed, files=[])

    # Sending errors leave the original message intact; do not replace a failed
    # transmission with a misleading "Facebook unavailable" card.
    await _send_facebook_preview(
        message, preview, original_url=url, commentary=user_commentary, spoiler=use_spoiler,
    )
    await cleanup_source_message(message, platform="Facebook", url=url)
    return True
