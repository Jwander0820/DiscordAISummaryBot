"""Private preview diagnostics and fixed public failure messages."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

from ..chat.records import build_summary_record
from ..notifications.discord_forwarder import forward_notify_to_channel

logger = logging.getLogger("discord_digest_bot")
NOTIFICATION_TIMEOUT_SECONDS = 5
_SAFE_CODE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
COMMON_DIAGNOSTICS = {
    "prepare_failed": "預覽抓取或組裝發生未預期錯誤。",
    "send_failed": "Discord 預覽發送失敗，原始訊息保留。",
    "cleanup_failed": "預覽已送出，但原始訊息清理失敗。",
    "no_content": "未取得可用的貼文內容。",
    "media_unavailable": "媒體無法呈現，且沒有可顯示的正文。",
    "fetch_failed": "貼文抓取失敗。",
    "unknown_error": "預覽回傳未識別的失敗分類，請檢查伺服器紀錄。",
}
PLATFORM_DIAGNOSTICS = {
    "Threads": {
        "invalid_post": "Threads 未提供有效貼文，請確認原連結。",
        "restricted_content": "Threads 限制這則貼文的觀看權限，請直接開啟原連結。",
        "cookie_restricted_content": "Threads 登入備援仍收到限制觀看頁，請用提供 Cookie 的帳號開啟原連結確認。",
        "cookie_login_required": "Threads 要求重新登入或驗證，請管理員確認登入狀態並更新 Cookie 設定，或直接開啟原連結。",
        "cookie_rate_limited": "Threads 暫時限制請求，請稍後再試或直接開啟原連結。",
        "cookie_config_invalid": "Threads 登入備援設定格式不正確，請管理員檢查設定。",
        "cookie_fetch_failed": "Threads 登入備援抓取失敗，請稍後再試或直接開啟原連結。",
        "cookie_no_content": "Threads 登入備援仍無法取得這則貼文，請直接開啟原連結。",
    },
    "Facebook": {
        "login_required": "Facebook 頁面要求登入或存取限制。",
        "request_rejected": "Facebook 拒絕頁面請求。",
        "rate_limited": "Facebook 暫時限制請求。",
        "redirect_failed": "Facebook 重新導向缺失、循環或超過上限。",
        "redirect_rejected": "Facebook 重新導向指向非 Facebook 網域。",
        "cookie_request_rejected": "Facebook 拒絕 Cookie 備援請求。",
        "cookie_target_unresolved": "Facebook Cookie 備援無法確認目標貼文。",
        "cookie_config_invalid": "Facebook 登入備援設定格式不正確。",
        "cookie_login_required": "Facebook Cookie 備援要求登入或驗證。",
        "cookie_rate_limited": "Facebook Cookie 備援暫時被限流。",
        "cookie_redirect": "Facebook Cookie 備援收到重新導向，已停止。",
        "cookie_fetch_failed": "Facebook Cookie 備援抓取失敗。",
        "not_public": "Facebook 目標貼文不是公開內容。",
        "public_unverified": "Facebook 無法確認目標貼文的公開範圍。",
        "target_not_found": "Facebook 未找到對應目標貼文。",
        "target_unresolved": "Facebook 無法確認目標貼文識別。",
        "target_has_no_content": "Facebook 目標貼文沒有可用內容。",
        "response_too_large": "Facebook 回應超過解析大小上限。",
        "parse_budget_exhausted": "Facebook 解析額度已用完。",
    },
    "Instagram": {
        "login_required": "Instagram 要求登入或驗證，請管理員更新 Instagram Cookie，或直接開啟原連結。",
        "checkpoint_required": "Instagram 要求完成登入安全驗證，請管理員在 Instagram 完成驗證後更新 Cookie，或直接開啟原連結。",
        "request_rejected": "Instagram 拒絕這次媒體請求，請稍後再試或直接開啟原連結。",
        "cookie_config_invalid": "Instagram 登入備援設定格式不正確，請管理員檢查設定。",
        "rate_limited": "Instagram 暫時限制請求，請稍後再試或直接開啟原連結。",
        "private_or_unverified": "Instagram 貼文為私人內容或無法確認公開範圍，請直接開啟原連結。",
        "not_found": "Instagram 貼文不存在或已無法存取，請直接開啟原連結。",
        "no_content": "Instagram 未回傳可用的貼文內容；可設定 Instagram Cookie 備援，或直接開啟原連結。",
        "fetch_failed": "Instagram 預覽抓取失敗，請稍後再試或直接開啟原連結。",
    },
}


def failure_reason(platform: str, reason: str | None) -> str | None:
    if not reason:
        return None
    return reason if reason in PLATFORM_DIAGNOSTICS[platform] or reason in COMMON_DIAGNOSTICS else "unknown_error"


def public_failure_message(platform: str) -> str:
    return f"{platform}預覽失敗 爛meta"


def _notification_url(url: str) -> str:
    """Keep source identifiers, never arbitrary tracking or credential queries."""
    parsed = urlsplit(url)
    try:
        fields = parse_qsl(parsed.query, max_num_fields=100)
    except ValueError:
        fields = []
    query = urlencode([
        (key, value) for key, value in fields
        if key in {"id", "story_fbid", "fbid", "v"} and _SAFE_CODE.fullmatch(value)
    ])
    return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, query, ""))


async def notify_preview_failure(
    message, *, platform: str, url: str, reason: str | None, bot_client=None,
    exception: Exception | None = None, http_status: int | None = None, trace_id: str | None = None,
) -> None:
    if not reason:
        return
    try:
        reason = failure_reason(platform, reason)
        diagnostic = PLATFORM_DIAGNOSTICS[platform].get(reason) or COMMON_DIAGNOSTICS[reason]
        context = []
        if exception is not None:
            name = type(exception).__name__
            context.append(f"error_type={name if _SAFE_CODE.fullmatch(name) else 'unknown'}")
        for key, value in (
            ("http_status", http_status if http_status is not None else getattr(exception, "status", None)),
            ("error_code", getattr(exception, "code", None)),
        ):
            if type(value) is int:
                context.append(f"{key}={value}")
        if isinstance(trace_id, str) and _SAFE_CODE.fullmatch(trace_id):
            context.append(f"trace_id={trace_id}")
        logger.log(logging.ERROR if exception is not None else logging.WARNING,
                   "%s preview unavailable: reason=%s %s", platform, reason, " ".join(context))
        record = build_summary_record(
            channel_id=str(getattr(message.channel, "id", "unknown")),
            user_id=str(getattr(message.author, "id", "unknown")),
            command=f"{platform} 預覽", question=_notification_url(url), summary=" ".join(context),
        )
        await asyncio.wait_for(
            forward_notify_to_channel(
                record=record, guild=getattr(message, "guild", None), bot=bot_client,
                notify_type="error", error=f"{reason}: {diagnostic}",
                source_user=message.author, source_channel=message.channel,
            ),
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error("%s preview failure notification failed: error_type=%s", platform, type(exc).__name__)


async def reply_preview_failure(message, *, platform: str, spoiler: bool = False) -> None:
    text = public_failure_message(platform)
    try:
        await message.reply(f"||{text}||" if spoiler else text, mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        logger.error("%s public failure reply failed: error_type=%s", platform, type(exc).__name__)
