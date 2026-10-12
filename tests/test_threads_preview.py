import unittest
import sys
import types
import importlib.util
from unittest.mock import AsyncMock, patch
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
    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_cookie_failures_keep_v2_spoiler_commentary_and_original_link(self, fetch, download, send, cleanup):
        for reason, expected in (("cookie_login_required", "重新登入或驗證"),
                                 ("restricted_content", "觀看權限"),
                                 ("cookie_restricted_content", "仍收到限制觀看頁"),
                                 ("cookie_rate_limited", "暫時限制請求"),
                                 ("cookie_config_invalid", "設定格式不正確"),
                                 ("cookie_fetch_failed", "抓取失敗"),
                                 ("cookie_no_content", "仍無法取得")):
            with self.subTest(reason=reason):
                url = "https://www.threads.com/@demo/post/Cookie001"
                fetch.return_value = ThreadsPost(url=url, preview_error=reason)
                send.return_value = types.SimpleNamespace(id=123)
                message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                                channel=types.SimpleNamespace(name="general"), content=f"我的留言 ||{url}||")
                self.assertTrue(await handle_threads_in_message(message))
                view = send.await_args.kwargs["view"]
                self.assertIsInstance(view, discord.ui.LayoutView)
                text = "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
                self.assertIn(expected, text)
                self.assertIn("我的留言", text)
                self.assertTrue(next(item for item in view.children if isinstance(item, discord.ui.Container)).spoiler)
                self.assertEqual(next(item for item in view.walk_children() if getattr(item, "label", None) == "原連結").url, url)
                self.assertEqual(gallery_items(view), [])
                self.assertEqual(set(send.await_args.kwargs), {"view"})
        download.assert_not_awaited()

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
    async def test_video_post_uses_only_first_video_without_downloading_poster_or_video(self, fetch, download):
        first = "https://scontent.cdninstagram.com/first.mp4?oh=signed&oe=expiry"
        fetch.return_value = ThreadsPost(url="https://www.threads.com/@demo/post/Video001", media=[
            ThreadsMedia("image", "https://scontent.cdninstagram.com/poster.jpg"),
            ThreadsMedia("video", first),
            ThreadsMedia("video", "https://scontent.cdninstagram.com/second.mp4"),
        ])
        preview = await build_threads_preview(fetch.return_value.url)
        self.assertEqual(preview.inline_video_url, first)
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
        self.assertTrue(card.spoiler)
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in view.children))
        self.assertIn("劇透文字", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])

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
