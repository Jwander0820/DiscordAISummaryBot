import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord_bot.features.social_preview import sender
from tests.support import isolated_discord_suite

load_tests = isolated_discord_suite(__name__)


def webhook(creator_id=42, *, state=None):
    # Use the real SDK object: discord.py 2.7.1 does not expose application_id,
    # even when that field is present in the API response.
    data = {
        "id": "100", "type": 1, "name": sender.PREVIEW_WEBHOOK_NAME,
        "application_id": "42", "token": "test-only-webhook-token",
    }
    if creator_id is not None:
        data["user"] = {
            "id": str(creator_id), "username": "test-bot", "discriminator": "0", "avatar": None, "bot": True,
        }
    return discord.Webhook(data, session=SimpleNamespace(), state=state)


class FakeChannel:
    def __init__(self):
        self.allow_webhooks = True
        self.webhooks = AsyncMock(return_value=[])
        self.create_webhook = AsyncMock(return_value=webhook())
        self.send = AsyncMock(return_value=SimpleNamespace(id=200))

    def permissions_for(self, me):
        return SimpleNamespace(manage_webhooks=self.allow_webhooks)


class FakeThread:
    def __init__(self, parent):
        self.id = 301
        self.parent = parent
        self.send = AsyncMock(return_value=SimpleNamespace(id=201))


class PreviewSenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from discord_bot.features.social_preview.preview_view import build_social_preview_views

        self.channel = FakeChannel()
        self.message = SimpleNamespace(
            channel=self.channel, guild=SimpleNamespace(me=SimpleNamespace(id=42)),
            author=SimpleNamespace(display_name="原作者", display_avatar=SimpleNamespace(url="https://example.com/avatar.png")),
            _state=SimpleNamespace(application_id=42),
        )
        self.view, = build_social_preview_views(
            original_url="https://www.threads.com/@demo/post/Video001",
            video_url="https://scontent.cdninstagram.com/first.mp4", title="Threads", author="demo",
            text="正文", commentary="留言", spoiler=False, timeout=None,
        )
        self.old_view = discord.ui.View(timeout=None)
        self.embed = discord.Embed(title="圖片或純文字")
        for name, replacement in (("TextChannel", FakeChannel), ("Thread", FakeThread)):
            patcher = patch.object(discord, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send_patch = patch.object(discord.Webhook, "send", autospec=True)
        self.webhook_send = self.send_patch.start()
        self.webhook_send.return_value = SimpleNamespace(id=100)
        self.addCleanup(self.send_patch.stop)

    async def test_v2_webhook_keeps_author_view_without_uploads_legacy_fields_or_pings(self):
        result = await sender.send_preview_as_author(self.message, view=self.view)
        self.assertIs(result, self.webhook_send.return_value)
        kwargs = self.webhook_send.await_args.kwargs
        self.assertFalse(set(kwargs) & {"content", "embed", "files"})
        self.assertIs(kwargs["view"], self.view)
        self.assertEqual(kwargs["username"], "原作者")
        self.assertEqual(kwargs["avatar_url"], "https://example.com/avatar.png")
        self.assertTrue(kwargs["wait"])
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.channel.create_webhook.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_v2_reuses_real_sdk_webhook_without_application_id_warning(self):
        own_hook = webhook()
        self.channel.webhooks.return_value = [own_hook]
        self.message._state = SimpleNamespace()
        with self.assertNoLogs("discord_digest_bot", level="WARNING"):
            result = await sender.send_preview_as_author(self.message, view=self.view)
        self.assertIs(result, self.webhook_send.return_value)
        self.channel.create_webhook.assert_not_awaited()
        self.webhook_send.assert_awaited_once()
        self.assertIs(self.webhook_send.await_args.args[0], own_hook)
        self.channel.send.assert_not_awaited()

    async def test_v2_thread_uses_parent_webhook_and_keeps_thread_destination(self):
        thread = FakeThread(self.channel)
        self.message.channel = thread
        await sender.send_preview_as_author(self.message, view=self.view)
        self.assertIs(self.webhook_send.await_args.kwargs["view"], self.view)
        self.assertIs(self.webhook_send.await_args.kwargs["thread"], thread)
        thread.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_v2_uses_bot_when_webhooks_are_unavailable(self):
        for scenario in ("missing-permission", "direct-message"):
            with self.subTest(scenario=scenario):
                self.channel.send.reset_mock()
                self.channel.allow_webhooks = scenario != "missing-permission"
                self.message.guild = None if scenario == "direct-message" else SimpleNamespace(me=SimpleNamespace(id=42))
                await sender.send_preview_as_author(self.message, view=self.view)
                self.channel.send.assert_awaited_once()
                kwargs = self.channel.send.await_args.kwargs
                self.assertEqual(set(kwargs), {"view", "allowed_mentions"})
                self.assertIs(kwargs["view"], self.view)
                self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
                self.channel.webhooks.assert_not_awaited()
                self.channel.create_webhook.assert_not_awaited()

    async def test_v2_foreign_or_unknown_creator_webhook_gets_bot_created_replacement(self):
        for creator_id in (99, None):
            with self.subTest(creator_id=creator_id):
                self.channel.create_webhook.reset_mock()
                self.webhook_send.reset_mock()
                self.channel.webhooks.return_value = [webhook(creator_id)]
                await sender.send_preview_as_author(self.message, view=self.view)
                self.channel.create_webhook.assert_awaited_once()
                self.webhook_send.assert_awaited_once()
                self.assertIs(self.webhook_send.await_args.args[0], self.channel.create_webhook.return_value)
                self.channel.send.assert_not_awaited()

    async def test_v2_reuses_own_hook_even_when_foreign_same_name_is_first(self):
        own_hook = webhook()
        self.channel.webhooks.return_value = [webhook(99), own_hook]
        await sender.send_preview_as_author(self.message, view=self.view)
        self.channel.create_webhook.assert_not_awaited()
        self.webhook_send.assert_awaited_once()
        self.assertIs(self.webhook_send.await_args.args[0], own_hook)
        self.channel.send.assert_not_awaited()

    async def test_v2_api_failures_fall_back_to_bot_with_same_view(self):
        for stage in ("list", "create", "send"):
            with self.subTest(stage=stage):
                self.channel.send.reset_mock()
                self.channel.webhooks.side_effect = None
                self.channel.create_webhook.side_effect = None
                self.webhook_send.side_effect = None
                failure = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
                if stage == "list":
                    self.channel.webhooks.side_effect = failure
                elif stage == "create":
                    self.channel.create_webhook.side_effect = failure
                else:
                    self.webhook_send.side_effect = failure
                with patch.object(sender.logger, "warning"):
                    await sender.send_preview_as_author(self.message, view=self.view)
                self.channel.send.assert_awaited_once()
                self.assertIs(self.channel.send.await_args.kwargs["view"], self.view)

    async def test_real_sdk_webhook_send_serializes_v2_and_registers_interactive_view(self):
        from discord.webhook.async_ import async_context

        self.send_patch.stop()
        state = SimpleNamespace(allowed_mentions=discord.AllowedMentions.none(), store_view=Mock())
        self.channel.webhooks.return_value = [webhook(state=state)]
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        adapter_token = async_context.set(adapter)
        try:
            sent = SimpleNamespace(id=123)
            with patch.object(discord.Webhook, "_create_message", return_value=sent):
                result = await sender.send_preview_as_author(self.message, view=self.view)
        finally:
            async_context.reset(adapter_token)

        self.assertIs(result, sent)
        kwargs = adapter.execute_webhook.await_args.kwargs
        self.assertTrue(kwargs["wait"])
        self.assertTrue(kwargs["with_components"])
        self.assertFalse(kwargs["files"])
        self.assertFalse(kwargs["multipart"])
        payload = kwargs["payload"]
        self.assertEqual(payload["flags"] & (1 << 15), 1 << 15)
        self.assertEqual(payload["username"], "原作者")
        self.assertEqual(payload["avatar_url"], "https://example.com/avatar.png")
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertFalse(set(payload) & {"content", "embeds", "attachments"})
        self.assertTrue(payload["components"])
        state.store_view.assert_called_once_with(self.view, sent.id)
        self.channel.send.assert_not_awaited()

    async def test_real_sdk_webhook_uploads_gallery_images_with_v2_multipart(self):
        from discord.webhook.async_ import async_context
        from discord_bot.features.social_preview.preview_view import build_social_preview_views

        self.send_patch.stop()
        files = [discord.File(io.BytesIO(b"image bytes"), filename=f"image_{i}.jpg", spoiler=True) for i in range(4)]
        view, = build_social_preview_views(
            original_url="https://www.facebook.com/post/123", title="Facebook", author="作者",
            text="正文", commentary="留言", spoiler=True, timeout=None, files=files,
        )
        state = SimpleNamespace(allowed_mentions=discord.AllowedMentions.none(), store_view=Mock())
        self.channel.webhooks.return_value = [webhook(state=state)]
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        token = async_context.set(adapter)
        try:
            with patch.object(discord.Webhook, "_create_message", return_value=SimpleNamespace(id=123)):
                await sender.send_preview_group(self.message, [view])
        finally:
            async_context.reset(token)

        kwargs = adapter.execute_webhook.await_args.kwargs
        payload = json.loads(next(part["value"] for part in kwargs["multipart"] if part["name"] == "payload_json"))
        self.assertTrue(kwargs["with_components"])
        self.assertEqual(payload["flags"] & (1 << 15), 1 << 15)
        self.assertFalse(set(payload) & {"content", "embeds"})
        self.assertEqual(payload["allowed_mentions"]["parse"], [])
        self.assertEqual([item["filename"] for item in payload["attachments"]], [file.filename for file in files])
        card = next(item for item in payload["components"] if item["type"] == 17)
        self.assertTrue(card["spoiler"])
        gallery = next(item for item in card["components"] if item["type"] == 12)
        self.assertEqual([item["media"]["url"] for item in gallery["items"]], [f"attachment://{file.filename}" for file in files])
        self.assertTrue(all(item["spoiler"] for item in gallery["items"]))
        self.assertEqual(view.files, [])  # Live buttons must not retain image buffers.
        self.channel.send.assert_not_awaited()
        state.store_view.assert_called_once_with(view, 123)

    async def test_v2_webhook_failure_resends_complete_spoiler_attachment_via_bot(self):
        from discord_bot.features.social_preview.preview_view import build_social_preview_views

        data = b"complete image bytes"
        file = discord.File(io.BytesIO(data), filename="image.jpg", spoiler=True)
        view, = build_social_preview_views(
            original_url="https://www.facebook.com/post/123", title="Facebook", author="作者",
            text="正文", commentary="留言", spoiler=True, timeout=None, files=[file],
        )

        async def failed_webhook(hook, **kwargs):
            kwargs["files"][0].fp.read()
            kwargs["files"][0].close()
            raise RuntimeError("upload failed after reading the stream")

        self.webhook_send.side_effect = failed_webhook
        with self.assertLogs("discord_digest_bot", level="WARNING"):
            await sender.send_preview_group(self.message, [view])
        kwargs = self.channel.send.await_args.kwargs
        fallback = kwargs["files"][0]
        self.assertIsNot(fallback, file)
        self.assertEqual(fallback.fp.read(), data)
        self.assertEqual(fallback.filename, "SPOILER_image.jpg")
        self.assertTrue(fallback.spoiler)
        self.assertIs(kwargs["view"], view)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertEqual(view.files, [])

    async def test_v2_rejects_mixed_legacy_fields_before_sending(self):
        for fields in ({"content": "text"}, {"embed": discord.Embed(title="old")}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(ValueError):
                    await sender.send_preview_as_author(self.message, view=self.view, **fields)
        self.channel.webhooks.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_legacy_embed_keeps_real_sdk_webhook_author_content_and_mentions(self):
        self.channel.webhooks.return_value = [webhook()]
        result = await sender.send_preview_as_author(
            self.message, content="留言", embed=self.embed, view=self.old_view,
        )
        self.assertIs(result, self.webhook_send.return_value)
        kwargs = self.webhook_send.await_args.kwargs
        self.assertEqual(kwargs["username"], "原作者")
        self.assertEqual(kwargs["avatar_url"], "https://example.com/avatar.png")
        self.assertEqual(kwargs["content"], "留言")
        self.assertIs(kwargs["embed"], self.embed)
        self.assertIs(kwargs["view"], self.old_view)
        self.assertTrue(kwargs["wait"])
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.channel.create_webhook.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_legacy_thread_uses_parent_webhook_and_keeps_thread_destination(self):
        self.channel.webhooks.return_value = [webhook()]
        thread = FakeThread(self.channel)
        self.message.channel = thread
        await sender.send_preview_as_author(self.message, embed=self.embed, view=self.old_view)
        self.assertIs(self.webhook_send.await_args.kwargs["thread"], thread)
        thread.send.assert_not_awaited()

    async def test_legacy_no_webhook_permission_falls_back_to_bot(self):
        self.channel.allow_webhooks = False
        await sender.send_preview_as_author(self.message, embed=self.embed, view=self.old_view)
        self.channel.webhooks.assert_not_awaited()
        self.assertIs(self.channel.send.await_args.kwargs["embed"], self.embed)
        self.assertEqual(self.channel.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_legacy_webhook_send_error_falls_back_to_bot(self):
        self.channel.webhooks.return_value = [webhook()]
        self.webhook_send.side_effect = RuntimeError("simulated webhook failure")
        with self.assertLogs("discord_digest_bot", level="WARNING"):
            await sender.send_preview_as_author(self.message, embed=self.embed, view=self.old_view)
        self.assertIs(self.channel.send.await_args.kwargs["embed"], self.embed)

    async def test_legacy_creates_webhook_when_none_exists(self):
        await sender.send_preview_as_author(self.message, embed=self.embed, view=self.old_view)
        self.channel.create_webhook.assert_awaited_once_with(
            name=sender.PREVIEW_WEBHOOK_NAME, reason="Relay social preview",
        )
        self.webhook_send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_legacy_creation_denied_falls_back_to_bot(self):
        self.channel.create_webhook.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "denied",
        )
        await sender.send_preview_as_author(self.message, embed=self.embed, view=self.old_view)
        self.channel.send.assert_awaited_once()

    async def test_source_cleanup_suppresses_original_when_deletion_is_forbidden(self):
        self.message.delete = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied"))
        self.message.edit = AsyncMock()
        await sender.cleanup_source_message(self.message, platform="Threads", url="https://www.threads.com/@demo/post/Video001")
        self.message.edit.assert_awaited_once_with(suppress=True)
