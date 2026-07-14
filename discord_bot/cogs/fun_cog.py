from __future__ import annotations

import logging
import os
import random

import discord
from discord import app_commands
from discord.ext import commands

from ..db.deepfaker_repository import deepfaker_repository
from ..db.repository import summary_repository
from ..features.chat.records import build_summary_record
from ..features.deepfaker.records import build_deepfaker_event
from ..features.notifications.service import notification_service

logger = logging.getLogger("discord_digest_bot")

DEEPFAKER_FAILURE_NOTICE = os.getenv("DEEPFAKER_FAILURE_NOTICE", "抓到你了！炸彈魔！")
DEEPFAKER_FAILURE_PROB = os.getenv("DEEPFAKER_FAILURE_PROB", 0.05)


def _require_text_channel(interaction: discord.Interaction) -> discord.TextChannel | None:
    """Return a text channel when the command needs webhook or history access."""
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


class FunCog(commands.Cog):
    """Fun or utility slash commands with side effects."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="測試d-mail", description="測試機器人連線狀態，應該不會改變世界線，應該啦...")
    async def send_test_dmail(self, interaction: discord.Interaction) -> None:
        """Run the worldline randomizer and optionally send notifications when it 'crosses'."""
        default_prob = float(os.getenv("WORLDLINE_PROB_DEFAULT", "0.01048596"))
        admin_prob = float(os.getenv("WORLDLINE_PROB_ADMIN", "0.1"))
        admin_ids_raw = os.getenv("WORLDLINE_ADMIN_IDS", "")
        admin_ids = {int(uid.strip()) for uid in admin_ids_raw.split(",") if uid.strip().isdigit()}

        is_admin = interaction.user.id in admin_ids
        threshold = 1 - (admin_prob if is_admin else default_prob)
        worldline = round(random.uniform(0.000001, 1.000000), 6)
        crossed = worldline > threshold
        cross_worldline_text = f"世界線變動率探測儀檢定值: {worldline}"

        if crossed:
            reply_content = (
                "⚠️ 世界線變動偵測！抵達新的世界線座標：1.048596！\n"
                "一切都是命運石之門的選擇。\n"
            )
        else:
            reply_content = f"D-Mail 送達，世界線沒有改變，目前座標為：{worldline}"

        await interaction.response.send_message(reply_content, ephemeral=False)
        logger.info(
            "Test D-Mail sent by %s in channel '%s' → 世界線座標:%s, admin: %s",
            interaction.user.global_name or interaction.user.name,
            interaction.channel.name if interaction.channel else "unknown",
            worldline,
            is_admin,
        )

        record = build_summary_record(
            channel_id=str(interaction.channel.name if interaction.channel else ""),
            user_id=str(interaction.user.global_name or interaction.user.name),
            command="測試d-mail",
            prompt=cross_worldline_text,
            summary=reply_content,
        )
        summary_repository.insert_summary(record)

        if crossed:
            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
            )

    @app_commands.command(name="deepfaker", description="DeepFaker 偽裝成指定用戶發送訊息，有一定機率會爆炸")
    async def deepfaker(self, interaction: discord.Interaction, 冒牌對象: discord.Member, 內容: str) -> None:
        """Post a webhook message as another member, with a configurable failure chance."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        trimmed_content = 內容.strip()
        if not trimmed_content:
            await interaction.response.send_message("請提供要偽裝發送的內容。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        failure_probability = float(DEEPFAKER_FAILURE_PROB)
        random_roll = random.random()
        should_fail = random_roll < failure_probability
        parts = [segment.strip() for segment in str(DEEPFAKER_FAILURE_NOTICE).split("|") if segment.strip()]
        failure_text = random.choice(parts) if parts else "抓到你了！炸彈魔！"
        fake_message_content = ""

        if should_fail:
            username = interaction.user.display_name or interaction.user.name
            avatar_url = interaction.user.display_avatar.url
            message_content = trimmed_content
            if failure_text:
                reserve_length = len(failure_text) + 1
                fake_username = 冒牌對象.display_name or 冒牌對象.name
                fake_message_content = f"↖️ 這個人想偽裝成 {fake_username} 說 「{message_content}」"
                if len(fake_message_content) + reserve_length > 2000:
                    allowed = 2000 - reserve_length
                    fake_message_content = fake_message_content[: max(allowed, 0)]
                message_content = f"{fake_message_content}\n{failure_text}".strip()
            result_message = f"DeepFaker 轉換失敗，已使用你的身份發送訊息。 ({failure_text or '無額外說明'})"
            success = False
            target_for_log = interaction.user.display_name or interaction.user.name
            logger.info("偽裝失敗log紀錄 %s%s", username, fake_message_content or message_content)
        else:
            username = 冒牌對象.display_name or 冒牌對象.name
            avatar_url = 冒牌對象.display_avatar.url
            message_content = trimmed_content[:2000]
            result_message = f"已偽裝成 {username} 發送訊息。"
            success = True
            target_for_log = username

        def record_deepfaker_event(delivery_status: str) -> None:
            event = build_deepfaker_event(
                guild=channel.guild,
                channel=channel,
                actor=interaction.user,
                target=冒牌對象,
                outcome_success=success,
                failure_probability=failure_probability,
                random_roll=random_roll,
                requested_content=trimmed_content,
                webhook_content=message_content,
                failure_notice=failure_text if should_fail else None,
                failure_exposed_content=fake_message_content or None,
                delivery_status=delivery_status,
            )
            deepfaker_repository.insert_event(event)

        try:
            await self._send_with_webhook(channel, message_content, username, avatar_url, "DeepFaker")
        except discord.Forbidden:
            record_deepfaker_event("forbidden")
            logger.error("DeepFaker 需要 Manage Webhooks 權限才能發送訊息。")
            await interaction.followup.send("無法使用 DeepFaker，缺少建立或使用 Webhook 的權限。", ephemeral=True)
            return
        except discord.HTTPException as http_err:
            record_deepfaker_event("http_error")
            logger.error("DeepFaker webhook 發送失敗: %s", http_err, exc_info=True)
            await interaction.followup.send("DeepFaker 發送失敗，請稍後再試一次。", ephemeral=True)
            return

        record_deepfaker_event("sent")

        log_message = (
            f"DeepFaker invoked by {interaction.user.display_name or interaction.user.name} "
            f"偽裝成 {target_for_log} (status={success})"
        )
        logger.info(log_message)

        record = build_summary_record(
            channel_id=str(channel.name),
            user_id=str(interaction.user.display_name or interaction.user.name),
            command="deepfaker",
            question=log_message,
            prompt=message_content,
            summary=fake_message_content,
        )
        summary_repository.insert_summary(record)

        fake_username = 冒牌對象.display_name or 冒牌對象.name
        tag = "✅SUCCESS" if not should_fail else "❌FAIL"
        user_id = str(interaction.user.display_name or interaction.user.name)
        subject = f"【SERN Notify】{user_id} 在 {channel.name} 使用了 deepfaker 偽裝成 {fake_username} {tag}！"

        await notification_service.dispatch(
            record=record,
            guild=interaction.guild,
            bot_client=interaction.client,
            deepfaker_subject=subject,
        )
        await interaction.followup.send(result_message, ephemeral=True)

    async def _send_with_webhook(
        self,
        channel: discord.TextChannel,
        content: str,
        username: str,
        avatar_url: str,
        webhook_name: str,
    ) -> None:
        """Send content through a reusable webhook for impersonation features."""
        webhooks = await channel.webhooks()
        webhook = discord.utils.get(webhooks, name=webhook_name)
        if webhook is None:
            webhook = await channel.create_webhook(name=webhook_name)
        await webhook.send(content, username=username, avatar_url=avatar_url)


async def setup(bot: commands.Bot) -> None:
    """Register the fun cog."""
    await bot.add_cog(FunCog(bot))
