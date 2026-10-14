import unittest
import sys
import types
import importlib.util
import os
from unittest.mock import AsyncMock, Mock, patch
from tests.support import isolated_discord_suite

from discord_bot.features.social_preview.threads_fetch import ThreadsMedia, ThreadsPost

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

if importlib.util.find_spec("aiohttp") is None or importlib.util.find_spec("discord") is None:
    raise unittest.SkipTest("threads_preview tests require aiohttp and discord.py")

from discord_bot.features.social_preview.threads_preview import (
    THREADS_INVALID_POST_MESSAGE,
    PreviewResult,
    build_threads_preview,
    extract_threads_urls,
    handle_threads_in_message,
)
import discord


load_tests = isolated_discord_suite(__name__)


def make_view(**kwargs):
    from discord_bot.features.social_preview.preview_view import build_social_preview_views
    views = build_social_preview_views(title="Threads", author="demo", text="", commentary="", spoiler=False, timeout=None, **kwargs)
    return views[0]


def gallery_items(view):
    return [media for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery) for media in item.items]


class ThreadsPreviewTests(unittest.IsolatedAsyncioTestCase):
    @patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_cookie_failures_keep_v2_spoiler_commentary_and_original_link(self, fetch, download, send, cleanup, notify):
        for reason, expected in (("cookie_login_required", "重新登入或驗證"),
                                 ("restricted_content", "觀看權限"),
                                 ("cookie_restricted_content", "仍收到限制觀看頁"),
                                 ("cookie_rate_limited", "暫時限制請求"),
                                 ("cookie_config_invalid", "設定格式不正確"),
                                 ("cookie_fetch_failed", "抓取失敗"),
                                 ("cookie_no_content", "仍無法取得")):
            with self.subTest(reason=reason):
                notify.reset_mock()
                cleanup.reset_mock()
                url = "https://www.threads.com/@demo/post/Cookie001"
                fetch.return_value = ThreadsPost(url=url, preview_error=reason, text="private-body-marker",
                                                debug={"cookie": "private-cookie-marker", "html": "private-html-marker"})
                send.return_value = types.SimpleNamespace(id=123)
                message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                                channel=types.SimpleNamespace(name="general"), content=f"我的留言 ||{url}||")
                self.assertTrue(await handle_threads_in_message(message))
                view = send.await_args.kwargs["view"]
                self.assertIsInstance(view, discord.ui.LayoutView)
                text = "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
                self.assertIn(THREADS_INVALID_POST_MESSAGE, text)
                self.assertNotIn(expected, text)
                self.assertNotIn(reason, text)
                self.assertIn("我的留言", text)
                card = next(item for item in view.children if isinstance(item, discord.ui.Container))
                self.assertNotIn("Cookie", card.children[1].content)
                self.assertFalse(card.spoiler)
                self.assertTrue(card.children[1].content.startswith("||"))
                self.assertTrue(card.children[1].content.endswith("||"))
                self.assertEqual(next(item for item in view.walk_children() if getattr(item, "label", None) == "原連結").url, url)
                self.assertEqual(gallery_items(view), [])
                self.assertEqual(set(send.await_args.kwargs), {"view"})
                notify.assert_awaited_once()
                self.assertEqual(notify.await_args.kwargs["notify_type"], "error")
                self.assertIn(reason, notify.await_args.kwargs["error"])
                self.assertIn(expected, notify.await_args.kwargs["error"])
                self.assertEqual(notify.await_args.kwargs["record"]["question"], url)
                self.assertNotIn("private-", text + str(notify.await_args))
                cleanup.assert_awaited_once()
        download.assert_not_awaited()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_restriction_diagnostic_reaches_configured_botlog_in_another_guild(self, fetch, send, cleanup):
        url = "https://www.threads.com/@demo/post/Restricted001"
        fetch.return_value = ThreadsPost(url=url, preview_error="cookie_restricted_content")
        send.return_value = types.SimpleNamespace(id=123)
        botlog = types.SimpleNamespace(send=AsyncMock())
        target_guild = types.SimpleNamespace(id=99, get_channel=Mock(return_value=botlog))
        bot = types.SimpleNamespace(get_guild=Mock(return_value=target_guild))
        source_guild = types.SimpleNamespace(id=1, get_channel=Mock())
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, id=10, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(id=20, name="general"), guild=source_guild,
            content=f"{url}?xmt=tracking-marker", reply=AsyncMock(),
        )
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "456", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "99"}):
            self.assertTrue(await handle_threads_in_message(message, bot_client=bot))

        bot.get_guild.assert_called_once_with(99)
        target_guild.get_channel.assert_called_once_with(456)
        source_guild.get_channel.assert_not_called()
        botlog.send.assert_awaited_once()
        diagnostic = botlog.send.await_args.args[0]
        self.assertIn("cookie_restricted_content", diagnostic)
        self.assertIn("登入備援仍收到限制觀看頁", diagnostic)
        self.assertIn(url, diagnostic)
        self.assertIn("使用者: `demo（10）`", diagnostic)
        self.assertIn("來源頻道: `general（20）`", diagnostic)
        self.assertNotIn("tracking-marker", diagnostic)
        self.assertNotIn("Email:", diagnostic)
        self.assertEqual(botlog.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        view = send.await_args.kwargs["view"]
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertEqual(card.children[1].content, THREADS_INVALID_POST_MESSAGE)
        self.assertEqual(next(item for item in view.walk_children() if getattr(item, "label", None) == "原連結").url, url)
        cleanup.assert_awaited_once()
        message.reply.assert_not_awaited()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_unconfigured_or_failed_botlog_does_not_break_public_preview(self, fetch, send, cleanup):
        url = "https://www.threads.com/@demo/post/Restricted001"
        fetch.return_value = ThreadsPost(url=url, preview_error="cookie_restricted_content")
        send.return_value = types.SimpleNamespace(id=123)
        botlog = types.SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("notify-send-failed")))
        guild = types.SimpleNamespace(id=1, get_channel=Mock(return_value=botlog))
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, id=10, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(id=20, name="general"), guild=guild, content=url, reply=AsyncMock(),
        )
        for channel_id in ("", "456"):
            with self.subTest(channel_id=channel_id), patch.dict(
                os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": channel_id, "DISCORD_NOTIFY_FORWARD_GUILD_ID": ""}
            ), self.assertLogs("discord_digest_bot", level="WARNING"):
                self.assertTrue(await handle_threads_in_message(message))
                card = next(item for item in send.await_args.kwargs["view"].children if isinstance(item, discord.ui.Container))
                self.assertEqual(card.children[1].content, THREADS_INVALID_POST_MESSAGE)
        botlog.send.assert_awaited_once()
        self.assertEqual(cleanup.await_count, 2)
        message.reply.assert_not_awaited()

        with patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel",
                   new=AsyncMock(side_effect=RuntimeError("unexpected-notify-error"))), \
                self.assertLogs("discord_digest_bot", level="WARNING"):
            self.assertTrue(await handle_threads_in_message(message))
        self.assertEqual(cleanup.await_count, 3)
        message.reply.assert_not_awaited()

    @patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_success_is_quiet_and_invalid_post_reports_failure(self, fetch, send, cleanup, notify):
        url = "https://www.threads.com/@demo/post/Example001"
        send.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"), content=url,
        )
        for reason in (None, "invalid_post"):
            with self.subTest(reason=reason):
                fetch.return_value = ThreadsPost(url=url, preview_error=reason, text="正常正文" if reason is None else "")
                self.assertTrue(await handle_threads_in_message(message))
                if reason is None:
                    notify.assert_not_awaited()
        notify.assert_awaited_once()
        self.assertIn("invalid_post", notify.await_args.kwargs["error"])

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_linked_ig_video_reaches_v2_gallery_without_upload(self, fetch, download, send, cleanup):
        from pathlib import Path
        from discord_bot.features.social_preview.threads_fetch import _parse_html
        url = "https://www.threads.com/@demo/post/Video001"
        fixture = (Path(__file__).parent / "fixtures" / "threads_linked_instagram_video.html").read_text(encoding="utf-8")
        fetch.return_value = _parse_html(url, fixture)
        send.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                        channel=types.SimpleNamespace(name="general"), content=url)
        self.assertTrue(await handle_threads_in_message(message))
        gallery = gallery_items(send.await_args.kwargs["view"])
        self.assertEqual(len(gallery), 1)
        self.assertIn("linked-reel.mp4", gallery[0].media.url)
        download.assert_not_awaited()

    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_video_post_keeps_multiple_videos_without_downloading_them(self, fetch, download):
        first = "https://scontent.cdninstagram.com/first.mp4?oh=signed&oe=expiry"
        fetch.return_value = ThreadsPost(url="https://www.threads.com/@demo/post/Video001", media=[
            ThreadsMedia("video", first),
            ThreadsMedia("video", "https://scontent.cdninstagram.com/second.mp4"),
        ])
        preview = await build_threads_preview(fetch.return_value.url)
        self.assertEqual(preview.inline_video_url, first)
        self.assertEqual(preview.media_urls, [first, "https://scontent.cdninstagram.com/second.mp4"])
        self.assertIsNone(preview.extra_text)
        self.assertEqual(preview.files, [])
        image = getattr(preview.embed, "image", None)
        self.assertFalse(image.get("url") if isinstance(image, dict) else getattr(image, "url", None))
        download.assert_not_awaited()

    def test_long_signed_video_url_is_not_put_in_a_512_character_button(self):
        video = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 900
        view = make_view(original_url="https://www.threads.com/@demo/post/Video001", video_url=video)
        self.assertFalse(any(getattr(item, "label", None) == "影片直連" for item in view.children))

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_spoiler_video_uses_native_mask_without_downloads_or_naked_links(self, fetch, download, send, cleanup):
        url = "https://www.threads.com/@demo/post/Video001"
        video = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 900
        fetch.return_value = ThreadsPost(url=url, text="劇透文字", media=[ThreadsMedia("video", video)])
        send.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                        channel=types.SimpleNamespace(name="general"), content=f"||{url}||")
        self.assertTrue(await handle_threads_in_message(message))
        download.assert_not_awaited()
        send.assert_awaited_once()
        self.assertEqual(set(send.await_args.kwargs), {"view"})
        view = send.await_args.kwargs["view"]
        self.assertIsInstance(view, discord.ui.LayoutView)
        media = gallery_items(view)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].media.url, video)
        self.assertTrue(media[0].spoiler)
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertFalse(card.spoiler)
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in card.children))
        self.assertIn("||劇透文字||", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_commentary_and_signed_video_fit_same_v2_message(self, fetch, send, cleanup):
        url = "https://www.threads.com/@demo/post/Video001"
        video = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 900
        fetch.return_value = ThreadsPost(url=url, text="貼文", media=[ThreadsMedia("video", video)])
        for spoiler in (False, True):
            with self.subTest(spoiler=spoiler):
                send.reset_mock()
                send.return_value = types.SimpleNamespace(id=123)
                original = f"||{url}||" if spoiler else url
                message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                                channel=types.SimpleNamespace(name="general"), content="評" * 1700 + " " + original)
                self.assertTrue(await handle_threads_in_message(message))
                self.assertEqual(send.await_count, 1)
                self.assertEqual(set(send.await_args.kwargs), {"view"})
                view = send.await_args.kwargs["view"]
                self.assertEqual(view.children[0].content, "評" * 1700)
                self.assertEqual(gallery_items(view)[0].media.url, video)

    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_oversized_signed_url_is_not_truncated_or_uploaded(self, fetch, download):
        video = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 2100
        fetch.return_value = ThreadsPost(url="https://www.threads.com/@demo/post/Video001", media=[ThreadsMedia("video", video)])
        preview = await build_threads_preview(fetch.return_value.url)
        self.assertEqual(preview.video_url, video)
        self.assertEqual(preview.inline_video_url, video)
        download.assert_not_awaited()

    def test_extract_threads_urls_accepts_share_link(self):
        url = "https://www.threads.com/share/BATCH_yt4N/"

        self.assertEqual(
            extract_threads_urls(f"看看這篇 {url}"),
            ["https://www.threads.com/share/BATCH_yt4N"],
        )

    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_build_threads_preview_replaces_invalid_post_meta_with_failure_message(self, mock_to_thread):
        url = "https://www.threads.com/@demo/post/abc"
        mock_to_thread.return_value = ThreadsPost(
            url="https://www.threads.com/?error=invalid_post",
            preview_error="invalid_post",
        )

        preview = await build_threads_preview(url)

        self.assertEqual(preview.embed.description, THREADS_INVALID_POST_MESSAGE)
        self.assertEqual(preview.embed.url, url)
        self.assertEqual(preview.files, [])
        self.assertIsNone(preview.inline_video_url)

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_handle_invalid_post_sends_fixed_failure_preview_with_original_link(
        self,
        mock_to_thread,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.threads.com/@demo/post/abc"
        mock_to_thread.return_value = ThreadsPost(
            url="https://www.threads.com/?error=invalid_post",
            preview_error="invalid_post",
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=url,
        )

        handled = await handle_threads_in_message(message)

        self.assertTrue(handled)
        sent_kwargs = mock_send_preview_as_author.await_args.kwargs
        view = sent_kwargs["view"]
        self.assertTrue(view.has_components_v2())
        self.assertFalse(set(sent_kwargs) & {"embed", "content"})
        self.assertIn(THREADS_INVALID_POST_MESSAGE, [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)])
        original_link = next(item for item in view.walk_children() if getattr(item, "label", None) == "原連結")
        self.assertEqual(original_link.url, url)
        mock_cleanup_source_message.assert_awaited_once()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.build_threads_preview", new_callable=AsyncMock)
    async def test_share_link_button_uses_clean_canonical_post_url(
        self,
        mock_build_threads_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        share_url = "https://www.threads.com/share/BATCH_yt4N"
        canonical_url = "https://www.threads.com/@waiteryoshie/post/DavTjGCEwon"
        mock_build_threads_preview.return_value = PreviewResult(
            embed=discord.Embed(title="Threads", url=canonical_url),
            files=[],
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=share_url,
        )

        handled = await handle_threads_in_message(message)

        self.assertTrue(handled)
        view = mock_send_preview_as_author.await_args.kwargs["view"]
        original_link = next(
            item for item in view.walk_children() if getattr(item, "label", None) == "原連結"
        )
        self.assertEqual(original_link.url, canonical_url)
        mock_cleanup_source_message.assert_awaited_once()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.build_threads_preview", new_callable=AsyncMock)
    async def test_spoiler_share_link_button_uses_clean_canonical_post_url(
        self,
        mock_build_threads_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        share_url = "https://www.threads.com/share/BATCH_yt4N"
        canonical_url = "https://www.threads.com/@waiteryoshie/post/DavTjGCEwon"
        mock_build_threads_preview.return_value = PreviewResult(
            embed=discord.Embed(title="Threads", description="spoiler", url=canonical_url),
            files=[],
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=f"||{share_url}||",
        )

        handled = await handle_threads_in_message(message)

        self.assertTrue(handled)
        view = mock_send_preview_as_author.await_args.kwargs["view"]
        original_link = next(
            item for item in view.walk_children() if getattr(item, "label", None) == "原連結"
        )
        self.assertEqual(original_link.url, canonical_url)
        mock_cleanup_source_message.assert_awaited_once()

    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_build_threads_preview_uses_clean_video_button_instead_of_raw_url(self, mock_to_thread):
        video_url = "https://video.xx.fbcdn.net/media/video.mp4?token=abc"
        mock_to_thread.return_value = ThreadsPost(
            url="https://www.threads.com/@demo/post/abc",
            author_username="demo",
            text="影片貼文",
            media=[ThreadsMedia("video", video_url)],
        )

        preview = await build_threads_preview("https://www.threads.com/@demo/post/abc")

        self.assertEqual(preview.video_url, video_url)
        self.assertEqual(preview.inline_video_url, video_url)
        self.assertIsNone(preview.extra_text)
        self.assertEqual(preview.files, [])

    def test_delete_preview_view_adds_video_link_button(self):
        view = make_view(
            original_url="https://www.threads.com/@demo/post/abc",
            video_url="https://video.xx.fbcdn.net/media/video.mp4?token=abc",
        )

        labels = [getattr(item, "label", None) for item in view.walk_children()]
        self.assertIn("原連結", labels)
        self.assertIn("影片直連", labels)
        original_link = next(item for item in view.walk_children() if getattr(item, "label", None) == "原連結")
        self.assertEqual(original_link.url, "https://www.threads.com/@demo/post/abc")

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.build_threads_preview", new_callable=AsyncMock)
    async def test_handle_threads_in_message_sends_video_in_v2_only(
        self,
        mock_build_threads_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.threads.com/@demo/post/abc"
        video_url = "https://video.xx.fbcdn.net/media/video.mp4?token=abc"
        mock_build_threads_preview.return_value = PreviewResult(
            embed=discord.Embed(title="Threads @demo"),
            files=[],
            video_url=video_url,
            inline_video_url=video_url,
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=url,
        )

        handled = await handle_threads_in_message(message)

        self.assertTrue(handled)
        mock_send_preview_as_author.assert_awaited_once()
        self.assertEqual(set(mock_send_preview_as_author.await_args.kwargs), {"view"})
        view = mock_send_preview_as_author.await_args.kwargs["view"]
        self.assertEqual(gallery_items(view)[0].media.url, video_url)
        mock_cleanup_source_message.assert_awaited_once()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.build_threads_preview", new_callable=AsyncMock)
    async def test_handle_threads_in_message_keeps_user_commentary_around_url(
        self,
        mock_build_threads_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.threads.com/@demo/post/abc"
        mock_build_threads_preview.return_value = PreviewResult(
            embed=discord.Embed(title="Threads @demo"),
            files=[],
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)

        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=f"一起來看 {url} 超好笑",
        )

        handled = await handle_threads_in_message(message)

        self.assertTrue(handled)
        self.assertEqual(
            mock_send_preview_as_author.await_args.kwargs["view"].children[0].content,
            "一起來看 超好笑",
        )
        mock_cleanup_source_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
