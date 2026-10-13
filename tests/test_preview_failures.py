import asyncio
import os
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from tests.support import isolated_discord_suite
from discord_bot.features.social_preview import facebook_preview, instagram_preview, threads_preview
from discord_bot.features.social_preview import failure_notifications as failures
from discord_bot.features.social_preview.facebook_fetch import FacebookMedia, FacebookPostData
from discord_bot.features.social_preview.instagram_fetch import InstagramMedia, InstagramPost
from discord_bot.features.social_preview.threads_fetch import ThreadsMedia, ThreadsPost

load_tests = isolated_discord_suite(__name__)

CASES = (
    ("Threads", "threads", threads_preview, threads_preview.PreviewResult, "https://www.threads.com/@demo/post/Example001"),
    ("Facebook", "facebook", facebook_preview, facebook_preview.FacebookPreview, "https://www.facebook.com/demo/posts/123"),
    ("Instagram", "instagram", instagram_preview, instagram_preview.InstagramPreview, "https://www.instagram.com/p/Example001"),
)


def source(url):
    return types.SimpleNamespace(
        author=types.SimpleNamespace(id=10, bot=False, nick="demo", global_name="demo"),
        channel=types.SimpleNamespace(id=20, name="general"),
        guild=types.SimpleNamespace(id=1), content=f"留言 ||{url}||", reply=AsyncMock(),
    )


def successful_preview(model, platform, url):
    embed = discord.Embed(title=platform, description="正常正文", url=url)
    embed.set_author(name="demo")
    return model(embed=embed, files=[])


class PreviewFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_botlog_is_cancelled_and_public_preview_still_completes(self):
        for platform, key, module, model, url in CASES:
            cancelled = asyncio.Event()

            async def stalled_notify(**kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            error_preview = successful_preview(model, platform, url)
            error_preview.embed.description = f"{platform}預覽失敗 爛meta"
            error_preview.preview_error = "no_content"
            with self.subTest(platform=platform), patch.object(
                module, f"build_{key}_preview", new=AsyncMock(return_value=error_preview)
            ), patch.object(failures, "forward_notify_to_channel", new=AsyncMock(side_effect=stalled_notify)) as notify, \
                    patch.object(failures, "NOTIFICATION_TIMEOUT_SECONDS", 0.01), \
                    patch.object(module, "send_preview_group", new_callable=AsyncMock) as send, \
                    patch.object(module, "cleanup_source_message", new_callable=AsyncMock) as cleanup, \
                    self.assertLogs("discord_digest_bot", level="WARNING") as logs:
                message = source(url)
                handled = await asyncio.wait_for(getattr(module, f"handle_{key}_in_message")(message), timeout=1)
                self.assertTrue(handled)
                self.assertTrue(cancelled.is_set())
                notify.assert_awaited_once()
                send.assert_awaited_once()
                cleanup.assert_awaited_once()
                message.reply.assert_not_awaited()
                self.assertIn("TimeoutError", " ".join(logs.output))

    async def test_unexpected_prepare_error_is_private_for_every_platform(self):
        for platform, key, module, model, url in CASES:
            with self.subTest(platform=platform), patch.object(
                module, f"build_{key}_preview", new=AsyncMock(side_effect=RuntimeError("sessionid=private-marker"))
            ), patch("discord_bot.features.social_preview.sender.send_preview_as_author", new=AsyncMock(return_value=types.SimpleNamespace(id=100))) as send, \
                    patch.object(module, "cleanup_source_message", new_callable=AsyncMock) as cleanup, \
                    patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify, \
                    self.assertLogs("discord_digest_bot", level="WARNING") as logs:
                message, bot = source(url), object()
                handled = await getattr(module, f"handle_{key}_in_message")(message, bot_client=bot)
                notify.assert_awaited_once()
                self.assertIs(notify.await_args.kwargs["bot"], bot)
                self.assertIn("prepare_failed", notify.await_args.kwargs["error"])
                self.assertIn("RuntimeError", notify.await_args.kwargs["record"]["summary"])
                if platform == "Facebook":
                    self.assertTrue(handled)
                    public = str(send.await_args.kwargs["view"].to_components())
                    cleanup.assert_awaited_once()
                else:
                    self.assertFalse(handled)
                    public = message.reply.await_args.args[0]
                    cleanup.assert_not_awaited()
                self.assertIn(f"||{platform}預覽失敗 爛meta||", public)
                self.assertNotIn("private-marker", public + str(notify.await_args) + " ".join(logs.output))

    async def test_delivery_error_reports_once_and_preserves_source_even_if_reply_fails(self):
        for platform, key, module, model, url in CASES:
            for reply_fails in (False, True):
                with self.subTest(platform=platform, reply_fails=reply_fails), patch.object(
                    module, f"build_{key}_preview", new=AsyncMock(return_value=successful_preview(model, platform, url))
                ), patch.object(module, "send_preview_group", new=AsyncMock(side_effect=RuntimeError("private-media-url"))), \
                        patch.object(module, "cleanup_source_message", new_callable=AsyncMock) as cleanup, \
                        patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify, \
                        self.assertLogs("discord_digest_bot", level="ERROR"):
                    message = source(url)
                    if reply_fails:
                        message.reply.side_effect = RuntimeError("reply blocked")
                    self.assertFalse(await getattr(module, f"handle_{key}_in_message")(message))
                    notify.assert_awaited_once()
                    self.assertIn("send_failed", notify.await_args.kwargs["error"])
                    self.assertNotIn("private-media-url", str(notify.await_args))
                    self.assertEqual(message.reply.await_args.args[0], f"||{platform}預覽失敗 爛meta||")
                    cleanup.assert_not_awaited()

    async def test_cleanup_exception_preserves_delivered_preview_without_failure_reply(self):
        for platform, key, module, model, url in CASES:
            with self.subTest(platform=platform), patch.object(
                module, f"build_{key}_preview", new=AsyncMock(return_value=successful_preview(model, platform, url))
            ), patch.object(module, "send_preview_group", new_callable=AsyncMock), \
                    patch.object(module, "cleanup_source_message", new=AsyncMock(side_effect=RuntimeError("cleanup failed"))), \
                    patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify, \
                    self.assertLogs("discord_digest_bot", level="ERROR"):
                message = source(url)
                self.assertTrue(await getattr(module, f"handle_{key}_in_message")(message))
                notify.assert_awaited_once()
                self.assertIn("cleanup_failed", notify.await_args.kwargs["error"])
                message.reply.assert_not_awaited()

    async def test_empty_content_and_unrenderable_media_generate_failure_card_and_notification(self):
        for platform, key, module, model, url in CASES:
            for media_fails in (False, True):
                media_url = "https://scontent.xx.fbcdn.net/photo.jpg"
                if platform == "Threads":
                    post = ThreadsPost(url=url, media=[ThreadsMedia("image", media_url)] if media_fails else [])
                elif platform == "Instagram":
                    post = InstagramPost(url=url, shortcode="Example001", media=[InstagramMedia("image", media_url)] if media_fails else [])
                else:
                    post = FacebookPostData(url=url, media=[FacebookMedia("image", media_url)] if media_fails else [])
                fetch_patch = patch.object(module, "_fetch_og_data", new=AsyncMock(return_value=post)) if platform == "Facebook" else \
                    patch.object(module.asyncio, "to_thread", new=AsyncMock(return_value=post))
                with self.subTest(platform=platform, media_fails=media_fails), fetch_patch, \
                        patch.object(module, "_download_bytes", new=AsyncMock(return_value=None)), \
                        patch.object(module, "cleanup_source_message", new_callable=AsyncMock), \
                        patch("discord_bot.features.social_preview.sender.send_preview_as_author", new=AsyncMock(return_value=types.SimpleNamespace(id=100))) as send, \
                        patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify:
                    self.assertTrue(await getattr(module, f"handle_{key}_in_message")(source(url)))
                    notify.assert_awaited_once()
                    self.assertIn(f"||{platform}預覽失敗 爛meta||", str(send.await_args.kwargs["view"].to_components()))

    async def test_success_has_no_failure_notification(self):
        for platform, key, module, model, url in CASES:
            with self.subTest(platform=platform), patch.object(
                module, f"build_{key}_preview", new=AsyncMock(return_value=successful_preview(model, platform, url))
            ), patch.object(module, "send_preview_group", new_callable=AsyncMock), \
                    patch.object(module, "cleanup_source_message", new_callable=AsyncMock), \
                    patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify:
                self.assertTrue(await getattr(module, f"handle_{key}_in_message")(source(url)))
                notify.assert_not_awaited()

    async def test_unknown_reason_and_exception_details_are_not_forwarded(self):
        for platform, _, _, _, url in CASES:
            with self.subTest(platform=platform), patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify:
                await failures.notify_preview_failure(
                    source(url), platform=platform, url=url + "?access_token=private-token&v=123#private-fragment",
                    reason="sessionid=private-cookie", exception=RuntimeError("private-response-body"),
                    trace_id="trace\nprivate-header",
                )
                notify.assert_awaited_once()
                self.assertIn("unknown_error", notify.await_args.kwargs["error"])
                self.assertNotIn("private-", str(notify.await_args))
                self.assertEqual(notify.await_args.kwargs["record"]["question"], url + "?v=123")

    async def test_all_platforms_forward_to_configured_guild_without_mentions(self):
        for platform, _, _, _, url in CASES:
            with self.subTest(platform=platform), patch.dict(os.environ, {
                "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "456", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "99",
            }):
                channel = types.SimpleNamespace(send=AsyncMock())
                guild = types.SimpleNamespace(get_channel=Mock(return_value=channel))
                bot = types.SimpleNamespace(get_guild=Mock(return_value=guild))
                await failures.notify_preview_failure(source(url), platform=platform, url=url,
                                                      reason="send_failed", bot_client=bot)
                bot.get_guild.assert_called_once_with(99)
                guild.get_channel.assert_called_once_with(456)
                channel.send.assert_awaited_once()
                self.assertIn(f"{platform} 預覽", channel.send.await_args.args[0])
                self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_excess_tracking_parameters_do_not_prevent_failure_notification(self):
        url = "https://www.facebook.com/demo/posts/123"
        with patch.object(failures, "forward_notify_to_channel", new_callable=AsyncMock) as notify:
            await failures.notify_preview_failure(source(url), platform="Facebook", reason="login_required",
                                                  url=url + "?" + "&".join(f"tracking{i}=value" for i in range(101)))
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.kwargs["record"]["question"], url)


if __name__ == "__main__":
    unittest.main()
