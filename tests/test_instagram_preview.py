import importlib.util
import io
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import install_discord_stub

if importlib.util.find_spec("aiohttp") is None:
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    aiohttp_stub.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp_stub

install_discord_stub()

import discord

from discord_bot.features.social_preview.instagram_fetch import InstagramMedia, InstagramPost
from discord_bot.features.social_preview.instagram_preview import (
    DeleteInstagramPreviewView,
    InstagramPreview,
    build_instagram_preview,
    extract_instagram_urls,
    handle_instagram_in_message,
)


class InstagramPreviewTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_instagram_urls_sanitizes_tracking_and_dedupes(self):
        content = (
            "look https://instagram.com/p/ABC123/?igsh=demo#frag "
            "again https://www.instagram.com/p/ABC123/"
        )

        self.assertEqual(extract_instagram_urls(content), ["https://www.instagram.com/p/ABC123"])

    def test_extract_instagram_urls_accepts_reels_path(self):
        self.assertEqual(
            extract_instagram_urls("https://www.instagram.com/reels/ABC123/"),
            ["https://www.instagram.com/reels/ABC123"],
        )

    def test_extract_instagram_urls_ignores_stories_and_profiles(self):
        content = (
            "https://www.instagram.com/stories/demo/123 "
            "https://www.instagram.com/demo/"
        )

        self.assertEqual(extract_instagram_urls(content), [])

    def test_delete_view_uses_threads_style_labels(self):
        view = DeleteInstagramPreviewView(
            original_url="https://www.instagram.com/reel/ABC123",
            video_url="https://cdn.example.com/video.mp4",
        )

        labels = [getattr(item, "label", None) for item in view.children]
        self.assertIn("原連結", labels)
        self.assertIn("影片直連", labels)

    @patch("discord_bot.features.social_preview.instagram_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.asyncio.to_thread")
    async def test_build_instagram_preview_reuploads_first_image(self, mock_to_thread, mock_download_bytes):
        mock_to_thread.return_value = InstagramPost(
            url="https://www.instagram.com/p/ABC123",
            shortcode="ABC123",
            author_username="demo",
            text="caption",
            media=[InstagramMedia("image", "https://cdn.example.com/post.jpg")],
        )
        mock_download_bytes.return_value = b"image-bytes"

        preview = await build_instagram_preview("https://www.instagram.com/p/ABC123/")

        self.assertEqual(preview.embed.title, "Instagram @demo")
        self.assertEqual(preview.embed.description, "caption")
        self.assertEqual(preview.files[0].filename, "instagram_image.jpg")
        self.assertEqual(preview.embed.image["url"], "attachment://instagram_image.jpg")

    @patch("discord_bot.features.social_preview.instagram_preview.asyncio.to_thread")
    async def test_build_instagram_preview_rejects_generic_empty_metadata(self, mock_to_thread):
        mock_to_thread.return_value = InstagramPost(
            url="https://www.instagram.com/p/ABC123",
            shortcode="ABC123",
        )

        with self.assertRaises(RuntimeError):
            await build_instagram_preview("https://www.instagram.com/p/ABC123/")

    @patch("discord_bot.features.social_preview.instagram_preview.asyncio.to_thread")
    async def test_build_instagram_preview_uses_native_embed_without_media(self, mock_to_thread):
        mock_to_thread.return_value = InstagramPost(
            url="https://www.instagram.com/reel/ABC123",
            shortcode="ABC123",
            preview_level="native_embed",
            native_embed_url="https://www.kkinstagram.com/reels/ABC123/",
        )

        preview = await build_instagram_preview("https://www.instagram.com/reel/ABC123/")

        self.assertIsNone(preview.embed)
        self.assertEqual(preview.native_embed_url, "https://www.kkinstagram.com/reels/ABC123/")

    @patch("discord_bot.features.social_preview.instagram_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.asyncio.to_thread")
    async def test_build_instagram_preview_labels_media_fallback(self, mock_to_thread, mock_download_bytes):
        mock_to_thread.return_value = InstagramPost(
            url="https://www.instagram.com/reel/ABC123",
            shortcode="ABC123",
            kind="reel",
            preview_level="media_fallback",
            native_embed_url="https://www.kkinstagram.com/reels/ABC123/",
            media=[InstagramMedia("image", "https://www.instagram.com/reel/ABC123/media/?size=l")],
        )
        mock_download_bytes.return_value = b"image-bytes"

        preview = await build_instagram_preview("https://www.instagram.com/reel/ABC123/")

        self.assertIsNone(preview.embed)
        self.assertEqual(preview.native_embed_url, "https://www.kkinstagram.com/reels/ABC123/")
        self.assertEqual(preview.files, [])

    @patch("discord_bot.features.social_preview.instagram_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.asyncio.to_thread")
    async def test_build_instagram_preview_tries_next_image_candidate(self, mock_to_thread, mock_download_bytes):
        mock_to_thread.return_value = InstagramPost(
            url="https://www.instagram.com/reel/ABC123",
            shortcode="ABC123",
            media=[
                InstagramMedia("image", "https://www.instagram.com/reel/ABC123/media/?size=l"),
                InstagramMedia("image", "https://www.instagram.com/p/ABC123/media/?size=l"),
            ],
        )
        mock_download_bytes.side_effect = [None, b"image-bytes"]

        preview = await build_instagram_preview("https://www.instagram.com/reel/ABC123/")

        self.assertEqual(mock_download_bytes.await_count, 2)
        self.assertEqual(preview.files[0].filename, "instagram_image.jpg")

    @patch("discord_bot.features.social_preview.instagram_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.build_instagram_preview", new_callable=AsyncMock)
    async def test_handle_instagram_in_message_keeps_user_commentary(
        self,
        mock_build_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.instagram.com/p/ABC123"
        mock_build_preview.return_value = InstagramPreview(
            embed=discord.Embed(title="Instagram @demo"),
            files=[],
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=f"nice post {url} thanks",
        )

        handled = await handle_instagram_in_message(message)

        self.assertTrue(handled)
        self.assertEqual(mock_send_preview_as_author.await_args.kwargs["content"], "nice post thanks")
        mock_cleanup_source_message.assert_awaited_once_with(message, platform="Instagram", url=url)

    @patch("discord_bot.features.social_preview.instagram_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.build_instagram_preview", new_callable=AsyncMock)
    async def test_handle_instagram_in_message_sends_native_proxy_embed_url(
        self,
        mock_build_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.instagram.com/reel/ABC123"
        proxy_url = "https://www.kkinstagram.com/reels/ABC123/"
        mock_build_preview.return_value = InstagramPreview(
            embed=None,
            files=[],
            native_embed_url=proxy_url,
        )
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=url,
        )

        handled = await handle_instagram_in_message(message)

        self.assertTrue(handled)
        self.assertEqual(mock_send_preview_as_author.await_args.kwargs["content"], f"[.]({proxy_url})")
        self.assertIsNone(mock_send_preview_as_author.await_args.kwargs["embed"])
        mock_cleanup_source_message.assert_awaited_once_with(message, platform="Instagram", url=url)

    @patch("discord_bot.features.social_preview.instagram_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.instagram_preview.build_instagram_preview", new_callable=AsyncMock)
    async def test_spoilered_instagram_url_disables_native_embed(
        self,
        mock_build_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        raw_url = "https://www.instagram.com/reel/ABC123/?igsh=tracking"
        clean_url = "https://www.instagram.com/reel/ABC123"
        mock_send_preview_as_author.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"),
            content=f"spoiler ||{raw_url}||",
        )

        handled = await handle_instagram_in_message(message)

        self.assertTrue(handled)
        mock_build_preview.assert_not_awaited()
        content = mock_send_preview_as_author.await_args.kwargs["content"]
        self.assertIn(f"||{clean_url}||", content)
        self.assertNotIn("igsh", content)
        self.assertNotIn("kkinstagram", content)
        mock_cleanup_source_message.assert_awaited_once_with(message, platform="Instagram", url=clean_url)


if __name__ == "__main__":
    unittest.main()
