from datetime import datetime, timezone, timedelta
import logging
import os
import sqlite3
import psycopg2
import discord
import google.generativeai as genai
from discord.ext import commands
from dotenv import load_dotenv  # Import dotenv

load_dotenv()

# # --- SQLite 初始化 ---
# # 建立或連線到 local 檔案 digest.db
# conn = sqlite3.connect("digest.db")
# cursor = conn.cursor()
# # 建立 summaries 資料表
# cursor.execute("""
# CREATE TABLE IF NOT EXISTS summaries (
#     id          INTEGER PRIMARY KEY AUTOINCREMENT,
#     channel_id  TEXT,
#     user_id     TEXT,
#     command     TEXT,
#     question    TEXT,
#     prompt      TEXT,
#     summary     TEXT,
#     call_time   TEXT
# );
# """)
# conn.commit()

# --- PostgreSQL 初始化 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL not set")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# 建立資料表
cursor.execute("""
CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    channel_id TEXT,
    user_id TEXT,
    command TEXT,
    question TEXT,
    prompt TEXT,
    summary TEXT,
    call_time TIMESTAMP
);
""")
conn.commit()

GUILD_ID = 1255783788097835018  # 把這裡換成你的伺服器 ID

# --- Load Environment Variables ---
# Load variables from ..env file in the current directory
logger = logging.getLogger('discord_digest_bot')  # Define logger early for .env var logging

# --- Configuration ---
# Get Bot Token from environment variable
BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("ERROR: DISCORD_BOT_TOKEN environment variable not set.")
    exit()

# Get Gemini API Key from environment variable
GEMINI_API_KEY = os.environ.get('GOOGLE_GENAI_API_KEY')
if not GEMINI_API_KEY:
    logger.warning("WARNING: GOOGLE_GENAI_API_KEY environment variable not set. Summarization will fail.")
    # Allow the bot to run but warn that summarization won't work
else:
    try:
        # Configure Gemini API
        genai.configure(api_key=GEMINI_API_KEY)
        # Test the key validity (optional, but good practice)
        # genai.list_models() # This would raise an error if the key is invalid
        logger.info("Gemini API Key configured successfully.")
    except Exception as e:
        logger.error(f"Error configuring Gemini API: {e}. Please check your API key.")
        GEMINI_API_KEY = None  # Disable summarization if config fails

# Initialize Gemini Model (only if key is valid)
gemini_model = None
if GEMINI_API_KEY:
    try:
        gemini_model = genai.GenerativeModel("gemini-2.0-flash")  # Use a known valid model, like flash
        logger.info(f"Initialized Gemini Model: {gemini_model.model_name}")
    except Exception as e:
        logger.error(f"Error initializing Gemini model: {e}. Summarization might fail.")
        gemini_model = None  # Disable if model init fails

# Define the intents required by the bot
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.messages = True  # Required to fetch message history

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

logger.addHandler(file_handler)

# Initialize the bot
bot = commands.Bot(command_prefix="!", intents=intents)  # Using slash commands is preferred for modern bots


@bot.event
async def on_ready():
    logger.info(f'{bot.user.name} has connected to Discord!')
    logger.info("Attempting to sync slash commands...")
    try:
        # Sync commands globally (can take up to an hour to propagate)
        # 先同步全域
        synced = await bot.tree.sync()
        # 再同步到指定 Guild（立刻生效）
        # await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        logger.info(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")


# --- Summarization Logic ---
async def summarize_messages(messages: list[discord.Message], prompt_scope: str = "過去24小時") -> str:
    """
    Analyzes a list of Discord messages and returns a summary using Gemini.

    :param messages: List of discord.Message objects.
    :param prompt_scope: A string describing the time range or context (e.g. "過去24小時", "過去七天").
    :return: Summary text.
    """
    logger.info(f"Summarizing {len(messages)} messages...")
    TZ_8 = timezone(timedelta(hours=8))

    if not gemini_model:
        logger.warning("Gemini model not initialized or API key is missing/invalid.")
        return "Error: Summarization feature is not available."

    if not messages:
        return "No recent non-bot messages found to summarize."

    # 組裝對話格式
    message_text = "\n".join([
        f"[{msg.created_at.astimezone(TZ_8).strftime('%H:%M')}] [id:{msg.author.name}] {msg.author.display_name}: {msg.content}"
        for msg in reversed(messages)
    ])

    # Construct dynamic prompt
    prompt = f"""請以繁體中文總結{prompt_scope}內以下Discord聊天消息的關鍵主題與重要資訊。
聊天格式為 [HH:MM] 使用者: 訊息內容。請抓出重點主題，風格詼諧幽默，挑出幾句代表性發言，最後點名最積極的參與者。

聊天紀錄:
{message_text}

總結:"""

    logger.info(f"Sending prompt to Gemini (length: {len(prompt)} chars)")
    try:
        response = await gemini_model.generate_content_async(prompt)

        if not response.parts:
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                return f"Summarization blocked due to policy: {response.prompt_feedback.block_reason}"
            return "Summarization failed: No response from Gemini."

        summary_text = response.text
        # 取得 GMT+8 的當前時間字串
        tz = timezone(timedelta(hours=8))
        call_time = datetime.now(tz).isoformat()
        channel_id = messages[0].channel.name
        user_id = messages[0].author.global_name
        cursor.execute(
            "INSERT INTO summaries(channel_id, call_time, prompt, summary, command, user_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(channel_id), call_time, message_text, summary_text, f"{prompt_scope}總結", user_id)
        )
        conn.commit()
        logger.info("Summary saved successfully.")
        return summary_text.strip()

    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        return f"Error during summarization: {e}"


