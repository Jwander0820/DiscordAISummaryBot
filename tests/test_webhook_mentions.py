import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import install_discord_stub

install_discord_stub()

# Some isolated test modules install a minimal dotenv stub without set_key.
import dotenv

if not hasattr(dotenv, "set_key"):
    dotenv.set_key = lambda *_args, **_kwargs: None

from discord_bot.cogs.conversation_cog import ConversationCog, _allowed_mentions_for_requester
from discord_bot.cogs.fun_cog import FunCog


class FakeChannel:
    def __init__(self, webhook):
        self.webhook = webhook

    async def webhooks(self):
        return [self.webhook]

    async def create_webhook(self, **_kwargs):
        raise AssertionError("existing webhook should be reused")


class WebhookMentionSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_mentions_suppressed(self, cog_class, webhook_name):
        marker = object()
        webhook = types.SimpleNamespace(name=webhook_name, send=AsyncMock())
        channel = FakeChannel(webhook)
        cog = cog_class(None)

        with patch(
            f"{cog_class.__module__}.discord.AllowedMentions.none",
            return_value=marker,
        ):
            await cog._send_with_webhook(
                channel,
                "@everyone test",
                "tester",
                "https://example.com/avatar.png",
                webhook_name,
            )

        self.assertIs(webhook.send.await_args.kwargs["allowed_mentions"], marker)

    async def test_deepfaker_webhook_suppresses_mentions(self):
        await self._assert_mentions_suppressed(FunCog, "DeepFaker")

    async def test_amadeus_webhook_suppresses_mentions(self):
        await self._assert_mentions_suppressed(ConversationCog, "Amadeus")


class RequesterMentionTests(unittest.IsolatedAsyncioTestCase):
    def test_requester_allowlist_excludes_everyone_roles_and_other_users(self):
        requester = object()

        allowed_mentions = _allowed_mentions_for_requester(requester)

        self.assertFalse(allowed_mentions.everyone)
        self.assertEqual(allowed_mentions.users, [requester])
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.replied_user)

    async def test_followup_notifies_only_requester(self):
        requester = object()
        interaction = types.SimpleNamespace(
            user=requester,
            followup=types.SimpleNamespace(send=AsyncMock()),
        )
        cog = ConversationCog(None)

        await cog._send_requester_followup(interaction, "result")

        allowed_mentions = interaction.followup.send.await_args.kwargs["allowed_mentions"]
        self.assertEqual(allowed_mentions.users, [requester])
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.roles)

    async def test_amadeus_notifies_only_requester(self):
        requester = object()
        cog = ConversationCog(None)
        cog._send_with_webhook = AsyncMock()

        await cog._send_as_amadeus(object(), "result", requester)

        allowed_mentions = cog._send_with_webhook.await_args.kwargs["allowed_mentions"]
        self.assertEqual(allowed_mentions.users, [requester])
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.roles)


if __name__ == "__main__":
    unittest.main()
