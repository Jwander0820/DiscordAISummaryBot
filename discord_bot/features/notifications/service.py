from __future__ import annotations

import logging
import os
from typing import Optional

import discord

from ...integrations.gmail_gateway import (
    gmail_notify_enabled,
    send_deepfaker_notify,
    send_error_notify,
    send_sarn_notify,
)
from .discord_forwarder import forward_notify_to_channel

logger = logging.getLogger("discord_digest_bot")


class NotificationService:
    async def dispatch(
        self,
        *,
        record: dict,
        guild: Optional[discord.Guild] = None,
        bot_client: Optional[discord.Client] = None,
        error: Optional[Exception] = None,
        deepfaker_subject: Optional[str] = None,
    ) -> None:
        """通知協調入口。

        先決定是否寄 Gmail，再把結果轉發到 Discord notify channel。
        這樣上層 command/service 不需要知道通知細節。
        """
        safe_record = dict(record or {})
        for field in ("command", "channel_id", "user_id"):
            if not safe_record.get(field):
                safe_record[field] = "unknown"

        email_sent = None
        message_id = None
        notify_type = "error" if error else "success"
        gmail_send_to = os.getenv("GMAIL_SEND_TO")

        try:
            if gmail_notify_enabled() and gmail_send_to:
                if error:
                    message_id = send_error_notify(error, safe_record, gmail_send_to)
                elif deepfaker_subject:
                    message_id = send_deepfaker_notify(safe_record, gmail_send_to, deepfaker_subject)
                else:
                    message_id = send_sarn_notify(safe_record, gmail_send_to)
                email_sent = True
            elif not gmail_notify_enabled():
                logger.info("Gmail notify disabled by GMAIL_NOTIFY_ENABLED; skipped command email.")
        except Exception as notify_err:
            email_sent = False
            logger.error("發信失敗：%s", notify_err, exc_info=True)

        await forward_notify_to_channel(
            record=safe_record,
            guild=guild,
            bot=bot_client,
            notify_type=notify_type,
            email_sent=email_sent,
            email_message_id=message_id,
            error=error,
        )


notification_service = NotificationService()
