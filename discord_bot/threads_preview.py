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
from .preview_sender import cleanup_source_message, send_preview_as_author


THREADS_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@[^\s/]+/post/[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)
SPOILER_BLOCK_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)

# --- 小工具資料結構 ---

load_dotenv()
logger = logging.getLogger('discord_digest_bot')


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


# --- Discord 互動元件 ---


class DeletePreviewView(discord.ui.View):
    """提供刪除 Threads 預覽訊息的按鈕。"""

    def __init__(self, *, original_url: str, timeout: Optional[float] = DELETE_VIEW_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.related_message_ids: List[int] = []
        self.add_item(discord.ui.Button(label="原連結", style=discord.ButtonStyle.link, url=original_url))

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
    # 連結包在 ||spoiler|| 時，regex 可能把結尾 || 一起吃進來
    clean = url.rstrip(").,>|")
    parsed = urlparse(clean)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))

def extract_threads_urls(content: str) -> List[str]:
    """
    從原始訊息內容中擷取 Threads 貼文 URL 清單
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
    if not content:
        return False
    for block in SPOILER_BLOCK_RE.findall(content):
        if url in extract_threads_urls(block):
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
    # 在背景執行同步抓取（避免阻塞事件迴圈）
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
                extra_lines.append(f"影片（mp4）：{mp4s[0]}")

        # HLS 一律貼連結
        for u in hls:
            extra_lines.append(f"影片（HLS）：{u}")

    extra_text = "\n".join(extra_lines) if extra_lines else None
    if spoiler and extra_text:
        extra_text = _spoiler_wrap(extra_text)
    return PreviewResult(embed=embed, files=files, extra_text=extra_text)


# --- Discord 事件整合（範例） ---

async def handle_threads_in_message(message: discord.Message) -> bool:
    """
    在 on_message 中呼叫，若訊息含 Threads 連結就回覆預覽。
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
            spoiler_content = _spoiler_wrap(preview.extra_text) if preview.extra_text else None

            view = DeletePreviewView(original_url=url)
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

        view = DeletePreviewView(original_url=url)
        reply_kwargs = {
            "embed": preview.embed,
            "view": view,
            "mention_author": False,
        }
        if preview.extra_text:
            reply_kwargs["content"] = preview.extra_text
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