# --- Slash Command Definition ---
@bot.tree.command(name="聊那麼多誰看的完", description="總結頻道中的24小時內2000則訊息")
async def summarize(interaction: discord.Interaction, len_msg: int = 2000):
    """Slash command to trigger the summarization."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
        return

    # Defer response as summarization can take time
    await interaction.response.defer(ephemeral=False)  # Acknowledge interaction, visible to others

    try:
        # Calculate the time 24 hours ago
        time_since = datetime.now(timezone.utc) - timedelta(days=1)

        # Fetch messages
        logger.info(f"Fetching messages from channel '{channel.name}' (ID: {channel.id}) since {time_since}")
        messages = []
        # Limit fetch to avoid exceeding rate limits or memory, increase if needed
        async for message in channel.history(limit=int(len_msg*1.1), after=time_since, oldest_first=False):
            if not message.author.bot:  # Ignore bot messages
                messages.append(message)
            if len(messages) >= len_msg:  # Stop fetching after 2000 non-bot messages to limit prompt size
                logger.info("Reached message limit (2000) for summarization.")
                break

        logger.info(f"Fetched {len(messages)} non-bot messages for summarization.")

        # Generate summary
        summary_text = await summarize_messages(messages)

        # Send the summary
        if len(summary_text) > 1900:  # Discord message limit is 2000 chars
            # Try sending in chunks or just truncate
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
    """Slash command to trigger the summarization."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("This command can only be used in text channels.", ephemeral=True)
        return

    # Defer response as summarization can take time
    await interaction.response.defer(ephemeral=False)  # Acknowledge interaction, visible to others

    try:
        # 1 小時前的 UTC 時間
        time_since = datetime.now(timezone.utc) - timedelta(hours=1)
        logger.info(f"Fetching messages from channel '{channel.name}' since {time_since.isoformat()}")

        messages = []
        # Limit fetch to avoid exceeding rate limits or memory, increase if needed
        async for message in channel.history(limit=len_msg, after=time_since, oldest_first=False):
            if not message.author.bot:  # Ignore bot messages
                messages.append(message)

        logger.info(f"Fetched {len(messages)} non-bot messages in last hour.")

        # Generate summary
        summary_text = await summarize_messages(messages, prompt_scope="過去一小時")

        # Send the summary
        if len(summary_text) > 1900:  # Discord message limit is 2000 chars
            # Try sending in chunks or just truncate
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
async def deep_summary(interaction: discord.Interaction, len_msg:int = 10000):
    """Slash command to summarize the last 7 days of messages (up to 10,000)."""
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    try:
        # 計算過去 7 天時間點
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
    """
    讓使用者根據最近 1000 則對話內容提問，Gemini 幫忙回答。
    """
    question = 想問些什麼
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("此指令僅能用於文字頻道", ephemeral=True)
        return
    TZ_8 = timezone(timedelta(hours=8))

    await interaction.response.defer(ephemeral=False)

    try:
        # Calculate the time 24 hours ago
        time_since = datetime.now(timezone.utc) - timedelta(days=1)
        messages = []
        async for message in channel.history(limit=int(len_msg*1.1), after=time_since, oldest_first=False):  # 多抓一些保險
            # print(f"id: {message.author.id}")
            # print(f"name: {message.author.name}")
            # print(f"display_name: {message.author.display_name}")
            if not message.author.bot:
                messages.append(message)
            if len(messages) >= len_msg:
                break

        if not messages:
            await interaction.followup.send("找不到最近的訊息，無法回答問題。")
            return

        logger.info(f"Fetched {len(messages)} messages for user question analysis.")

        # 組裝對話格式
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

        response = await gemini_model.generate_content_async(prompt)

        if not response.parts:
            await interaction.followup.send("AI 無法提供回應（可能被內容審核攔截）。")
            return

        answer = response.text.strip()
        if len(answer) > 1900:
            answer = answer[:1900] + "...（回應過長，已截斷）"

        # 把使用者跟問題補在前面
        asker = interaction.user.mention  # 或 .display_name
        reply_content = (
            f"{asker} 問了：{question}\n\n"
            f"{answer}"
        )

        # 取得 GMT+8 的當前時間字串
        tz = timezone(timedelta(hours=8))
        call_time = datetime.now(tz).isoformat()
        # 寫入DB
        cursor.execute(
            """
            INSERT INTO summaries (channel_id, user_id, command, question, prompt, summary, call_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(channel.name),  # channel_id
                str(interaction.user.global_name),  # user_id
                "你要不要聽聽看你現在在講什麼",  # command
                question,  # question
                message_text,  # prompt
                answer,  # summary
                call_time  # call_time (GMT+8)
            )
        )
        conn.commit()
        await interaction.followup.send(reply_content)

    except Exception as e:
        logger.error(f"Error in ask_about_conversation: {e}", exc_info=True)
        await interaction.followup.send(f"發生錯誤：{e}", ephemeral=True)


def replit_run_bot():
    if not BOT_TOKEN:
        logger.critical("Bot token is not configured. Exiting.")
    elif not GEMINI_API_KEY:
        logger.warning("Gemini API Key not found. Bot will run without summarization features.")
        logger.info("Starting bot (summarization disabled)...")
        bot.run(BOT_TOKEN)
    else:
        logger.info("Starting bot...")
        bot.run(BOT_TOKEN)


# --- Run the Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("Bot token is not configured. Exiting.")
    elif not GEMINI_API_KEY:
        logger.warning("Gemini API Key not found. Bot will run without summarization features.")
        logger.info("Starting bot (summarization disabled)...")
        bot.run(BOT_TOKEN)
    elif os.environ.get("DISABLE_DISCORD_BOT") == "1":
        logger.info("DISABLE_DISCORD_BOT is set. Skipping bot startup.")
    else:
        logger.info("Starting bot...")
        bot.run(BOT_TOKEN)
