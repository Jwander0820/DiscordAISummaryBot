from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from tests.support import isolated_discord_suite

load_tests = isolated_discord_suite(__name__)


def request(user=42, manager=True, guild=True):
    return SimpleNamespace(guild=SimpleNamespace(id=1) if guild else None, guild_id=1,
                           user=SimpleNamespace(id=user, guild_permissions=SimpleNamespace(manage_guild=manager)),
                           response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())


class CrossChannelViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from discord_bot.cogs import cross_channel_patrol_view as module
        self.module = module
        self.service = SimpleNamespace(ready=True, cross_effective=Mock(return_value=(False, frozenset())),
            effective=Mock(return_value=(True, 7)), text_channel_ids=lambda guild: {"10", "11", "12"},
            configure_cross_channel=AsyncMock(return_value=True))
        async def persist(guild, *, enabled, channel_ids, **kwargs):
            self.service.cross_effective.return_value = (enabled, frozenset(channel_ids))
            return True
        self.service.configure_cross_channel.side_effect = persist
        self.patcher = patch.object(module, "social_duplicate_service", self.service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_real_sdk_picker_payload_and_staged_batch_selection(self):
        panel = self.module.CrossChannelPanel(42, "1")
        components = panel.to_components()
        self.assertEqual(components[0]["components"][0]["type"], 8)
        self.assertEqual(components[0]["components"][0]["channel_types"], [0])
        self.assertEqual(components[0]["components"][0]["max_values"], 25)
        for batch in ((10, 11), (12,)):
            panel.children[0]._values = [SimpleNamespace(id=value) for value in batch]
            await panel.add_channels(request())
        self.assertEqual(panel.channels, {"10", "11", "12"})
        self.service.configure_cross_channel.assert_not_awaited()
        panel.children[1]._values = [SimpleNamespace(id=11)]
        await panel.remove_channels(request())
        self.assertEqual(panel.channels, {"10", "12"})
        interaction = request()
        self.assertIn("尚未儲存", panel.content())
        self.assertEqual(panel.children[2].label, "啟用跨頻道並儲存")
        await panel.enable_and_save(interaction)
        self.service.configure_cross_channel.assert_awaited_once_with("1", enabled=True, channel_ids={"10", "12"},
                                                                     updated_by="42", expected=(False, frozenset()))
        self.assertIn("已儲存", interaction.edit_original_response.await_args.kwargs["content"])
        self.assertIn("已生效", interaction.edit_original_response.await_args.kwargs["content"])
        self.assertNotIn("尚未儲存", panel.content())

    async def test_permissions_owner_and_dm_rechecked(self):
        panel = self.module.CrossChannelPanel(42, "1")
        self.assertTrue(await panel.interaction_check(request()))
        for interaction in (request(user=43), request(manager=False), request(guild=False)):
            self.assertFalse(await panel.interaction_check(interaction))
            interaction.response.send_message.assert_awaited_once()

    async def test_command_without_arguments_opens_management_entry(self):
        from discord_bot.cogs import social_duplicate_cog as cog_module
        self.service.settings = {}
        self.service.cross_channels = lambda guild: frozenset()
        cog = cog_module.SocialDuplicateCog(None)
        interaction = request()
        with patch.object(cog_module, "social_duplicate_service", self.service):
            await cog.configure_patrol.callback(cog, interaction)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["view"].children[0].label, "管理跨頻道出警")
        await kwargs["view"].open_panel.callback(interaction)
        self.assertIsInstance(interaction.response.send_message.await_args.kwargs["view"], self.module.CrossChannelPanel)

    async def test_cross_channel_audit_keeps_reference_channel(self):
        from discord_bot.features.social_preview.patrol_records import build_patrol_event
        from discord_bot.features.social_preview.duplicate_service import DuplicateMatch
        from tests.test_social_duplicate_service import event
        source = request()
        source.author = source.user
        source.channel = SimpleNamespace(name="範例頻道")
        record = build_patrol_event(source, event("101", channel_id="11"), DuplicateMatch(event(), 2))
        self.assertEqual(record["channel_id"], "11")
        self.assertEqual(record["reference_channel_id"], "10")
        self.assertEqual(record["reference_message_id"], "1100")

    async def test_failed_save_and_missing_channels_never_claim_success(self):
        self.service.cross_effective.return_value = (True, frozenset())
        panel = self.module.CrossChannelPanel(42, "1")
        self.assertIn("尚未生效", panel.content())
        for outcome in (False, ValueError("設定已更新")):
            self.service.configure_cross_channel.side_effect = outcome if isinstance(outcome, Exception) else None
            self.service.configure_cross_channel.return_value = False
            interaction = request()
            await panel.save(interaction)
            interaction.edit_original_response.assert_not_awaited()
            interaction.followup.send.assert_awaited_once()
        panel.channels = {"10"}
        await panel.clear(request())
        self.assertFalse(panel.channels)

    async def test_saving_list_alone_explicitly_keeps_cross_channel_disabled(self):
        panel = self.module.CrossChannelPanel(42, "1")
        panel.channels = {"10", "11"}
        await panel.refresh(request())
        self.assertEqual(panel.children[4].label, "只儲存清單（不啟用）")
        self.assertIn("只儲存清單不會開啟", panel.content())
        interaction = request()
        await panel.save(interaction)
        self.assertFalse(self.service.configure_cross_channel.await_args.kwargs["enabled"])
        self.assertIn("仍為停用", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_enable_requires_two_channels_and_master_switch(self):
        panel = self.module.CrossChannelPanel(42, "1")
        self.assertTrue(panel.children[2].disabled)
        interaction = request()
        await panel.enable_and_save(interaction)
        self.assertIn("至少兩個", interaction.response.send_message.await_args.args[0])
        panel.channels = {"10", "11"}
        self.service.effective.return_value = (False, 7)
        interaction = request()
        await panel.enable_and_save(interaction)
        self.assertIn("總出警已停用", interaction.response.send_message.await_args.args[0])
        self.service.configure_cross_channel.assert_not_awaited()

    async def test_disable_keeps_channels_and_discard_restores_saved_list(self):
        self.service.cross_effective.return_value = (True, frozenset({"10", "11"}))
        panel = self.module.CrossChannelPanel(42, "1")
        panel.channels = {"10", "12"}
        await panel.discard(request())
        self.assertEqual(panel.channels, {"10", "11"})
        self.assertTrue(panel.children[4].disabled)
        interaction = request()
        await panel.disable_and_save(interaction)
        self.assertEqual(self.service.cross_effective.return_value, (False, frozenset({"10", "11"})))
        self.assertIn("仍為停用", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_only_general_text_channels_in_inventory(self):
        bot = Mock()
        bot.get_guild.return_value = SimpleNamespace(text_channels=[
            SimpleNamespace(id=10, type=discord.ChannelType.text),
            SimpleNamespace(id=11, type=discord.ChannelType.news)])
        self.assertEqual(self.module.text_channel_ids(bot, "1"), {"10"})
        bot.get_guild.return_value = None
        self.assertFalse(self.module.text_channel_ids(bot, "1"))

    async def test_cross_channel_fallback_links_first_share_without_reply(self):
        from discord_bot.features.social_preview import duplicate_patrol as patrol
        from tests.test_social_duplicate_service import event
        source = SimpleNamespace(channel=SimpleNamespace(id=11, send=AsyncMock()), author=SimpleNamespace(id=42))
        with patch.object(patrol, "send_patrol_webhook", AsyncMock(return_value=False)):
            result = await patrol._announce(source, event())
        self.assertEqual(result.method, "bot_notice")
        self.assertNotIn("reference", source.channel.send.await_args.kwargs)
        self.assertIn("https://discord.com/channels/1/10/1100", source.channel.send.await_args.args[0])
        self.assertIn("<#10>", source.channel.send.await_args.args[0])
