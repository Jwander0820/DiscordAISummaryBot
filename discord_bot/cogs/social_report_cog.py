from __future__ import annotations

import asyncio
from contextlib import suppress
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..features.monthly_report.delivery import DiscordReportTransport, channel_problem
from ..features.monthly_report.service import describe_schedule, monthly_report_service

STATUS_LABELS = {"pending": "處理中", "sent": "已送達", "empty": "整月無資料，已結算略過",
                 "failed": "失敗，需手動處理", "unknown": "送達不明，需手動確認"}
PROBLEM_LABELS = {"ChannelNotFound": "找不到這個頻道。", "ChannelOutsideGuild": "頻道必須屬於本伺服器。",
                  "ChannelNotText": "請選擇一般文字頻道。",
                  "MissingPermissions": "Bot 在該頻道需要「檢視頻道」、「傳送訊息」與「讀取訊息歷史」權限。"}
DB_UNAVAILABLE = "資料庫暫時無法使用，月報設定未變更，請稍後再試。"


class SocialReportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.transport = DiscordReportTransport(bot)
        self._task = None

    async def cog_load(self):
        # One scheduler per process; it only acts on guilds an administrator enabled, and
        # cross-process duplicates are stopped by the DB claim.
        if self._task is None:
            self._task = asyncio.create_task(monthly_report_service.run_forever(
                self.transport, wait_ready=self.bot.wait_until_ready))

    async def cog_unload(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await monthly_report_service.close()

    async def _guard(self, interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
            return False
        if not getattr(getattr(interaction.user, "guild_permissions", None), "manage_guild", False):
            await interaction.response.send_message("你需要「管理伺服器」權限才能管理社群出警月報。", ephemeral=True)
            return False
        return True

    @staticmethod
    def _status_text(settings, runs):
        channel = settings.get("channel_id")
        lines = [f"社群出警月報：{'啟用' if settings['enabled'] else '停用'}｜頻道："
                 + (f"<#{channel}>" if channel else "未指定")]
        if settings["enabled"]:
            lines.append(f"首次結算月份：{settings['first_report_month']}｜"
                         f"下次月界：{describe_schedule(time.time())}（台北時間）")
        else:
            lines.append("啟用方式：`/社群月報設定 頻道:#頻道 狀態:啟用`，從下一個月界開始結算。")
        for run in runs:
            pages = len(run["pages"] or [])
            sent = sum(1 for value in run["message_ids"] or [] if value)
            detail = f"，已送達 {sent}/{pages} 頁" if pages > 1 else ""
            lines.append(f"・{run['report_month']}：{STATUS_LABELS.get(run['status'], run['status'])}{detail}"
                         + (f"（<#{run['channel_id']}>）" if run["channel_id"] else ""))
        lines.append("頻道變更從下一期生效；已開始的月報維持原目的地。")
        return "\n".join(lines)

    @app_commands.command(name="社群月報設定", description="啟用或停用社群出警月報，並指定發送頻道")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(channel="頻道", state="狀態")
    @app_commands.choices(state=[app_commands.Choice(name="啟用", value="enabled"),
                                app_commands.Choice(name="停用", value="disabled")])
    async def configure_report(self, interaction: discord.Interaction,
                               channel: Optional[discord.TextChannel] = None,
                               state: Optional[app_commands.Choice[str]] = None):
        if not await self._guard(interaction):
            return
        state_value = getattr(state, "value", state)
        target = None
        if channel is not None:
            target = interaction.guild.get_channel(channel.id) or channel
            problem = channel_problem(target, interaction.guild.id)
            if problem:
                await interaction.response.send_message(
                    PROBLEM_LABELS.get(problem, "無法使用這個頻道。") + " 設定未變更。", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        try:
            if channel is None and state_value is None:
                settings = await monthly_report_service.settings(interaction.guild.id)
            else:
                settings = await monthly_report_service.configure(
                    interaction.guild.id, state=state_value,
                    channel_id=None if target is None else target.id, updated_by=interaction.user.id)
            runs = await monthly_report_service.runs(interaction.guild.id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            await interaction.followup.send(DB_UNAVAILABLE, ephemeral=True)
            return
        prefix = "已更新。\n" if channel is not None or state_value is not None else ""
        await interaction.followup.send(prefix + self._status_text(settings, runs), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(SocialReportCog(bot))
