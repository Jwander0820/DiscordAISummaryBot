from __future__ import annotations

import discord
from discord.ext import commands

from ..features.social_preview.facebook_preview import extract_facebook_urls, handle_facebook_in_message
from ..features.social_preview.instagram_preview import extract_instagram_urls, handle_instagram_in_message
from ..features.social_preview.settings import PLATFORM_FACEBOOK, PLATFORM_INSTAGRAM, PLATFORM_THREADS, is_social_preview_enabled
from ..features.social_preview.threads_preview import extract_threads_urls, handle_threads_in_message


class SocialPreviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """社群預覽的總入口。

        這層只負責判斷是否要處理哪一種平台，實際 preview 組裝交給 feature layer。
        """
        if message.author.bot:
            return

        content = message.content or ""
        has_threads_url = bool(extract_threads_urls(content))
        has_instagram_url = bool(extract_instagram_urls(content))
        has_facebook_url = bool(extract_facebook_urls(content))
        if not has_threads_url and not has_instagram_url and not has_facebook_url:
            return

        guild_id = str(message.guild.id) if message.guild is not None else None

        # Threads 優先處理；若已經成功代發 preview，Facebook 就不用再接手。
        if has_threads_url and is_social_preview_enabled(guild_id, PLATFORM_THREADS):
            handled = await handle_threads_in_message(message)
            if handled:
                return

        if has_instagram_url and is_social_preview_enabled(guild_id, PLATFORM_INSTAGRAM):
            handled = await handle_instagram_in_message(message)
            if handled:
                return

        if has_facebook_url and is_social_preview_enabled(guild_id, PLATFORM_FACEBOOK):
            await handle_facebook_in_message(message)


async def setup(bot: commands.Bot) -> None:
    """註冊 social preview cog。"""
    await bot.add_cog(SocialPreviewCog(bot))
