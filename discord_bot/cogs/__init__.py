from __future__ import annotations

import os

from discord.ext import commands

BASE_EXTENSIONS = (
    "discord_bot.cogs.summary_cog",
    "discord_bot.cogs.conversation_cog",
    "discord_bot.cogs.fun_cog",
    "discord_bot.cogs.social_preview_cog",
    "discord_bot.cogs.social_preview_settings_cog",
)

WORLD_CUP_BETTING_EXTENSION = "discord_bot.cogs.world_cup_betting_cog"


def get_extensions() -> tuple[str, ...]:
    if os.getenv("WORLD_CUP_BETTING_ENABLED") == "1":
        return BASE_EXTENSIONS + (WORLD_CUP_BETTING_EXTENSION,)
    return BASE_EXTENSIONS


async def load_extensions(bot: commands.Bot) -> None:
    """Load every first-party cog declared in this package."""
    for extension in get_extensions():
        if extension in bot.extensions:
            continue
        await bot.load_extension(extension)
