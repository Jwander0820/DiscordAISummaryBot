from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..db.repository import summary_repository
from ..features.chat.history import collect_non_bot_messages, format_message_history, truncate_for_discord
from ..features.chat.records import build_summary_record
from ..features.notifications.service import notification_service
from ..features.summaries.service import call_cloud_llm
from ..integrations.gemini_client import gemini_model
from ..integrations.local_llm import query_local_llm

logger = logging.getLogger("discord_digest_bot")


def _require_text_channel(interaction: discord.Interaction) -> discord.TextChannel | None:
    """Return a text channel when the interaction depends on chat history."""
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        return channel
    return None

class ConversationCog(commands.Cog):
    """Slash commands that answer questions based on recent conversation context."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="你要不要聽聽看你現在在講什麼",
        description="取得24小時內最近1000則訊息，根據你問的問題回覆(實驗性)",
    )
    async def ask_about_conversation(self, interaction: discord.Interaction, 想問些什麼: str, len_msg: int = 1000) -> None:
        """Answer a user's question by analyzing recent channel history with Gemini."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        question = 想問些什麼
        record = {}
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=1)
            messages = await collect_non_bot_messages(channel, limit=len_msg, after=time_since, fetch_multiplier=1.1)
            if not messages:
                await interaction.followup.send("找不到最近的訊息，無法回答問題。")
                return

            message_text = format_message_history(messages, include_author_id=True)
            contents = [
                {
                    "role": "model",
                    "parts": ["你是 Discord 頻道的觀察者，會用詼諧風格根據歷史訊息回答問題。請用繁體中文簡潔地作答。"],
                },
                {
                    "role": "user",
                    "parts": [
                        f"""以下是過去 24 小時最近的 {len(messages)} 則對話：

                        {message_text}

                        使用者的提問：
                        {question}

                        請回答："""
                    ],
                },
            ]

            logger.info("Sending user question prompt to Gemini (length: %s chars)", len(contents[1]["parts"][0]))
            if not gemini_model:
                await interaction.followup.send("Summarization feature is unavailable (missing API key).", ephemeral=True)
                return

            response = await gemini_model.generate_content_async(contents=contents)
            if not response.parts:
                await interaction.followup.send("AI 無法提供回應（可能被內容審核攔截）。")
                return

            answer = truncate_for_discord(response.text.strip())
            reply_content = f"{interaction.user.mention} 問了：{question}\n\n{answer}"

            record = build_summary_record(
                channel_id=str(channel.name),
                user_id=str(interaction.user.global_name or interaction.user.name),
                command="你要不要聽聽看你現在在講什麼",
                question=question,
                prompt=message_text,
                summary=answer,
            )
            summary_repository.insert_summary(record)

            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
            )
            await interaction.followup.send(reply_content)
        except Exception as exc:
            logger.error("Error in ask_about_conversation: %s", exc, exc_info=True)
            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
                error=exc,
            )
            await interaction.followup.send(f"發生錯誤：{exc}", ephemeral=True)

    @app_commands.command(name="解答之書", description="取樣最近20則訊息，向本地 LLM 詢問")
    async def answer_book(self, interaction: discord.Interaction, 問題: str) -> None:
        """Answer with either the local LLM or cloud model using the latest 20 messages as context."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        role_mode = os.getenv("ROLE_MODE", "local")
        record = {}
        try:
            messages = await collect_non_bot_messages(channel, limit=20, after=None, fetch_multiplier=2.5)
            if not messages:
                await interaction.followup.send("找不到最近的訊息。")
                return

            history = format_message_history(messages)
            prompt = (
                f"以下是此頻道最近的 20 則對話：\n{history}\n\n"
                f"使用者問題：{問題}\n可以根據對話內容與使用者聊天，並以繁體中文回答。"
            )
            logger.info("Sending prompt to %s LLM (length: %s chars)", role_mode, len(prompt))

            answer = await query_local_llm(prompt, role="basic") if role_mode == "local" else await call_cloud_llm(prompt, role="basic")
            answer = truncate_for_discord(answer)
            reply = f"{interaction.user.mention} 問了：{問題}\n\n{answer}"

            record = build_summary_record(
                channel_id=str(channel.name),
                user_id=str(interaction.user.global_name or interaction.user.name),
                command="解答之書",
                question=問題,
                prompt=prompt,
                summary=answer,
            )
            summary_repository.insert_summary(record)

            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
            )

            if answer.startswith("Error contacting local LLM"):
                await interaction.followup.send("AI 罷工了捏 _(:з」∠)_", ephemeral=True)
                return

            await interaction.followup.send(reply)
        except Exception as exc:
            logger.error("Error in answer_book: %s", exc, exc_info=True)
            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
                error=exc,
            )
            await interaction.followup.send(f"發生錯誤：{exc}", ephemeral=True)

    @app_commands.command(name="el_psy_kongroo", description="一切都是命運石之門的選擇！")
    async def el_psy_kongroo(self, interaction: discord.Interaction, 問題: str) -> None:
        """Role-flavored variant of answer_book that replies as Amadeus."""
        channel = _require_text_channel(interaction)
        if channel is None:
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        role_mode = os.getenv("ROLE_MODE", "local")
        record = {}
        try:
            messages = await collect_non_bot_messages(channel, limit=20, after=None, fetch_multiplier=2.5)
            if not messages:
                await interaction.followup.send("找不到最近的訊息。")
                return

            history = format_message_history(messages)
            prompt = (
                f"以下是此頻道最近的 20 則對話：\n{history}\n\n"
                f"使用者問題：{問題}\n可以根據對話內容與使用者聊天，並以繁體中文回答，可適度帶入命運石之門風格語感。"
            )
            logger.info("Sending prompt to %s LLM (length: %s chars)", role_mode, len(prompt))

            answer = await query_local_llm(prompt, role="kurisu") if role_mode == "local" else await call_cloud_llm(prompt, role="kurisu")
            answer = truncate_for_discord(answer)
            reply = f"{interaction.user.mention} 問了：{問題}\n\n{answer}"

            record = build_summary_record(
                channel_id=str(channel.name),
                user_id=str(interaction.user.global_name or interaction.user.name),
                command="el_psy_kongroo",
                question=問題,
                prompt=prompt,
                summary=answer,
            )
            summary_repository.insert_summary(record)

            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
            )

            if answer.startswith("Error contacting local LLM"):
                await interaction.followup.send("Amadeus 罷工了捏 _(:з」∠)_", ephemeral=True)
                return

            await self._send_as_amadeus(channel, reply)
            await interaction.delete_original_response()
        except Exception as exc:
            logger.error("Error in el_psy_kongroo: %s", exc, exc_info=True)
            await notification_service.dispatch(
                record=record,
                guild=interaction.guild,
                bot_client=interaction.client,
                error=exc,
            )
            await interaction.followup.send(f"發生錯誤：{exc}", ephemeral=True)

    async def _send_as_amadeus(self, channel: discord.TextChannel, content: str) -> None:
        """Send a webhook message that impersonates the Amadeus persona."""
        avatar_url = (
            "https://media.discordapp.net/attachments/1409948881822814450/1413559087609938031/Amadeus.png"
            "?ex=68bc5efd&is=68bb0d7d&hm=a9069c5e97de585f0d34479acee579394a52d69bc81445ff3a1fb7c5571e00fa"
            "&=&format=webp&quality=lossless&width=1084&height=1084"
        )
        await self._send_with_webhook(channel, content, "Amadeus", avatar_url, "Amadeus")

    async def _send_with_webhook(
        self,
        channel: discord.TextChannel,
        content: str,
        username: str,
        avatar_url: str,
        webhook_name: str,
    ) -> None:
        """Send a message through a named webhook, creating it on demand."""
        webhooks = await channel.webhooks()
        webhook = discord.utils.get(webhooks, name=webhook_name)
        if webhook is None:
            webhook = await channel.create_webhook(name=webhook_name)
        await webhook.send(content, username=username, avatar_url=avatar_url)


async def setup(bot: commands.Bot) -> None:
    """Register the conversation cog."""
    await bot.add_cog(ConversationCog(bot))
