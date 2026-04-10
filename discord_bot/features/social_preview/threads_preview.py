# threads_preview.py
from __future__ import annotations
"""
將 Threads 貼文轉為 Discord 預覽的工具函式。

主要功能：
- extract_threads_urls: 從訊息字串擷取 Threads 貼文 URLs
- build_threads_preview: 取得單一貼文的 Embed 與附件（圖片/影片連結）
- handle_threads_in_message: Discord on_message 幫手，偵測 & 回覆預覽

依賴：
- 你專案內的 threads_fetch.fetch_threads_post(url)
- discord.py, aiohttp

使用方式參考檔尾範例。
"""

import re
import asyncio
import io
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

import aiohttp
import discord
from discord.errors import HTTPException, InteractionResponded, NotFound

# 你已有的抓取器
from .threads_fetch import fetch_threads_post
from .sender import cleanup_source_message, send_preview_as_author
from .text import extract_message_commentary


THREADS_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@[^\s/]+/post/[A-Za-z0-9_\-]+(?:\?[^\s<>()|]+)?(?:#[^\s<>()|]+)?",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)

# --- 小工具資料結構 ---

load_dotenv()
logger = logging.getLogger('discord_digest_bot')


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
    建立給 Discord 用的預覽結果。
    :param embed: Discord Embed
    :param files: discord.File 附件清單（通常包含下載後的首圖）
    :param extra_text: 額外要貼在訊息裡的文字（例如影片連結）
    """
    embed: discord.Embed
    files: List[discord.File]
    extra_text: Optional[str] = None
    video_url: Optional[str] = None
    inline_video_url: Optional[str] = None


# --- Discord 互動元件 ---


class DeletePreviewView(discord.ui.View):
    """提供刪除 Threads 預覽訊息的按鈕。"""

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
    async def delete_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # pragma: no cover - 互動流程難以以測試覆蓋
        try:
            await interaction.response.send_message("預覽已刪除。", ephemeral=True)
        except InteractionResponded:
            pass

        preview_message = interaction.message
        channel = getattr(preview_message, "channel", None)
        deleter = f"{interaction.user.global_name}"

        try:
            await interaction.message.delete()
        except (NotFound, HTTPException):
            # 訊息已刪除或 Discord 拒絕刪除，忽略即可。
            logger.info(
                "Threads 預覽刪除失敗：訊息不存在或無法刪除 "
                f"{deleter} 想刪除 {channel} 內的 Threads預覽對話",
            )
            return

        # 若有額外附件訊息，一併刪除，避免只刪到上方卡片
        if channel and self.related_message_ids:
            for message_id in list(self.related_message_ids):
                try:
                    related = await channel.fetch_message(message_id)
                    await related.delete()
                except (NotFound, HTTPException, AttributeError):
                    continue

        logger.info(
            "Threads 預覽已刪除 "
            f"{deleter} 刪除了 {channel} 內的 Threads預覽對話",
        )
    async def on_timeout(self) -> None:  # pragma: no cover - 互動流程難以以測試覆蓋
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=None)
            except (NotFound, HTTPException):
                pass


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


def _spoiler_wrap(text: str) -> str:
    """用 Discord spoiler 語法包住字串。"""
    if not text:
        return text
    return f"||{text}||"


def _clone_files_as_spoiler(files: List[discord.File]) -> List[discord.File]:
    """複製附件並改成 spoiler，避免直接重用舊檔案指標。"""
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


def _format_masked_link(label: str, url: str) -> str:
    """把長影片網址縮成 Discord markdown 連結。"""
    return f"[{label}]({url})"


def _extract_threads_commentary(content: str, url: str) -> str:
    """移除目標 Threads URL 後保留使用者評論文字。"""
    return extract_message_commentary(
        content,
        target_url=url,
        url_pattern=THREADS_URL_RE,
        sanitize_url=_sanitize_threads_url,
    )


# --- 下載媒體 ---

async def _download_bytes(session: aiohttp.ClientSession, url: str, timeout_sec: int = 20) -> Optional[bytes]:
    """
    下載二進位內容（圖片/影片），失敗回 None。
    """
    try:
        async with session.get(url, timeout=timeout_sec) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None


# --- 建立預覽 ---

async def build_threads_preview(
    url: str,
    *,
    reupload_image: bool = True,
    allow_video_upload: bool = False,
    spoiler: bool = False,
) -> PreviewResult:
    """
    取得單一 Threads URL 的預覽結果（Embed + 附件）。
    :param url: Threads 貼文 URL
    :param reupload_image: 是否下載首圖後以上傳附件（建議 True，避免簽名網址失效）
    :param allow_video_upload: 若為 mp4 且檔案不大是否嘗試上傳（預設 False，避免大檔）
    :return: PreviewResult
    """
    # fetch_threads_post() 仍是同步 parser，這裡轉到 thread 避免卡住 bot event loop。
    post = await asyncio.to_thread(fetch_threads_post, url)

    # --- 準備 Embed ---
    title = f"Threads @{post.author_username or ''}".strip() or "Threads 貼文"
    description = (post.text or "(無文字內容)")[:4096]
    embed = discord.Embed(title=title, description=description, url=post.url)

    # 作者顯示
    author_display = post.author_name
    if not author_display or author_display.lower() == "threads":
        # 若抓到的 author_name 是預設 "Threads" 或空，就用 @username 當顯示
        author_display = f"@{post.author_username}" if post.author_username else "Threads"
    embed.set_author(name=author_display)
    if spoiler:
        embed.description = _spoiler_wrap(description)

    # 補時間（若你之後抓得到）
    # if post.created_at:
    #     embed.set_footer(text=post.created_at)

    # --- 圖片處理 ---
    image_urls = [m.url for m in (post.media or []) if m.type == "image"]
    files: List[discord.File] = []

    if image_urls:
        first_img = image_urls[0]
        if reupload_image:
            # 下載後上傳，避免 instagram CDN 簽名參數過期
            async with aiohttp.ClientSession() as sess:
                data = await _download_bytes(sess, first_img)
            if data:
                fname = "threads_image.jpg"  # 以實際 content-type 命名更好，可在 _download_bytes 補判斷
                file = discord.File(io.BytesIO(data), filename=fname, spoiler=spoiler)
                files.append(file)
                # 注意：spoiler=True 時 discord 可能改寫成 SPOILER_ 前綴，需用 file.filename
                embed.set_image(url=f"attachment://{file.filename}")
            else:
                # 下載失敗就直接熱鏈接
                if not spoiler:
                    embed.set_image(url=first_img)
        else:
            if not spoiler:
                embed.set_image(url=first_img)

    # --- 影片處理 ---
    video_urls = [m.url for m in (post.media or []) if m.type == "video"]

    extra_lines = []
    primary_video_url: Optional[str] = None
    inline_video_url: Optional[str] = None
    if video_urls:
        # m3u8 不適合上傳，直接貼連結；mp4 若小可選擇嘗試下載上傳
        mp4s = [u for u in video_urls if u.lower().endswith(".mp4")]
        hls = [u for u in video_urls if u.lower().endswith(".m3u8")]

        if allow_video_upload and mp4s:
            # （可選）小檔案才建議上傳，避免觸發檔案大小限制
            async with aiohttp.ClientSession() as sess:
                data = await _download_bytes(sess, mp4s[0], timeout_sec=40)
            if data and len(data) < 20 * 1024 * 1024:  # 20MB 只是範例，依你的機器人等級調整
                vname = "threads_video.mp4"
                files.append(discord.File(io.BytesIO(data), filename=vname, spoiler=spoiler))
                # Discord 無法把影片當 embed.image，直接上傳附件即可
            else:
                primary_video_url = mp4s[0]
        elif mp4s:
            primary_video_url = mp4s[0]

        if not primary_video_url and hls:
            primary_video_url = hls[0]

        # 讓第一支影片網址出現在同一則卡片訊息中，交給 Discord 自動轉成影片預覽。
        inline_video_url = primary_video_url

        # 額外影片只提供簡短遮罩連結，避免訊息出現超長 CDN URL。
        for index, u in enumerate(mp4s[1:], start=2 if primary_video_url and primary_video_url == mp4s[0] else 1):
            extra_lines.append(_format_masked_link(f"影片 {index}", u))
        for index, u in enumerate(hls[1:] if primary_video_url and hls and primary_video_url == hls[0] else hls, start=1):
            extra_lines.append(_format_masked_link(f"HLS {index}", u))

    extra_text = "\n".join(extra_lines) if extra_lines else None
    if spoiler and extra_text:
        extra_text = _spoiler_wrap(extra_text)
    return PreviewResult(
        embed=embed,
        files=files,
        extra_text=extra_text,
        video_url=primary_video_url,
        inline_video_url=inline_video_url,
    )


# --- Discord 事件整合（範例） ---

async def handle_threads_in_message(message: discord.Message) -> bool:
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
    try:
        logger.info(f"{message.author.nick or message.author.global_name} 在 {message.channel.name} 貼了url {url}")
        use_spoiler = _is_threads_url_spoilered(message.content or "", url)
        user_commentary = _extract_threads_commentary(message.content or "", url)

        if use_spoiler:
            preview = await build_threads_preview(
                url,
                reupload_image=True,
                allow_video_upload=True,
                spoiler=False,
            )

            if preview.embed.description:
                preview.embed.description = _spoiler_wrap(preview.embed.description)
            preview.embed.set_image(url=None)
            content_lines = []
            if user_commentary:
                content_lines.append(user_commentary)
            if preview.extra_text:
                content_lines.append(_spoiler_wrap(preview.extra_text))
            spoiler_content = "\n".join(content_lines) if content_lines else None

            view = DeletePreviewView(original_url=url, video_url=preview.video_url)
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

            await cleanup_source_message(message, platform="Threads", url=url)
            return True

        preview = await build_threads_preview(
            url,
            reupload_image=True,
            allow_video_upload=False,
            spoiler=False,
        )

        view = DeletePreviewView(original_url=url, video_url=preview.video_url)
        reply_kwargs = {
            "embed": preview.embed,
            "view": view,
            "mention_author": False,
        }
        content_lines = []
        if user_commentary:
            content_lines.append(user_commentary)
        if preview.inline_video_url:
            content_lines.append(preview.inline_video_url)
        if preview.extra_text:
            content_lines.append(preview.extra_text)
        if content_lines:
            reply_kwargs["content"] = "\n".join(content_lines)
        if preview.files:
            reply_kwargs["files"] = preview.files
        sent = await send_preview_as_author(
            message,
            content=reply_kwargs.get("content"),
            embed=reply_kwargs.get("embed"),
            files=reply_kwargs.get("files"),
            view=reply_kwargs.get("view"),
        )
        view.message = sent

        await cleanup_source_message(message, platform="Threads", url=url)

        return True
    except Exception as e:
        logger.error(e, exc_info=True)
        await message.reply(f"Threads 預覽失敗：{e}", mention_author=False)
        return False
