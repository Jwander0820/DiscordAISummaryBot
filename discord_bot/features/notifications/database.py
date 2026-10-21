"""Deliver bounded, sanitized database health events to the existing botlog channel."""
from __future__ import annotations

import asyncio
import logging

import discord

from ...db.health import DatabaseEvent, DatabaseHealth, database_health
from .discord_forwarder import _resolve_int_env

logger = logging.getLogger("discord_digest_bot")
NOTIFICATION_TIMEOUT_SECONDS = 5


def format_database_event(event: DatabaseEvent) -> str:
    labels = {"summaries": "summaries", "deepfaker_events": "deepfaker_events",
              "guild_social_preview_settings": "guild 社群設定", "social_share_events": "社群分享歷史／出警設定"}
    repository = labels.get(event.repository, "DB")
    backend = event.backend if event.backend in {"sqlite", "postgres"} else "unknown"
    operation = {"initialize": "初始化／重連", "read": "讀取", "write": "寫入"}.get(event.operation, "操作")
    status = "🟢 DB 已恢復" if event.recovered else "🔴 DB 操作失敗"
    lines = [f"**{status}**", f"資料表: `{repository}`｜類型: `{backend}`｜階段: `{operation}`",
             f"時間: `{event.occurred_at}`"]
    if event.recovered:
        lines.append("連線／操作已恢復；先前失敗或跳過的紀錄不會自動補寫，送達結果不確定的寫入也不會重播。")
    else:
        error = event.error_type + (f" / SQLSTATE {event.sqlstate}" if event.sqlstate else "")
        lines.append(f"錯誤類型: `{error}`（詳細 traceback 請看 Server log）")
        if event.repository == "social_share_events":
            lines.append("出警已暫停，背景每 30 秒嘗試重新載入 DB 與快取；正常時不輪詢。")
        else:
            lines.append("連線不可用：冷卻 30 秒後，等下一次 DB 需求再重連。" if event.unavailable
                         else "本次操作失敗，已 rollback；下一次寫入仍會使用現有連線。")
        lines.append("失敗紀錄不自動補寫；持續故障最多每 5 分鐘提醒一次。")
        lines.append("若持續失敗，先檢查 DB 服務／網路或儲存掛載與權限，再判斷是否需重啟容器。")
    return "\n".join(lines)


async def send_database_event(bot, event: DatabaseEvent) -> None:
    channel_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_CHANNEL_ID")
    if channel_id is None:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    guild_id = _resolve_int_env("DISCORD_NOTIFY_FORWARD_GUILD_ID")
    if guild_id is not None and getattr(getattr(channel, "guild", None), "id", None) != guild_id:
        logger.warning("DB notification channel does not match configured guild; skipped.")
        return
    await channel.send(format_database_event(event), allowed_mentions=discord.AllowedMentions.none())


async def run_database_notifications(bot, *, health: DatabaseHealth = database_health) -> None:
    """One event consumer, not a DB polling loop; startup events wait for Discord ready."""
    while True:
        event = await health.events.get()
        try:
            await bot.wait_until_ready()
            await asyncio.wait_for(send_database_event(bot, event), timeout=NOTIFICATION_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not feed notification failures back into DB events, or log the raw payload.
            logger.warning("DB botlog notification failed: error_type=%s", type(exc).__name__)
        finally:
            health.events.task_done()
