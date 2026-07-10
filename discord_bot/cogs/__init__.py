from __future__ import annotations

from discord.ext import commands

EXTENSIONS = (
    "discord_bot.cogs.summary_cog",
    "discord_bot.cogs.conversation_cog",
    "discord_bot.cogs.fun_cog",
    "discord_bot.cogs.social_preview_cog",
    "discord_bot.cogs.social_preview_settings_cog",
)


async def load_extensions(bot: commands.Bot) -> None:
    """Load every first-party cog declared in this package."""
    for extension in EXTENSIONS:
        if extension in bot.extensions:
            continue
        await bot.load_extension(extension)
