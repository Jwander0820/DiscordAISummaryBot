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
        for name, value in (("TextChannel", FakeChannel), ("Thread", FakeThread)):
            patcher = patch.object(relay.discord, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_first_patrol_real_sdk_payload(self):
        await patrol._announce(self.source, previous())
        creation = self.channel.create_webhook.await_args.kwargs
        self.assertEqual(creation["name"], relay.PATROL_WEBHOOK_NAME)
        self.assertNotIn("avatar", creation)
        sent = self.channel.create_webhook.return_value.send.await_args
        self.assertEqual(sent.args, ())
        self.assertEqual(sent.kwargs["avatar_url"], relay.PATROL_AVATAR_URL)
        self.assertTrue(sent.kwargs["wait"])
        self.assertNotIn("suppress_embeds", sent.kwargs)
        from discord.http import handle_message_parameters
        with handle_message_parameters(view=sent.kwargs["view"], username=sent.kwargs["username"],
                                       avatar_url=sent.kwargs["avatar_url"],
                                       allowed_mentions=sent.kwargs["allowed_mentions"]) as params:
            payload = params.payload
            self.assertEqual(payload["username"], "不看訊息警察")
            self.assertTrue(payload["flags"] & 32768)
            self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": [42]})
            self.assertNotIn("content", payload)
            self.assertNotIn("embeds", payload)
            card = payload["components"][0]
            self.assertEqual(card["type"], 17)
            self.assertEqual(card["accent_color"], 0xFF6600)
            self.assertEqual(len(card["components"]), 1)
            section = card["components"][0]
            self.assertEqual(section["type"], 9)
            self.assertEqual(section["accessory"]["media"]["url"], relay.PATROL_GIF_URL)
            self.assertEqual(section["components"][0]["content"], "**不看訊息警察**")
            self.assertEqual(section["components"][1]["content"],
                             "🚨 逼逼！<@42>！你已經被不看訊息警察逮捕了，逼波逼波逼波。")
            self.assertEqual(section["components"][2]["content"],
                             "⬆️ [查看原始連結](https://discord.com/channels/1/10/1100)")
        self.channel.send.assert_not_awaited()

    async def test_only_reuses_bot_owned_incoming_patrol_webhook(self):
        others = [webhook(99), webhook(None), webhook(name="digest-preview-relay")]
        owned = webhook()
        self.channel.webhooks.return_value = others + [owned]
        await patrol._announce(self.source, previous())
        owned.send.assert_awaited_once()
        self.assertEqual(owned.send.await_args.kwargs["avatar_url"], relay.PATROL_AVATAR_URL)
        self.channel.create_webhook.assert_not_awaited()
        for other in others:
            other.send.assert_not_awaited()

    async def test_thread_routes_through_parent(self):
        thread = FakeThread(self.channel)
        self.source.channel = thread
        await patrol._announce(self.source, previous())
        self.assertIs(self.channel.create_webhook.return_value.send.await_args.kwargs["thread"], thread)
        thread.send.assert_not_awaited()

    async def test_all_four_social_platforms_reach_the_same_v2_webhook(self):
        from discord_bot.features.social_preview.duplicate_service import DuplicateMatch

        cases = (
            ("threads", "https://www.threads.com/@demo/post/Demo001"),
            ("instagram", "https://www.instagram.com/p/Demo001/"),
            ("facebook", "https://www.facebook.com/demo/posts/123"),
            ("x", "https://x.com/demo/status/123"),
        )
        for platform, url in cases:
            with self.subTest(platform=platform):
                service = SimpleNamespace(register=AsyncMock(return_value=DuplicateMatch(previous(), 2)))
                hook = webhook()
                self.channel.webhooks.return_value = [hook]
                with patch.object(patrol, "social_duplicate_service", service):
                    if platform == "x":
                        await patrol.record_original_share(self.source, platform=platform, url=url)
                    else:
                        await patrol.record_delivered_preview(self.source, platform=platform, url=url,
                                                               sent_messages=[SimpleNamespace(id=1200)])
                self.assertEqual(service.register.await_args.args[0].platform, platform)
                hook.send.assert_awaited_once()
                sent = hook.send.await_args.kwargs
                self.assertEqual(sent["avatar_url"], relay.PATROL_AVATAR_URL)
                card = sent["view"].to_components()[0]
                self.assertEqual(card["type"], 17)
                self.assertEqual(card["components"][0]["accessory"]["media"]["url"], relay.PATROL_GIF_URL)
                self.assertEqual(sent["allowed_mentions"].to_dict(), {"parse": [], "users": [42]})
                self.channel.send.assert_not_awaited()

    async def test_counts_preserve_per_post_avatar_and_gif(self):
        dog, police = webhook(), webhook(name=relay.REPEAT_WEBHOOK_NAME)
        self.channel.webhooks.return_value = [dog, police]
        expected = {3: "第 3 次了！三回啊三回！", 4: "第 4 次了！你是只看自己傳的訊息是不是！",
                    5: "第 5 次了！警察都認得這篇了，你還不認得！", 6: "第 6 次了……好，你開心就好。",
                    100: "第 100 次了……好，你開心就好。"}
        for count, suffix in expected.items():
            await patrol._announce(self.source, previous(), share_count=count)
            sent = police.send.await_args.kwargs
            section = sent["view"].to_components()[0]["components"][0]
            self.assertEqual(section["components"][2]["content"],
                             "⬆️ [查看原始連結](https://discord.com/channels/1/10/1100) " + suffix)
            self.assertEqual(section["accessory"]["media"]["url"], relay.PATROL_GIF_URL)
            self.assertEqual(sent["avatar_url"], relay.REPEAT_AVATAR_URL)
        await patrol._announce(self.source, previous(), share_count=2)
        dog.send.assert_awaited_once()
        self.assertEqual(dog.send.await_args.kwargs["avatar_url"], relay.PATROL_AVATAR_URL)
        self.channel.create_webhook.assert_not_awaited()

    async def test_existing_dog_is_not_reused_for_police(self):
        dog, other = webhook(), webhook(owner=99, name=relay.REPEAT_WEBHOOK_NAME)
        self.channel.webhooks.return_value = [dog, other]
        await patrol._announce(self.source, previous(), share_count=3)
        self.assertEqual(self.channel.create_webhook.await_args.kwargs["name"], relay.REPEAT_WEBHOOK_NAME)
        self.assertEqual(self.channel.create_webhook.return_value.send.await_args.kwargs["avatar_url"],
                         relay.REPEAT_AVATAR_URL)
        dog.send.assert_not_awaited()
        other.send.assert_not_awaited()

    async def test_missing_permissions_preserves_bot_reply_fallback(self):
        self.channel.manage_webhooks = False
        await patrol._announce(self.source, previous())
        self.channel.webhooks.assert_not_awaited()
        self.assertEqual(self.channel.send.await_args.kwargs["reference"].message_id, 1100)

    async def test_failed_webhook_preserves_bot_reply_fallback(self):
        self.channel.create_webhook.return_value.send.side_effect = RuntimeError("synthetic failure")
        await patrol._announce(self.source, previous())
        self.channel.send.assert_awaited_once()
        self.assertIn("reference", self.channel.send.await_args.kwargs)
