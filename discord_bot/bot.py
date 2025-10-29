from datetime import datetime
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables before importing modules that depend on them
load_dotenv()

from . import database
from .gemini_client import gemini_model
from .commands import register as register_commands
from .threads_preview import handle_threads_in_message, extract_threads_urls
logger = logging.getLogger('discord_digest_bot')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Initialize database
database.init_db()

GUILD_ID = os.environ.get('DISCORD_GUILD_ID')  # 把這裡換成你的伺服器 ID

BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN environment variable not set.")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


def threads_preview_enabled() -> bool:
    # 預設啟用；Zeabur 上設 THREADS_PREVIEW_ENABLED=0 就會關閉
    return os.getenv("THREADS_PREVIEW_ENABLED", "1") == "1"


@bot.event
async def on_ready():
    logger.info(f'{bot.user.name} has connected to Discord!')
    logger.info("Attempting to sync slash commands...")
    try:
        # 全域註冊
        synced = await bot.tree.sync()
        # 只同步到特定伺服器（GUILD），立即生效，更新指令使用
        # guild = discord.Object(id=GUILD_ID)
        # synced = await bot.tree.sync(guild=guild)

        logger.info(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")


register_commands(bot)

@bot.event
async def on_message(message: discord.Message):
    # 其他 bot/系統訊息直接放行
    if message.author.bot:
        return await bot.process_commands(message)

    # 功能關閉時：**不要做 Threads 預覽**，直接交給其他指令
    if not threads_preview_enabled():
        return await bot.process_commands(message)

    # （可選）快速檢查訊息裡是否有 Threads 連結，沒連結就不要呼叫 handler（省成本）
    has_threads_url = bool(extract_threads_urls(message.content or ""))

    # 功能開啟時，且真的有連結再處理
    if has_threads_url:
        handled = await handle_threads_in_message(message)
        if handled:
            return  # 成功預覽就不要往下傳給指令解析

    # 沒處理或沒連結：交給其他指令
    await bot.process_commands(message)

def run():
    if not BOT_TOKEN:
        logger.critical("Bot token is not configured. Exiting.")
        return
    if not gemini_model:
        logger.warning("Gemini API Key not found or model init failed. Summarization features disabled.")
    logger.info("Starting bot...")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    run()
