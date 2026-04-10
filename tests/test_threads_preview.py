import unittest
import sys
import types
import importlib.util
from unittest.mock import AsyncMock, patch

from discord_bot.features.social_preview.threads_fetch import ThreadsMedia, ThreadsPost

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

if importlib.util.find_spec("aiohttp") is None or importlib.util.find_spec("discord") is None:
    raise unittest.SkipTest("threads_preview tests require aiohttp and discord.py")

from discord_bot.features.social_preview.threads_preview import DeletePreviewView, PreviewResult, build_threads_preview, handle_threads_in_message
import discord


class ThreadsPreviewTests(unittest.IsolatedAsyncioTestCase):
    @patch("discord_bot.features.social_preview.threads_preview.asyncio.to_thread")
    async def test_build_threads_preview_uses_clean_video_button_instead_of_raw_url(self, mock_to_thread):
        video_url = "https://cdn.example.com/media/video.mp4?token=abc"
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
        view = DeletePreviewView(
            original_url="https://www.threads.com/@demo/post/abc",
            video_url="https://cdn.example.com/media/video.mp4?token=abc",
        )

        labels = [getattr(item, "label", None) for item in view.children]
        self.assertIn("原連結", labels)
        self.assertIn("影片直連", labels)

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.build_threads_preview", new_callable=AsyncMock)
    async def test_handle_threads_in_message_sends_inline_video_url_in_content(
        self,
        mock_build_threads_preview,
        mock_send_preview_as_author,
        mock_cleanup_source_message,
    ):
        url = "https://www.threads.com/@demo/post/abc"
        video_url = "https://cdn.example.com/media/video.mp4?token=abc"
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
        self.assertEqual(mock_send_preview_as_author.await_args.kwargs["content"], video_url)
        mock_cleanup_source_message.assert_awaited_once()

    @patch("discord_bot.features.social_preview.threads_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.threads_preview.send_preview_as_author", new_callable=AsyncMock)
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
            mock_send_preview_as_author.await_args.kwargs["content"],
            "一起來看 超好笑",
        )
        mock_cleanup_source_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
