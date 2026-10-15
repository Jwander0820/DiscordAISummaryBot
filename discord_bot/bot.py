import asyncio
from contextlib import suppress
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from .core.bootstrap import bootstrap_application

bootstrap_application()

from .cogs import load_extensions
from .integrations.gemini_client import gemini_model
from .features.notifications.database import run_database_notifications

logger = logging.getLogger("discord_digest_bot")

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN environment variable not set.")


class DiscordSummaryBot(commands.Bot):
    """Bot 主體。

    這層盡量只保留 Discord 啟動與 extension 載入，不放業務邏輯。
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._synced = False
        self._database_notifications_task = None

    async def setup_hook(self) -> None:
        """在連上 Discord 前先載入所有 cogs。"""
        await load_extensions(self)
        if self._database_notifications_task is None:
            self._database_notifications_task = asyncio.create_task(run_database_notifications(self))

    async def close(self) -> None:
        if self._database_notifications_task is not None:
            self._database_notifications_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._database_notifications_task
            self._database_notifications_task = None
        await super().close()


bot = DiscordSummaryBot()


@bot.event
async def on_ready() -> None:
    """Bot 上線後只做一次 slash command sync，避免重複同步。"""
    if bot.user is None:
        return

    logger.info("%s has connected to Discord!", bot.user.name)
    if bot._synced:
        return

    logger.info("Attempting to sync slash commands...")
    try:
        synced = await bot.tree.sync()
        bot._synced = True
        logger.info("Synced %s slash commands.", len(synced))
    except Exception as exc:
        logger.error("Failed to sync slash commands: %s", exc, exc_info=True)


def run() -> None:
    """根據環境變數啟動 bot。"""
    if not BOT_TOKEN:
        logger.critical("Bot token is not configured. Exiting.")
        return
    if not gemini_model:
        logger.warning("Gemini API Key not found or model init failed. Summarization features disabled.")
    logger.info("Starting bot...")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    run()
