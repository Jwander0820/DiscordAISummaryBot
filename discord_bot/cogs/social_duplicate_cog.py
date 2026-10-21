from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..features.social_preview.duplicate_service import social_duplicate_service
from ..features.social_preview.duplicate_patrol import record_original_share
from ..features.social_preview.x_links import extract_first_x_url
from ..features.social_preview.sender import log_preview_link


class SocialDuplicateCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._recovery_task = None

    async def cog_load(self):
        from .cross_channel_patrol_view import text_channel_ids
        social_duplicate_service.text_channel_ids = lambda guild_id: text_channel_ids(self.bot, guild_id)
        await social_duplicate_service.rebuild()
        self._recovery_task = asyncio.create_task(social_duplicate_service.recover())

    async def cog_unload(self):
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        await social_duplicate_service.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (message.author.bot or getattr(message, "webhook_id", None) is not None
                or message.guild is None):
            return
        url = extract_first_x_url(message.content or "")
        if url is not None:
            log_preview_link(message, platform="X", url=url)
            await record_original_share(message, platform="x", url=url)

    @app_commands.command(name="社群出警設定", description="設定社群重複分享出警的開關、天數與跨頻道範圍")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(state="狀態", days="天數")
    @app_commands.choices(state=[app_commands.Choice(name="啟用", value="enabled"),
                                app_commands.Choice(name="停用", value="disabled"),
                                app_commands.Choice(name="恢復環境預設", value="default")])
    async def configure_patrol(self, interaction: discord.Interaction,
                               state: Optional[app_commands.Choice[str]] = None,
                               days: Optional[int] = None):
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
            return
        if not getattr(getattr(interaction.user, "guild_permissions", None), "manage_guild", False):
            await interaction.response.send_message("你需要「管理伺服器」權限才能修改社群出警設定。", ephemeral=True)
            return
        if state is None and days is None:
            from .cross_channel_patrol_view import CrossChannelLauncher
            await interaction.response.send_message(self._status(str(interaction.guild.id)), ephemeral=True,
                                                    view=CrossChannelLauncher(interaction.user.id))
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await social_duplicate_service.configure(
                str(interaction.guild.id), state=getattr(state, "value", state), days=days, updated_by=str(interaction.user.id))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not result:
            await interaction.followup.send("資料庫或快取重建失敗，變更未完整套用；出警暫停，請稍後查詢狀態。", ephemeral=True)
            return
        from .cross_channel_patrol_view import CrossChannelLauncher
        await interaction.followup.send("已更新。" + self._status(str(interaction.guild.id)), ephemeral=True,
                                        view=CrossChannelLauncher(interaction.user.id))

    def _status(self, guild_id):
        if not social_duplicate_service.ready:
            return "社群出警暫停：資料庫／快取尚未就緒，正在等待重建。"
        enabled, days = social_duplicate_service.effective(guild_id)
        source = "伺服器設定" if guild_id in social_duplicate_service.settings else "環境預設"
        from .cross_channel_patrol_view import channel_summary
        cross_enabled, channels = social_duplicate_service.cross_effective(guild_id)
        active = social_duplicate_service.cross_channels(guild_id)
        cross_status = "啟用" if cross_enabled else "停用"
        effect = "生效" if enabled and active else "未生效"
        hint = "\n已選頻道不代表已啟用，請在管理面板按「啟用跨頻道並儲存」。" if channels and not cross_enabled else ""
        return (f"社群出警：{'啟用' if enabled else '停用'}｜載入最近 {days} 天｜{source}。快取已就緒。\n"
                f"跨頻道：{cross_status}（{effect}，至少兩個有效文字頻道）｜清單外同頻道判重。\n"
                f"已選頻道：{channel_summary(channels)}\n有效範圍：{channel_summary(active)}{hint}")

    @app_commands.command(name="社群出警狀態", description="查看社群出警設定、跨頻道範圍與快取狀態")
    async def patrol_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
            return
        await interaction.response.send_message(self._status(str(interaction.guild.id)), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SocialDuplicateCog(bot))
