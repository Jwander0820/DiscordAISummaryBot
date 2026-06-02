import importlib
import sys
import types
import unittest
from unittest.mock import Mock

from tests.support import install_discord_stub


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, *, ephemeral=False):
        self.messages.append({"content": content, "ephemeral": ephemeral})


class FakeInteraction:
    def __init__(self, *, manage_guild=True, guild_id=123, user_id=42):
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.user = types.SimpleNamespace(
            id=user_id,
            guild_permissions=types.SimpleNamespace(manage_guild=manage_guild),
        )
        self.response = FakeResponse()


class SocialPreviewSettingsCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_discord_stub()
        sys.modules.pop("discord_bot.cogs.social_preview_settings_cog", None)
        self.cog_module = importlib.import_module("discord_bot.cogs.social_preview_settings_cog")
        self.cog = self.cog_module.SocialPreviewSettingsCog(bot=object())

    async def test_non_manager_cannot_modify_settings(self):
        service = Mock()
        self.cog_module.social_preview_settings_service = service
        interaction = FakeInteraction(manage_guild=False)

        await self.cog.configure_social_preview(
            interaction,
            types.SimpleNamespace(value="threads"),
            types.SimpleNamespace(value="disabled"),
        )

        service.set_override.assert_not_called()
        self.assertTrue(interaction.response.messages[-1]["ephemeral"])
        self.assertIn("管理伺服器", interaction.response.messages[-1]["content"])

    async def test_manager_can_disable_single_platform(self):
        service = Mock()
        service.list_statuses.return_value = {
            "threads": self.cog_module.SocialPreviewSettingStatus("threads", True, False, False, "guild_override"),
            "facebook": self.cog_module.SocialPreviewSettingStatus("facebook", True, None, True, "global_default"),
            "instagram": self.cog_module.SocialPreviewSettingStatus("instagram", False, None, False, "global_default"),
        }
        self.cog_module.social_preview_settings_service = service
        interaction = FakeInteraction(manage_guild=True)

        await self.cog.configure_social_preview(
            interaction,
            types.SimpleNamespace(value="threads"),
            types.SimpleNamespace(value="disabled"),
        )

        service.set_override.assert_called_once_with("123", "threads", False, updated_by="42")
        self.assertTrue(interaction.response.messages[-1]["ephemeral"])
        self.assertIn("Threads: 停用", interaction.response.messages[-1]["content"])

    async def test_default_state_clears_override(self):
        service = Mock()
        service.list_statuses.return_value = {
            "threads": self.cog_module.SocialPreviewSettingStatus("threads", True, None, True, "global_default"),
            "facebook": self.cog_module.SocialPreviewSettingStatus("facebook", True, None, True, "global_default"),
            "instagram": self.cog_module.SocialPreviewSettingStatus("instagram", False, None, False, "global_default"),
        }
        self.cog_module.social_preview_settings_service = service
        interaction = FakeInteraction(manage_guild=True)

        await self.cog.configure_social_preview(
            interaction,
            types.SimpleNamespace(value="facebook"),
            types.SimpleNamespace(value="default"),
        )

        service.clear_override.assert_called_once_with("123", "facebook")
        service.set_override.assert_not_called()

    async def test_manager_can_enable_instagram(self):
        service = Mock()
        service.list_statuses.return_value = {
            "threads": self.cog_module.SocialPreviewSettingStatus("threads", False, None, False, "global_default"),
            "facebook": self.cog_module.SocialPreviewSettingStatus("facebook", False, None, False, "global_default"),
            "instagram": self.cog_module.SocialPreviewSettingStatus("instagram", False, True, True, "guild_override"),
        }
        self.cog_module.social_preview_settings_service = service
        interaction = FakeInteraction(manage_guild=True)

        await self.cog.configure_social_preview(
            interaction,
            types.SimpleNamespace(value="instagram"),
            types.SimpleNamespace(value="enabled"),
        )

        service.set_override.assert_called_once_with("123", "instagram", True, updated_by="42")
        self.assertTrue(interaction.response.messages[-1]["ephemeral"])
        self.assertIn("Instagram", interaction.response.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
