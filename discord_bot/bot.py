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
