import logging
import os
from typing import Optional, Union

import discord

logger = logging.getLogger("discord_digest_bot")


def _truncate(text: object, limit: int = 260) -> str:
    if text is None:
        return ""
    value = str(text).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _resolve_int_env(name: str) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        logger.warning("%s 不是有效數字，已略過轉發設定。", name)
        return None
    return int(raw)


async def forward_notify_to_channel(
    *,
    record: dict,
    guild: Optional[discord.Guild] = None,
    bot: Optional[discord.Client] = None,
    notify_type: str = "success",
    email_sent: Optional[bool] = None,
    email_message_id: Optional[str] = None,
    error: Optional[Union[Exception, str]] = None,
) -> bool:
    """
    將通知記錄轉發到指定 Discord 頻道。
    .env:
      - DISCORD_NOTIFY_FORWARD_CHANNEL_ID: 目標頻道 ID（必填）
      - DISCORD_NOTIFY_FORWARD_GUILD_ID: 目標伺服器 ID（可選）
    """
    target_channel_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_CHANNEL_ID")
    if not target_channel_id:
        return False

    target_guild_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_GUILD_ID")
    target_guild = guild
    if target_guild_id is not None:
        target_guild = None
        if bot is not None:
            target_guild = bot.get_guild(target_guild_id)
        if target_guild is None and guild and guild.id == target_guild_id:
            target_guild = guild

    if target_guild is None:
        logger.warning("notify forward: 找不到目標 guild，已跳過。")
        return False

    channel = target_guild.get_channel(target_channel_id)
    if channel is None:
        try:
            channel = await target_guild.fetch_channel(target_channel_id)
        except Exception as exc:
            logger.error("notify forward: 抓取目標頻道失敗: %s", exc, exc_info=True)
            return False

    if not hasattr(channel, "send"):
        logger.warning("notify forward: 目標頻道不支援發送訊息 (%s)", type(channel).__name__)
        return False

    icon = "📣"
    title = "SERN Notify 轉發"
    if notify_type == "error":
        icon = "🔴"
        title = "SERN Error 轉發"

    lines = [
        f"{icon} **{title}**",
        f"使用者: `{_truncate(record.get('user_id')) or 'N/A'}`",
        f"來源頻道: `{_truncate(record.get('channel_id')) or 'N/A'}`",
        f"指令: `{_truncate(record.get('command')) or 'N/A'}`",
        f"時間: `{_truncate(record.get('call_time')) or 'N/A'}`",
    ]

    if email_sent is not None:
        lines.append(f"Email: `{'sent' if email_sent else 'failed'}`")
    if email_message_id:
        lines.append(f"Email messageId: `{_truncate(email_message_id, 120)}`")
    if error:
        lines.append(f"錯誤: `{_truncate(error, 180)}`")

    question = _truncate(record.get("question"), 220)
    summary = _truncate(record.get("summary"), 220)
    if question:
        lines.append(f"問題: `{question}`")
    if summary:
        lines.append(f"摘要: `{summary}`")

    message = "\n".join(lines)
    try:
        await channel.send(message[:2000])
        return True
    except Exception as exc:
        logger.error("notify forward: 發送失敗: %s", exc, exc_info=True)
        return False
