from datetime import datetime, timezone, timedelta
import logging
import discord
from discord.ext import commands
import random
import os

import json

from discord.ext.commands import NoEntryPointError
from google.protobuf.internal.message_listener import NullMessageListener

from .summarizer import summarize_messages
from .summarizer import call_cloud_llm
from .database import insert_summary
from .local_llm_client import query_local_llm
from .gemini_client import gemini_model
from .gemini_client import role_model
from .gmail_utils import send_sarn_notify, send_error_notify, send_deepfaker_notify
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger('discord_digest_bot')
GMAIL_SEND_TO = os.getenv("GMAIL_SEND_TO")
DEEPFAKER_FAILURE_NOTICE = os.getenv("DEEPFAKER_FAILURE_NOTICE", "抓到你了！炸彈魔！")
DEEPFAKER_FAILURE_PROB = os.getenv("DEEPFAKER_FAILURE_PROB", 0.05)


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
            async for message in channel.history(limit=int(len_msg * 1.1), after=time_since, oldest_first=False):
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
            async for message in channel.history(limit=int(len_msg * 1.1), after=time_since, oldest_first=False):
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

    @bot.tree.command(name="你要不要聽聽看你現在在講什麼",
                      description="取得24小時內最近1000則訊息，根據你問的問題回覆(實驗性)")
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
            async for message in channel.history(limit=int(len_msg * 1.1), after=time_since, oldest_first=False):
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

            contents = [
                {
                    "role": "model",
                    "parts": ["你是 Discord 頻道的觀察者，會用詼諧風格根據歷史訊息回答問題。請用繁體中文簡潔地作答。"]
                },
                {
                    "role": "user",
                    "parts": [
                        f"""以下是過去 24 小時最近的 {len(messages)} 則對話：

                        {message_text}
            
                        使用者的提問：
                        {question}
            
                        請回答："""]
                }
            ]
            logger.info(f"Sending user question prompt to Gemini (length: {len(contents[1]['parts'][0])} chars)")
            if not gemini_model:
                logger.warning("Gemini model not initialized or API key missing."
                               )
                await interaction.followup.send(
                    "Summarization feature is unavailable (missing API key).",
                    ephemeral=True,
                )
                return

            response = await gemini_model.generate_content_async(contents=contents)
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

            # 發信通知
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_sarn_notify(record, GMAIL_SEND_TO)
                    logger.info(f"SERN Notify sent, messageId={msg_id}")
                except Exception as e:
                    logger.error(f"發信失敗：{e}")

            await interaction.followup.send(reply_content)
        except Exception as e:
            logger.error(f"Error in ask_about_conversation: {e}", exc_info=True)
            # 發錯誤通知信
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_error_notify(e, record, GMAIL_SEND_TO)
                    logger.info(f"Error notify sent, messageId={msg_id}")
                except Exception as mail_err:
                    logger.error(f"無法發送錯誤電子郵件: {mail_err}", exc_info=True)
            await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)

    @bot.tree.command(name="測試d-mail", description="測試機器人連線狀態，應該不會改變世界線，應該啦...")
    async def send_test_dmail(interaction: discord.Interaction):
        """
        機器人測試，世界線檢定指令，支援 .env 特權設定。
        """
        # 從 .env 載入設定
        default_prob = float(os.getenv("WORLDLINE_PROB_DEFAULT", "0.01048596"))
        admin_prob = float(os.getenv("WORLDLINE_PROB_ADMIN", "0.1"))
        admin_ids_raw = os.getenv("WORLDLINE_ADMIN_IDS", "")
        admin_ids = set(int(uid.strip()) for uid in admin_ids_raw.split(",") if uid.strip().isdigit())

        # 檢查是否為特權使用者
        is_admin = interaction.user.id in admin_ids
        threshold = 1 - (admin_prob if is_admin else default_prob)

        # 世界線變動率探測儀隨機檢定
        worldline = round(random.uniform(0.000001, 1.000000), 6)
        crossed = worldline > threshold
        cross_worldline_text = f"世界線變動率探測儀檢定值: {worldline}"

        # 訊息內容
        if crossed:
            reply_content = (
                f"⚠️ 世界線變動偵測！抵達新的世界線座標：1.048596！\n"
                f"一切都是命運石之門的選擇。\n"
            )
        else:
            reply_content = (f"D-Mail 送達，世界線沒有改變，目前座標為：{worldline}")

        await interaction.response.send_message(reply_content, ephemeral=False)

        logger.info(
            f"Test D-Mail sent by {interaction.user.global_name} in channel '{interaction.channel.name}' → 世界線座標:{worldline}, admin: {is_admin}"
        )

        tz = timezone(timedelta(hours=8))
        call_time = datetime.now(tz).isoformat()
        record = {
            "channel_id": str(interaction.channel.name),
            "user_id": str(interaction.user.global_name),
            "command": "測試d-mail",
            "question": "",
            "prompt": cross_worldline_text,
            "summary": reply_content,
            "call_time": call_time,
        }
        insert_summary(record)

        # 發信通知(若跨越世界線)
        if crossed:
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_sarn_notify(record, GMAIL_SEND_TO)
                    logger.info(f"SERN Notify sent, messageId={msg_id}")
                except Exception as e:
                    logger.error(f"發信失敗：{e}")

    @bot.tree.command(name="解答之書", description="取樣最近20則訊息，向本地 LLM 詢問")
    async def answer_book(interaction: discord.Interaction, 問題: str):
        role_mode = os.getenv("ROLE_MODE", "local")
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        TZ_8 = timezone(timedelta(hours=8))
        try:
            messages = []
            async for msg in channel.history(limit=50, oldest_first=False):
                messages.append(msg)
                if len(messages) >= 20:
                    break

            if not messages:
                await interaction.followup.send("找不到最近的訊息。")
                return

            history = "\n".join([
                f"[{m.created_at.astimezone(TZ_8).strftime('%H:%M')}] {m.author.display_name}: {m.content}"
                for m in reversed(messages)
            ])

            prompt = f"""以下是此頻道最近的 20 則對話：\n{history}\n\n使用者問題：{問題}\n可以根據對話內容與使用者聊天，並以繁體中文回答。"""
            logger.info(f"Sending prompt to {role_mode} LLM (length: {len(prompt)} chars)")

            if role_mode == "local":
                answer = await query_local_llm(prompt, role="basic")
            else:
                answer = await call_cloud_llm(prompt, role="basic")

            if len(answer) > 1900:
                answer = answer[:1900] + "...（已截斷）"

            reply = f"{interaction.user.mention} 問了：{問題}\n\n{answer}"

            tz = timezone(timedelta(hours=8))
            call_time = datetime.now(tz).isoformat()
            record = {
                "channel_id": str(channel.name),
                "user_id": str(interaction.user.global_name),
                "command": "解答之書",
                "question": 問題,
                "prompt": prompt,
                "summary": answer,
                "call_time": call_time,
            }
            insert_summary(record)

            # 發信通知
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_sarn_notify(record, GMAIL_SEND_TO)
                    logger.info(f"SERN Notify sent, messageId={msg_id}")
                except Exception as e:
                    logger.error(f"發信失敗：{e}")

            if answer.startswith("Error contacting local LLM"):  # 終止問答
                await interaction.followup.send("AI 罷工了捏 _(:з」∠)\_", ephemeral=True)
                return

            await interaction.followup.send(reply)
        except Exception as e:
            logger.error(f"Error in answer_book: {e}", exc_info=True)
            # 發錯誤通知信
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_error_notify(e, record, GMAIL_SEND_TO)
                    logger.info(f"Error notify sent, messageId={msg_id}")
                except Exception as mail_err:
                    logger.error(f"無法發送錯誤電子郵件: {mail_err}", exc_info=True)
            await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)

    @bot.tree.command(name="deepfaker", description=f"DeepFaker 偽裝成指定用戶發送訊息，有一定機率會爆炸")
    async def deepfaker(interaction: discord.Interaction, 冒牌對象: discord.Member, 內容: str):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        trimmed_content = 內容.strip()
        if not trimmed_content:
            await interaction.response.send_message("請提供要偽裝發送的內容。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        should_fail = random.random() < float(DEEPFAKER_FAILURE_PROB)
        parts = [s.strip() for s in DEEPFAKER_FAILURE_NOTICE.split('|') if s.strip()]
        if not parts:
            failure_text = "抓到你了！炸彈魔！"
        else:
            failure_text = random.choice(parts)
        fake_message_content = ""  # 偽裝失敗訊息

        if should_fail:
            username = interaction.user.display_name or interaction.user.name
            avatar_url = interaction.user.display_avatar.url
            message_content = trimmed_content  # 訊息內容
            if failure_text:
                reserve_length = len(failure_text) + 1
                # 爆炸後增補內容
                fake_username = 冒牌對象.display_name or 冒牌對象.name
                fake_message_content = f"↖️ 這個人想偽裝成 {fake_username} 說 「{message_content}」"
                if len(fake_message_content) + reserve_length > 2000:
                    allowed = 2000 - reserve_length
                    fake_message_content = fake_message_content[:max(allowed, 0)]
                message_content = f"{fake_message_content}\n{failure_text}".strip()
            result_message = f"DeepFaker 轉換失敗，已使用你的身份發送訊息。 ({failure_text or '無額外說明'})"
            success = False
            target_for_log = interaction.user.display_name or interaction.user.name
            logger.info(f"偽裝失敗log紀錄 {username}{fake_message_content or message_content}")
        else:
            username = 冒牌對象.display_name or 冒牌對象.name
            avatar_url = 冒牌對象.display_avatar.url
            message_content = trimmed_content[:2000]
            result_message = f"已偽裝成 {username} 發送訊息。"
            success = True
            target_for_log = username

        try:
            await send_with_webhook(channel, message_content, username, avatar_url, "DeepFaker")
        except discord.Forbidden:
            logger.error("DeepFaker 需要 Manage Webhooks 權限才能發送訊息。")
            await interaction.followup.send("無法使用 DeepFaker，缺少建立或使用 Webhook 的權限。", ephemeral=True)
            return
        except discord.HTTPException as http_err:
            logger.error(f"DeepFaker webhook 發送失敗: {http_err}", exc_info=True)
            await interaction.followup.send(f"DeepFaker 發送失敗：{http_err}", ephemeral=True)
            return

        log_message = f"DeepFaker invoked by {interaction.user.display_name or interaction.user.name} -> {target_for_log} (status={success})"
        logger.info(log_message)

        # 組織發信內容
        tz = timezone(timedelta(hours=8))
        call_time = datetime.now(tz).isoformat()
        record = {
            "channel_id": str(channel.name),
            "user_id": str(interaction.user.display_name or interaction.user.name),
            "command": "deepfaker",
            "question": log_message,
            "prompt": message_content,
            "summary": fake_message_content,
            "call_time": call_time,
        }

        # 發信通知
        if GMAIL_SEND_TO:
            try:
                msg_id = send_deepfaker_notify(record, GMAIL_SEND_TO)
                logger.info(f"SERN deepfaker result sent, messageId={msg_id}")
            except Exception as e:
                logger.error(f"發信失敗：{e}")

        await interaction.followup.send(result_message, ephemeral=True)

    @bot.tree.command(name="el_psy_kongroo", description="一切都是命運石之門的選擇！")
    async def el_psy_kongroo(interaction: discord.Interaction, 問題: str):
        role_mode = os.getenv("ROLE_MODE", "local")
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)  # 改以webhook處理回答，不需要此行效果
        TZ_8 = timezone(timedelta(hours=8))
        try:
            messages = []
            async for msg in channel.history(limit=50, oldest_first=False):
                messages.append(msg)
                if len(messages) >= 20:
                    break

            if not messages:
                await interaction.followup.send("找不到最近的訊息。")
                return

            history = "\n".join([
                f"[{m.created_at.astimezone(TZ_8).strftime('%H:%M')}] {m.author.display_name}: {m.content}"
                for m in reversed(messages)
            ])

            prompt = f"""以下是此頻道最近的 20 則對話：\n{history}\n\n使用者問題：{問題}\n可以根據對話內容與使用者聊天，並以繁體中文回答，可適度帶入命運石之門風格語感。"""
            logger.info(f"Sending prompt to {role_mode} LLM (length: {len(prompt)} chars)")

            if role_mode == "local":
                answer = await query_local_llm(prompt, role="kurisu")
            else:
                answer = await call_cloud_llm(prompt, role="kurisu")

            if len(answer) > 1900:
                answer = answer[:1900] + "...（已截斷）"

            reply = f"{interaction.user.mention} 問了：{問題}\n\n{answer}"

            tz = timezone(timedelta(hours=8))
            call_time = datetime.now(tz).isoformat()
            record = {
                "channel_id": str(channel.name),
                "user_id": str(interaction.user.global_name),
                "command": "el_psy_kongroo",
                "question": 問題,
                "prompt": prompt,
                "summary": answer,
                "call_time": call_time,
            }
            insert_summary(record)

            # 發信通知
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_sarn_notify(record, GMAIL_SEND_TO)
                    logger.info(f"SERN Notify sent, messageId={msg_id}")
                except Exception as e:
                    logger.error(f"發信失敗：{e}")

            if answer.startswith("Error contacting local LLM"):  # 終止問答
                await interaction.followup.send("Amadeus 罷工了捏 _(:з」∠)\_", ephemeral=True)
                return

            # await interaction.followup.send(reply)
            await send_as_amadeus(channel, reply)  # webhook偽裝角色
            # 安靜刪除 loading 訊息，SERN 消失
            await interaction.delete_original_response()

        except Exception as e:
            logger.error(f"Error in answer_book: {e}", exc_info=True)
            # 發錯誤通知信
            if GMAIL_SEND_TO:
                try:
                    msg_id = send_error_notify(e, record, GMAIL_SEND_TO)
                    logger.info(f"Error notify sent, messageId={msg_id}")
                except Exception as mail_err:
                    logger.error(f"無法發送錯誤電子郵件: {mail_err}", exc_info=True)
            await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)

    async def send_as_amadeus(channel: discord.TextChannel, content: str):
        avatar_url = "https://media.discordapp.net/attachments/1409948881822814450/1413559087609938031/Amadeus.png?ex=68bc5efd&is=68bb0d7d&hm=a9069c5e97de585f0d34479acee579394a52d69bc81445ff3a1fb7c5571e00fa&=&format=webp&quality=lossless&width=1084&height=1084"
        await send_with_webhook(channel, content, "Amadeus", avatar_url, "Amadeus")

    async def send_with_webhook(channel: discord.TextChannel, content: str, username: str,
                                avatar_url: str, webhook_name: str):
        webhooks = await channel.webhooks()
        webhook = discord.utils.get(webhooks, name=webhook_name)

        if webhook is None:
            webhook = await channel.create_webhook(name=webhook_name)

        await webhook.send(
            content,
            username=username,
            avatar_url=avatar_url
        )
