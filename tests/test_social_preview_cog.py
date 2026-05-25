import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock

from tests.support import install_discord_stub


class FakeAuthor:
    def __init__(self, *, bot=False):
        self.bot = bot


class FakeMessage:
    def __init__(self, content, *, bot_author=False, guild_id=123):
        self.content = content
        self.author = FakeAuthor(bot=bot_author)
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None


class SocialPreviewCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_discord_stub()
        self.original_modules = {
            name: sys.modules.get(name)
            for name in (
                "discord_bot.features.social_preview.threads_preview",
                "discord_bot.features.social_preview.facebook_preview",
                "discord_bot.cogs.social_preview_cog",
            )
        }

        self.threads_stub = types.ModuleType("discord_bot.features.social_preview.threads_preview")
        self.threads_stub.extract_threads_urls = Mock(return_value=[])
        self.threads_stub.handle_threads_in_message = AsyncMock(return_value=False)

        self.facebook_stub = types.ModuleType("discord_bot.features.social_preview.facebook_preview")
        self.facebook_stub.extract_facebook_urls = Mock(return_value=[])
        self.facebook_stub.handle_facebook_in_message = AsyncMock(return_value=False)

        sys.modules["discord_bot.features.social_preview.threads_preview"] = self.threads_stub
        sys.modules["discord_bot.features.social_preview.facebook_preview"] = self.facebook_stub
        sys.modules.pop("discord_bot.cogs.social_preview_cog", None)
        self.cog_module = importlib.import_module("discord_bot.cogs.social_preview_cog")
        self.cog = self.cog_module.SocialPreviewCog(bot=object())

    def tearDown(self):
        for name, module in self.original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_bot_author_message_is_ignored_before_settings_lookup(self):
        self.cog_module.is_social_preview_enabled = Mock(return_value=True)

        await self.cog.on_message(FakeMessage("https://facebook.com/demo", bot_author=True))

        self.cog_module.is_social_preview_enabled.assert_not_called()
        self.threads_stub.extract_threads_urls.assert_not_called()
        self.facebook_stub.extract_facebook_urls.assert_not_called()

    async def test_both_disabled_skips_url_extraction(self):
        self.cog_module.is_social_preview_enabled = Mock(return_value=False)

        await self.cog.on_message(FakeMessage("https://facebook.com/demo"))

        self.assertEqual(self.cog_module.is_social_preview_enabled.call_count, 2)
        self.threads_stub.extract_threads_urls.assert_not_called()
        self.facebook_stub.extract_facebook_urls.assert_not_called()

    async def test_threads_disabled_facebook_enabled_only_handles_facebook(self):
        def enabled(_guild_id, platform):
            return platform == "facebook"

        self.cog_module.is_social_preview_enabled = Mock(side_effect=enabled)
        self.facebook_stub.extract_facebook_urls.return_value = ["https://facebook.com/demo"]
        self.facebook_stub.handle_facebook_in_message.return_value = True

        await self.cog.on_message(FakeMessage("https://facebook.com/demo"))

        self.threads_stub.extract_threads_urls.assert_not_called()
        self.facebook_stub.extract_facebook_urls.assert_called_once()
        self.threads_stub.handle_threads_in_message.assert_not_awaited()
        self.facebook_stub.handle_facebook_in_message.assert_awaited_once()

    async def test_threads_handler_short_circuits_facebook(self):
        self.cog_module.is_social_preview_enabled = Mock(return_value=True)
        self.threads_stub.extract_threads_urls.return_value = ["https://threads.com/@demo/post/abc"]
        self.facebook_stub.extract_facebook_urls.return_value = ["https://facebook.com/demo"]
        self.threads_stub.handle_threads_in_message.return_value = True

        await self.cog.on_message(FakeMessage("https://threads.com/@demo/post/abc https://facebook.com/demo"))

        self.threads_stub.handle_threads_in_message.assert_awaited_once()
        self.facebook_stub.handle_facebook_in_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
