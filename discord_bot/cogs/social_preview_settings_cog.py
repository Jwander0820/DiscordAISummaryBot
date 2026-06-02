from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..features.social_preview.settings import (
    PLATFORM_FACEBOOK,
    PLATFORM_INSTAGRAM,
    PLATFORM_THREADS,
    SUPPORTED_PLATFORMS,
    SocialPreviewSettingStatus,
    social_preview_settings_service,
)

PLATFORM_ALL = "all"
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_DEFAULT = "default"

_PLATFORM_LABELS = {
    PLATFORM_THREADS: "Threads",
    PLATFORM_FACEBOOK: "Facebook",
    PLATFORM_INSTAGRAM: "Instagram",
}

_STATE_LABELS = {
    STATE_ENABLED: "啟用",
    STATE_DISABLED: "停用",
    STATE_DEFAULT: "預設",
}

_SOURCE_LABELS = {
    "global_default": "全域預設",
    "guild_override": "伺服器設定",
}


def _choice_value(value) -> str:
    return getattr(value, "value", value)


def _has_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    return bool(getattr(permissions, "manage_guild", False))


def _format_status_line(status: SocialPreviewSettingStatus) -> str:
    state = "啟用" if status.effective_enabled else "停用"
    source = _SOURCE_LABELS.get(status.source, status.source)
    label = _PLATFORM_LABELS.get(status.platform, status.platform)
    return f"{label}: {state}（{source}）"


class SocialPreviewSettingsCog(commands.Cog):
    """Slash commands for guild-level Social Preview settings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="社群預覽設定", description="設定本伺服器的 Threads / Facebook / Instagram 自動預覽")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(platform="平台", state="狀態")
    @app_commands.describe(platform="要調整的平台", state="要套用的狀態")
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="Threads", value=PLATFORM_THREADS),
            app_commands.Choice(name="Facebook", value=PLATFORM_FACEBOOK),
            app_commands.Choice(name="Instagram", value=PLATFORM_INSTAGRAM),
            app_commands.Choice(name="全部", value=PLATFORM_ALL),
        ],
        state=[
            app_commands.Choice(name="啟用", value=STATE_ENABLED),
            app_commands.Choice(name="停用", value=STATE_DISABLED),
            app_commands.Choice(name="預設", value=STATE_DEFAULT),
        ],
    )
    async def configure_social_preview(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        state: app_commands.Choice[str],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
            return

        if not _has_manage_guild(interaction):
            await interaction.response.send_message("你需要「管理伺服器」權限才能修改社群預覽設定。", ephemeral=True)
            return

        platform_value = _choice_value(platform)
        state_value = _choice_value(state)
        target_platforms = SUPPORTED_PLATFORMS if platform_value == PLATFORM_ALL else (platform_value,)
        guild_id = str(interaction.guild.id)
        updated_by = str(interaction.user.id)

        for target_platform in target_platforms:
            if state_value == STATE_DEFAULT:
                social_preview_settings_service.clear_override(guild_id, target_platform)
            else:
                social_preview_settings_service.set_override(
                    guild_id,
                    target_platform,
                    state_value == STATE_ENABLED,
                    updated_by=updated_by,
                )

        statuses = social_preview_settings_service.list_statuses(guild_id)
        lines = [_format_status_line(statuses[platform_name]) for platform_name in SUPPORTED_PLATFORMS]
        target_label = "全部平台" if platform_value == PLATFORM_ALL else _PLATFORM_LABELS.get(platform_value, platform_value)
        state_label = _STATE_LABELS.get(state_value, state_value)
        await interaction.response.send_message(
            f"已將 {target_label} 設為「{state_label}」。\n" + "\n".join(lines),
            ephemeral=True,
        )

    @app_commands.command(name="社群預覽狀態", description="查看本伺服器的社群預覽設定")
    async def social_preview_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
            return

        statuses = social_preview_settings_service.list_statuses(str(interaction.guild.id))
        lines = [_format_status_line(statuses[platform_name]) for platform_name in SUPPORTED_PLATFORMS]
        await interaction.response.send_message("社群預覽狀態：\n" + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register the Social Preview settings cog."""
    await bot.add_cog(SocialPreviewSettingsCog(bot))
