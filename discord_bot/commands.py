from datetime import datetime, timezone, timedelta
import logging
import discord
from discord.ext import commands

from .summarizer import summarize_messages
from .database import insert_summary

logger = logging.getLogger('discord_digest_bot')


def register(bot: commands.Bot):
    @bot.tree.command(name="聊那麼多誰看的完", description="總結頻道中的24小時內2000則訊息")
    async def summarize(interaction: discord.Interaction, len_msg: int = 2000):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=1)
            messages = []
            async for message in channel.history(limit=int(len_msg*1.1), after=time_since, oldest_first=False):
                if not message.author.bot:
                    messages.append(message)
                if len(messages) >= len_msg:
                    logger.info("Reached message limit (2000) for summarization.")
                    break

            logger.info(f"Fetched {len(messages)} non-bot messages for summarization.")
            summary_text = await summarize_messages(messages)
            if len(summary_text) > 1900:
                logger.warning(f"Summary length ({len(summary_text)}) exceeds Discord limit. Truncating.")
                summary_text = summary_text[:1900] + "... (summary truncated)"
            elif not summary_text:
                summary_text = "Could not generate a summary (empty response)."

            await interaction.followup.send(f"{summary_text}")
            logger.info(f"Sent summary to channel '{channel.name}'")
        except discord.Forbidden:
            logger.error(
                f"Permission error: Bot lacks permissions to read history or send messages in channel '{channel.name}' (ID: {channel.id})")
            await interaction.followup.send(
                "Error: I don't have the necessary permissions to read message history or send messages in this channel.",
                ephemeral=True)
        except Exception as e:
            logger.error(f"An unexpected error occurred during summarization command: {e}", exc_info=True)
            await interaction.followup.send(f"An unexpected error occurred: {e}", ephemeral=True)

    @bot.tree.command(name="整理廢話的魔法", description="總結頻道中的1小時內所有訊息")
    async def magic_summarize(interaction: discord.Interaction, len_msg: int = 5000):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(hours=1)
            logger.info(f"Fetching messages from channel '{channel.name}' since {time_since.isoformat()}")
            messages = []
            async for message in channel.history(limit=len_msg, after=time_since, oldest_first=False):
                if not message.author.bot:
                    messages.append(message)

            logger.info(f"Fetched {len(messages)} non-bot messages in last hour.")
            summary_text = await summarize_messages(messages, prompt_scope="過去一小時")
            if len(summary_text) > 1900:
                logger.warning(f"Summary length ({len(summary_text)}) exceeds Discord limit. Truncating.")
                summary_text = summary_text[:1900] + "...（已截斷）"
            elif not summary_text:
                summary_text = "找不到有效內容，無法生成摘要。"

            await interaction.followup.send(f"＜(´⌯  ̫⌯`)＞ {summary_text}")
            logger.info(f"Sent 1h summary to channel '{channel.name}'")
        except discord.Forbidden:
            logger.error(
                f"Permission error: Bot lacks permissions to read history or send messages in channel '{channel.name}' (ID: {channel.id})")
            await interaction.followup.send(
                "Error: I don't have the necessary permissions to read message history or send messages in this channel.",
                ephemeral=True)
        except Exception as e:
            logger.error(f"An unexpected error occurred during summarization command: {e}", exc_info=True)
            await interaction.followup.send(f"An unexpected error occurred: {e}", ephemeral=True)

    @bot.tree.command(name="命運探知之魔眼", description="總結頻道中七天內一萬則訊息的精華(實驗性)")
    async def deep_summary(interaction: discord.Interaction, len_msg: int = 10000):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=7)
            logger.info(
                f"Fetching messages (7d, max 10000) from channel '{channel.name}' (ID: {channel.id}) since {time_since}")
            messages = []
            async for message in channel.history(limit=int(len_msg*1.1), after=time_since, oldest_first=False):
                if not message.author.bot:
                    messages.append(message)
                if len(messages) >= len_msg:
                    logger.info("Reached message limit (10000) for deep summary.")
                    break

            logger.info(f"Fetched {len(messages)} non-bot messages for 7-day summary.")
            summary_text = await summarize_messages(messages, prompt_scope="過去七天")
            if len(summary_text) > 1900:
                logger.warning(f"Summary length ({len(summary_text)}) exceeds Discord limit. Truncating.")
                summary_text = summary_text[:1900] + "... (summary truncated)"
            elif not summary_text:
                summary_text = "總結失敗，無法取得任何有效的訊息。"

            await interaction.followup.send(f"{summary_text}")
            logger.info(f"Sent 7-day summary to channel '{channel.name}'")
        except discord.Forbidden:
            logger.error(
                f"Permission error: No access to read or send in channel '{channel.name}' (ID: {channel.id})")
            await interaction.followup.send(
                "權限錯誤：無法讀取或發送訊息至此頻道。", ephemeral=True)
        except Exception as e:
            logger.error(f"Unexpected error in /命運探知之魔眼: {e}", exc_info=True)
            await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)

    @bot.tree.command(name="你要不要聽聽看你現在在講什麼", description="取得24小時內最近1000則訊息，根據你問的問題回覆(實驗性)")
    async def ask_about_conversation(interaction: discord.Interaction, 想問些什麼: str, len_msg: int = 1000):
        question = 想問些什麼
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        TZ_8 = timezone(timedelta(hours=8))
        try:
            time_since = datetime.now(timezone.utc) - timedelta(days=1)
            messages = []
            async for message in channel.history(limit=int(len_msg*1.1), after=time_since, oldest_first=False):
                if not message.author.bot:
                    messages.append(message)
                if len(messages) >= len_msg:
                    break

            if not messages:
                await interaction.followup.send("找不到最近的訊息，無法回答問題。")
                return

            logger.info(f"Fetched {len(messages)} messages for user question analysis.")
            message_text = "\n".join([
                f"[{msg.created_at.astimezone(TZ_8).strftime('%H:%M')}] [id:{msg.author.name}] {msg.author.display_name}: {msg.content}"
                for msg in reversed(messages)
            ])

            prompt = f"""你是 Discord 頻道中的觀察者，以下是24小時內最近的 1000 則對話紀錄，請根據這些內容回答使用者的問題。

聊天紀錄:
{message_text}

使用者的提問：
{question}

請用繁體中文回答，風格可以幽默，但務必根據對話內容作答。
回答：
"""
            logger.info(f"Sending user question prompt to Gemini (length: {len(prompt)} chars)")
            from .gemini_client import gemini_model
            if not gemini_model:
                logger.warning("Gemini model not initialized or API key missing."
                               )
                await interaction.followup.send(
                    "Summarization feature is unavailable (missing API key).",
                    ephemeral=True,
                )
                return

            response = await gemini_model.generate_content_async(prompt)
            if not response.parts:
                await interaction.followup.send("AI 無法提供回應（可能被內容審核攔截）。")
                return

            answer = response.text.strip()
            if len(answer) > 1900:
                answer = answer[:1900] + "...（回應過長，已截斷）"

            asker = interaction.user.mention
            reply_content = (
                f"{asker} 問了：{question}\n\n"
                f"{answer}"
            )
            tz = timezone(timedelta(hours=8))
            call_time = datetime.now(tz).isoformat()
            record = {
                "channel_id": str(channel.name),
                "user_id": str(interaction.user.global_name),
                "command": "你要不要聽聽看你現在在講什麼",
                "question": question,
                "prompt": message_text,
                "summary": answer,
                "call_time": call_time,
            }
            insert_summary(record)
            await interaction.followup.send(reply_content)
        except Exception as e:
            logger.error(f"Error in ask_about_conversation: {e}", exc_info=True)
            await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)

