from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..features.chat.history import collect_non_bot_messages, truncate_for_discord
from ..features.summaries.service import summarize_messages

logger = logging.getLogger("discord_digest_bot")


def _require_text_channel(interaction: discord.Interaction) -> discord.TextChannel | None:
    """Return a text channel when the interaction target is valid for history-based commands."""
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


class SummaryCog(commands.Cog):
    """Slash commands related to channel summarization."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="聊那麼多誰看的完", description="總結頻道中的24小時內5000則訊息")
    async def summarize(self, interaction: discord.Interaction, len_msg: int = 5000) -> None:
        """Summarize up to the last 24 hours of non-bot messages."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=1)
            messages = await collect_non_bot_messages(
                channel,
                limit=len_msg,
                after=time_since,
                fetch_multiplier=1.1,
            )
            logger.info("Fetched %s non-bot messages for summarization.", len(messages))

            user_id = str(interaction.user.display_name or interaction.user.name)
            summary_text = await summarize_messages(messages, user_id=user_id)
            if not summary_text:
                summary_text = "Could not generate a summary (empty response)."

            await interaction.followup.send(truncate_for_discord(summary_text, suffix="... (summary truncated)"))
            logger.info("Sent summary to channel '%s'", channel.name)
        except discord.Forbidden:
            logger.error(
                "Permission error: Bot lacks permissions to read history or send messages in channel '%s' (ID: %s)",
                channel.name,
                channel.id,
            )
            await interaction.followup.send(
                "Error: I don't have the necessary permissions to read message history or send messages in this channel.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error("An unexpected error occurred during summarization command: %s", exc, exc_info=True)
            await interaction.followup.send(f"An unexpected error occurred: {exc}", ephemeral=True)

    @app_commands.command(name="整理廢話的魔法", description="總結頻道中的1小時內所有訊息")
    async def magic_summarize(self, interaction: discord.Interaction, len_msg: int = 5000) -> None:
        """Summarize recent discussion from the last hour."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(hours=1)
            logger.info("Fetching messages from channel '%s' since %s", channel.name, time_since.isoformat())
            messages = await collect_non_bot_messages(channel, limit=len_msg, after=time_since)

            logger.info("Fetched %s non-bot messages in last hour.", len(messages))
            user_id = str(interaction.user.display_name or interaction.user.name)
            summary_text = await summarize_messages(messages, prompt_scope="過去一小時", user_id=user_id)
            if not summary_text:
                summary_text = "找不到有效內容，無法生成摘要。"

            await interaction.followup.send(f"＜(´⌯  ̫⌯`)＞ {truncate_for_discord(summary_text)}")
            logger.info("Sent 1h summary to channel '%s'", channel.name)
        except discord.Forbidden:
            logger.error(
                "Permission error: Bot lacks permissions to read history or send messages in channel '%s' (ID: %s)",
                channel.name,
                channel.id,
            )
            await interaction.followup.send(
                "Error: I don't have the necessary permissions to read message history or send messages in this channel.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error("An unexpected error occurred during summarization command: %s", exc, exc_info=True)
            await interaction.followup.send(f"An unexpected error occurred: {exc}", ephemeral=True)

    @app_commands.command(name="命運探知之魔眼", description="總結頻道中七天內一萬則訊息的精華(實驗性)")
    async def deep_summary(self, interaction: discord.Interaction, len_msg: int = 10000) -> None:
        """Summarize a longer seven-day window for high-volume channels."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=7)
            logger.info(
                "Fetching messages (7d, max %s) from channel '%s' (ID: %s) since %s",
                len_msg,
                channel.name,
                channel.id,
                time_since,
            )
            messages = await collect_non_bot_messages(
                channel,
                limit=len_msg,
                after=time_since,
                fetch_multiplier=1.1,
            )
            logger.info("Fetched %s non-bot messages for 7-day summary.", len(messages))

            user_id = str(interaction.user.display_name or interaction.user.name)
            summary_text = await summarize_messages(messages, prompt_scope="過去七天", user_id=user_id)
            if not summary_text:
                summary_text = "總結失敗，無法取得任何有效的訊息。"

            await interaction.followup.send(truncate_for_discord(summary_text, suffix="... (summary truncated)"))
            logger.info("Sent 7-day summary to channel '%s'", channel.name)
        except discord.Forbidden:
            logger.error("Permission error: No access to read or send in channel '%s' (ID: %s)", channel.name, channel.id)
            await interaction.followup.send("權限錯誤：無法讀取或發送訊息至此頻道。", ephemeral=True)
        except Exception as exc:
            logger.error("Unexpected error in /命運探知之魔眼: %s", exc, exc_info=True)
            await interaction.followup.send(f"發生錯誤：{exc}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register the summary cog."""
    await bot.add_cog(SummaryCog(bot))
