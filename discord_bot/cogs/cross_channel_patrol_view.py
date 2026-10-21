"""Ephemeral, staged cross-channel settings using Discord's channel picker."""
from __future__ import annotations

import discord

from ..features.social_preview.duplicate_service import social_duplicate_service


def text_channel_ids(bot, guild_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    return frozenset(str(channel.id) for channel in guild.text_channels
                     if channel.type == discord.ChannelType.text) if guild else frozenset()


def channel_summary(channels):
    ordered = sorted(channels, key=int)
    shown = "、".join(f"<#{channel}>" for channel in ordered[:25]) or "未選取"
    return shown + (f"（另有 {len(ordered) - 25} 個，可用移除選單調整）" if len(ordered) > 25 else "")


class CrossChannelLauncher(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if (interaction.guild is None or interaction.user.id != self.owner_id
                or not interaction.user.guild_permissions.manage_guild):
            await interaction.response.send_message("僅開啟面板且具有「管理伺服器」權限的管理員可操作。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="管理跨頻道出警", style=discord.ButtonStyle.secondary)
    async def open_panel(self, interaction, button):
        if not social_duplicate_service.ready:
            await interaction.response.send_message("資料庫／快取尚未就緒，請稍後再試。", ephemeral=True)
            return
        panel = CrossChannelPanel(self.owner_id, str(interaction.guild_id))
        await interaction.response.send_message(panel.content(), view=panel, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


class CrossChannelPanel(CrossChannelLauncher):
    def __init__(self, owner_id, guild_id):
        super().__init__(owner_id)
        self.clear_items()
        self.guild_id = guild_id
        self.original = social_duplicate_service.cross_effective(guild_id)
        self.enabled, channels = self.original
        self.channels = set(channels)
        for row, label, callback in ((0, "新增文字頻道（可分批選取）", self.add_channels),
                                     (1, "移除文字頻道（可分批選取）", self.remove_channels)):
            picker = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                               placeholder=label, min_values=1, max_values=25, row=row)
            picker.callback = callback
            self.add_item(picker)
        for label, callback, row in (("啟用跨頻道並儲存", self.enable_and_save, 2),
                                     ("停用跨頻道並儲存", self.disable_and_save, 2),
                                     ("只儲存頻道清單", self.save, 2),
                                     ("清空清單", self.clear, 3),
                                     ("取消未儲存變更", self.discard, 3)):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=row)
            button.callback = callback
            self.add_item(button)
        self.update_buttons()

    def valid_channels(self):
        return self.channels & set(social_duplicate_service.text_channel_ids(self.guild_id))

    def update_buttons(self):
        self.children[2].style = discord.ButtonStyle.success
        self.children[2].disabled = len(self.valid_channels()) < 2
        self.children[3].disabled = not self.enabled
        self.children[4].label = "儲存頻道變更" if self.enabled else "只儲存清單（不啟用）"
        self.children[4].disabled = self.channels == set(self.original[1])
        self.children[6].disabled = self.channels == set(self.original[1])

    def content(self):
        active = len(self.valid_channels())
        master, days = social_duplicate_service.effective(self.guild_id)
        saved_enabled, saved_channels = self.original
        valid = set(social_duplicate_service.text_channel_ids(self.guild_id))
        saved_active = saved_channels & valid if saved_enabled else set()
        running = master and saved_enabled and len(saved_active) >= 2
        status = "🟢 已生效" if running else "⚪ 已停用" if not saved_enabled else "🟡 尚未生效"
        dirty = self.channels != set(saved_channels)
        if not master:
            hint = "總出警已停用。請先執行 `/社群出警設定 狀態:啟用`，再開啟此面板。"
        elif active < 2:
            hint = "下一步：從上方新增選單選取至少兩個文字頻道，再按「啟用跨頻道並儲存」。"
        elif not saved_enabled:
            hint = "頻道已選好！請按綠色「啟用跨頻道並儲存」。只儲存清單不會開啟跨頻道。"
        elif dirty:
            hint = "請按「儲存頻道變更」套用；儲存前仍使用原本的頻道範圍。"
        else:
            hint = "清單內已雙向出警，共用次數並引用首次分享。" if running else "請調整頻道清單後儲存。"
        return (f"**跨頻道出警設定**\n"
                f"目前已套用：{status}｜總出警：{'啟用' if master else '停用'}｜追溯 {days} 天\n"
                f"已套用頻道：{channel_summary(saved_channels)}\n"
                f"**{'📝 尚未儲存的頻道清單' if dirty else '頻道清單'}**（{len(self.channels)} 個，{active} 個有效）\n"
                f"{channel_summary(self.channels)}\n{hint}\n"
                "清單外維持同頻道出警。請自行排除私密頻道；討論串不納入。")

    async def refresh(self, interaction):
        self.update_buttons()
        await interaction.response.edit_message(content=self.content(), view=self,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def add_channels(self, interaction):
        self.channels.update(str(channel.id) for channel in self.children[0].values)
        await self.refresh(interaction)

    async def remove_channels(self, interaction):
        self.channels.difference_update(str(channel.id) for channel in self.children[1].values)
        self.channels.intersection_update(social_duplicate_service.text_channel_ids(self.guild_id))
        await self.refresh(interaction)

    async def enable_and_save(self, interaction):
        if len(self.valid_channels()) < 2:
            await interaction.response.send_message("請先選取至少兩個有效文字頻道。", ephemeral=True)
            return
        if not social_duplicate_service.effective(self.guild_id)[0]:
            await interaction.response.send_message("總出警已停用，請先執行 `/社群出警設定 狀態:啟用`。", ephemeral=True)
            return
        await self.save(interaction, enabled=True)

    async def disable_and_save(self, interaction):
        await self.save(interaction, enabled=False)

    async def discard(self, interaction):
        self.enabled, channels = self.original
        self.channels = set(channels)
        await self.refresh(interaction)

    async def clear(self, interaction):
        self.channels.clear()
        await self.refresh(interaction)

    async def save(self, interaction, *, enabled=None):
        await interaction.response.defer(ephemeral=True)
        try:
            saved = await social_duplicate_service.configure_cross_channel(
                self.guild_id, enabled=self.enabled if enabled is None else enabled, channel_ids=self.channels,
                updated_by=str(interaction.user.id), expected=self.original)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not saved:
            await interaction.followup.send("資料庫或快取重建失敗，變更未完整套用；出警暫停，請稍後查詢狀態。", ephemeral=True)
            return
        self.original = social_duplicate_service.cross_effective(self.guild_id)
        self.enabled, channels = self.original
        self.channels = set(channels)
        self.update_buttons()
        if self.enabled:
            summary = "已儲存。" + ("跨頻道出警已生效。" if len(self.valid_channels()) >= 2
                                  and social_duplicate_service.effective(self.guild_id)[0]
                                  else "跨頻道尚未生效，請查看下方說明。")
        else:
            summary = "已儲存。跨頻道出警仍為停用，只執行同頻道出警。"
        await interaction.edit_original_response(content=summary + "\n\n" + self.content(), view=self,
                                                 allowed_mentions=discord.AllowedMentions.none())
