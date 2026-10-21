import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from tests.support import isolated_discord_suite
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview import duplicate_webhook as relay
from tests.test_social_duplicate_patrol import message, previous

load_tests = isolated_discord_suite(__name__)


def webhook(owner=42, *, name=relay.PATROL_WEBHOOK_NAME):
    return SimpleNamespace(name=name, type=discord.WebhookType.incoming,
                           user=SimpleNamespace(id=owner) if owner is not None else None, send=AsyncMock())


class FakeChannel:
    def __init__(self):
        self.id = 10
        self.webhooks = AsyncMock(return_value=[])
        self.create_webhook = AsyncMock(return_value=webhook())
        self.send = AsyncMock()
        self.manage_webhooks = True

    def permissions_for(self, me):
        return SimpleNamespace(manage_webhooks=self.manage_webhooks)


class FakeThread:
    def __init__(self, parent):
        self.id = 20
        self.parent = parent
        self.send = AsyncMock()


class DuplicateWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = FakeChannel()
        self.source = message()
        self.source.channel = self.channel
        self.source.guild.me = SimpleNamespace(id=42)
        relay._avatar_bytes.cache_clear()
        for name, value in (("TextChannel", FakeChannel), ("Thread", FakeThread)):
            patcher = patch.object(relay.discord, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_fixed_name_avatar_and_jump_link_only_ping_current_sharer(self):
        await patrol._announce(self.source, previous())
        self.channel.create_webhook.assert_awaited_once()
        creation = self.channel.create_webhook.await_args.kwargs
        self.assertEqual(creation["avatar"], relay.PATROL_AVATAR_PATH.read_bytes())
        self.assertTrue(creation["avatar"].startswith(b"\xff\xd8\xff"))
        sent = self.channel.create_webhook.return_value.send.await_args
        self.assertEqual(sent.kwargs["username"], "不看訊息警察")
        self.assertEqual(sent.args[0],
            "🚨 逼逼！<@42>！你已經被不看訊息警察逮捕了，逼波逼波逼波。\n"
            "[查看原始預覽](https://discord.com/channels/1/10/1100)")
        self.assertTrue(sent.kwargs["wait"])
        self.assertTrue(sent.kwargs["suppress_embeds"])
        self.assertNotIn("reference", sent.kwargs)
        self.assertNotIn("avatar_url", sent.kwargs)  # Uses the bundled avatar uploaded at creation.
        from discord.http import handle_message_parameters
        with handle_message_parameters(content=sent.args[0], username=sent.kwargs["username"],
                                       allowed_mentions=sent.kwargs["allowed_mentions"]) as params:
            self.assertEqual(params.payload["username"], "不看訊息警察")
            self.assertEqual(params.payload["allowed_mentions"], {"parse": [], "users": [42]})
        self.channel.send.assert_not_awaited()

    async def test_only_reuses_bot_owned_incoming_patrol_webhook(self):
        other_bot = webhook(99)
        no_owner = webhook(None)
        preview = webhook()
        preview.name = "digest-preview-relay"
        owned = webhook()
        self.channel.webhooks.return_value = [other_bot, no_owner, preview, owned]
        await patrol._announce(self.source, previous())
        owned.send.assert_awaited_once()
        self.channel.create_webhook.assert_not_awaited()
        for other in (other_bot, no_owner, preview):
            other.send.assert_not_awaited()

    async def test_thread_uses_parent_webhook_but_sends_to_current_thread(self):
        thread = FakeThread(self.channel)
        self.source.channel = thread
        await patrol._announce(self.source, previous())
        kwargs = self.channel.create_webhook.return_value.send.await_args.kwargs
        self.assertIs(kwargs["thread"], thread)
        thread.send.assert_not_awaited()

    async def test_third_and_later_shares_create_police_with_second_asset(self):
        await patrol._announce(self.source, previous(), share_count=3)
        creation = self.channel.create_webhook.await_args.kwargs
        self.assertEqual(creation["name"], relay.REPEAT_WEBHOOK_NAME)
        self.assertEqual(creation["avatar"], relay.REPEAT_AVATAR_PATH.read_bytes())
        self.assertNotEqual(creation["avatar"], relay.PATROL_AVATAR_PATH.read_bytes())
        self.assertEqual(self.channel.create_webhook.return_value.send.await_args.kwargs["username"], "不看訊息警察")

    async def test_two_stable_webhooks_do_not_change_each_other_or_other_post_avatars(self):
        dog = webhook()
        police = webhook(name=relay.REPEAT_WEBHOOK_NAME)
        self.channel.webhooks.return_value = [dog, police]
        # A popular link escalates. Another link's first duplicate must still be dog.
        await patrol._announce(self.source, previous(), share_count=3)
        await patrol._announce(self.source, previous(), share_count=2)
        await patrol._announce(self.source, previous(), share_count=4)
        self.assertEqual(police.send.await_count, 2)
        dog.send.assert_awaited_once()
        self.channel.create_webhook.assert_not_awaited()

    async def test_existing_dog_webhook_is_not_used_as_repeat_police(self):
        dog = webhook()
        another_bot_police = webhook(owner=99, name=relay.REPEAT_WEBHOOK_NAME)
        self.channel.webhooks.return_value = [dog, another_bot_police]
        await patrol._announce(self.source, previous(), share_count=3)
        self.assertEqual(self.channel.create_webhook.await_args.kwargs["name"], relay.REPEAT_WEBHOOK_NAME)
        dog.send.assert_not_awaited()
        another_bot_police.send.assert_not_awaited()

    async def test_missing_permissions_falls_back_to_original_bot_reply(self):
        self.channel.manage_webhooks = False
        await patrol._announce(self.source, previous())
        self.channel.webhooks.assert_not_awaited()
        self.channel.create_webhook.assert_not_awaited()
        self.assertEqual(self.channel.send.await_args.kwargs["reference"].message_id, 1100)

    async def test_webhook_send_failure_falls_back_without_affecting_preview(self):
        self.channel.create_webhook.return_value.send.side_effect = RuntimeError("synthetic failure")
        await patrol._announce(self.source, previous())
        self.channel.send.assert_awaited_once()
        self.assertIn("reference", self.channel.send.await_args.kwargs)

    async def test_missing_bundled_avatar_falls_back_without_creating_webhook(self):
        with patch.object(relay, "_avatar_bytes", side_effect=FileNotFoundError("synthetic path")):
            await patrol._announce(self.source, previous())
        self.channel.create_webhook.assert_not_awaited()
        self.channel.send.assert_awaited_once()


class AvatarSdkTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_encodes_bundled_jpeg_for_create_webhook(self):
        from discord.channel import TextChannel
        state = SimpleNamespace(http=SimpleNamespace(create_webhook=AsyncMock(return_value={
            "id": "100", "type": 1, "name": relay.PATROL_WEBHOOK_NAME})))
        channel = SimpleNamespace(id=10, _state=state)
        for path in (relay.PATROL_AVATAR_PATH, relay.REPEAT_AVATAR_PATH):
            with self.subTest(asset=path.name), patch.object(discord.Webhook, "from_state", return_value=object()):
                await TextChannel.create_webhook(channel, name=relay.PATROL_WEBHOOK_NAME, avatar=path.read_bytes())
            avatar = state.http.create_webhook.await_args.kwargs["avatar"]
            self.assertTrue(avatar.startswith("data:image/jpeg;base64,"))
            self.assertEqual(base64.b64decode(avatar.split(",", 1)[1]), path.read_bytes())
