import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import isolated_discord_suite

load_tests = isolated_discord_suite(__name__)

import discord
from discord_bot.features.social_preview.instagram_fetch import InstagramMedia, InstagramPost
from discord_bot.features.social_preview import instagram_preview as preview

URL = "https://www.instagram.com/p/ABC123"
IMAGES = [f"https://scontent.cdninstagram.com/photo-{n}.jpg" for n in range(7)]


def source(content):
    return types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                 channel=types.SimpleNamespace(name="general"), content=content, reply=AsyncMock())


def gallery(view):
    return [media for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery) for media in item.items]


class InstagramPreviewTests(unittest.IsolatedAsyncioTestCase):
    def test_url_extraction_sanitizes_and_deduplicates(self):
        self.assertEqual(preview.extract_instagram_urls(URL + "/?mdxt=tracking#fragment " + URL), [URL])
        self.assertEqual(preview.extract_instagram_urls("https://instagr.am/reels/ABC123/"), ["https://www.instagram.com/reels/ABC123"])
        self.assertEqual(preview.extract_instagram_urls("https://www.instagram.com/stories/demo/123 https://www.instagram.com/demo"), [])

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_images_use_v2_first_four_order_commentary_and_native_spoiler(self, fetch, download, send, cleanup):
        for count in (1, 2, 4, 7):
            for spoiler in (False, True):
                with self.subTest(count=count, spoiler=spoiler):
                    download.reset_mock()
                    send.reset_mock()
                    cleanup.reset_mock()
                    fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", author_username="demo_user", text="原文",
                                                       media=[InstagramMedia("image", u) for u in IMAGES[:count]])
                    download.return_value = b"synthetic image"
                    send.return_value = types.SimpleNamespace(id=123)
                    message = source("我的留言 " + (f"||{URL}/?mdxt=track||" if spoiler else URL))
                    self.assertTrue(await preview.handle_instagram_in_message(message))
                    kwargs = send.await_args.kwargs
                    self.assertEqual(set(kwargs), {"view", "files"})
                    view = kwargs["view"]
                    self.assertIsInstance(view, discord.ui.LayoutView)
                    self.assertEqual([call.args[1] for call in download.await_args_list], IMAGES[:min(count, 4)])
                    self.assertEqual([m.media.url for m in gallery(view)], [f"attachment://{f.filename}" for f in kwargs["files"]])
                    self.assertEqual(len(kwargs["files"]), min(count, 4))
                    self.assertTrue(all(f.spoiler == spoiler for f in kwargs["files"]))
                    self.assertTrue(all(m.spoiler == spoiler for m in gallery(view)))
                    card = next(c for c in view.children if isinstance(c, discord.ui.Container))
                    self.assertFalse(card.spoiler)
                    self.assertIn("||原文||" if spoiler else "原文", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
                    self.assertIn("Instagram", card.children[0].content)
                    self.assertFalse(card.children[0].content.startswith("||"))
                    self.assertEqual(view.children[0].content, "我的留言")
                    buttons = [x for x in view.walk_children() if isinstance(x, discord.ui.Button)]
                    self.assertEqual([x.url for x in buttons], [None, URL])
                    self.assertEqual(str(buttons[0].emoji), "🗑️")
                    cleanup.assert_awaited_once_with(message, platform="Instagram", url=URL)

    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_failed_images_are_skipped_without_fetching_a_fifth(self, fetch, download):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", media=[InstagramMedia("image", u) for u in IMAGES])
        download.side_effect = [None, b"second", None, b"fourth"]
        result = await preview.build_instagram_preview(URL)
        self.assertEqual([c.args[1] for c in download.await_args_list], IMAGES[:4])
        self.assertEqual([f.filename for f in result.files], ["instagram_image_2.jpg", "instagram_image_4.jpg"])
        for file in result.files:
            file.close()

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_videos_use_ordered_mp4s_without_reupload(self, fetch, download, send, cleanup):
        video = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 900
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", text="reel", media=[
            InstagramMedia("video", video),
            InstagramMedia("video", "https://scontent.cdninstagram.com/second.mp4")])
        send.return_value = types.SimpleNamespace(id=123)
        self.assertTrue(await preview.handle_instagram_in_message(source(f"||{URL}||")))
        self.assertEqual(set(send.await_args.kwargs), {"view"})
        self.assertEqual([m.media.url for m in gallery(send.await_args.kwargs["view"])],
                         [video, "https://scontent.cdninstagram.com/second.mp4"])
        download.assert_not_awaited()

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_fallback_video_retains_spoiler_and_original_instagram_link(self, fetch, download, send, cleanup):
        url = "https://www.instagram.com/reel/ABC123"
        video = "https://scontent.cdninstagram.com/fallback.mp4?sig=synthetic"
        fetch.return_value = InstagramPost(url=url, shortcode="ABC123", kind="reel", text="原文",
                                           preview_level="video_fallback", media=[InstagramMedia("video", video)],
                                           trace_id="synthetictrace", fetch_source="kkinstagram")
        send.return_value = types.SimpleNamespace(id=123)
        with self.assertLogs(preview.logger, level="DEBUG") as logs:
            self.assertTrue(await preview.handle_instagram_in_message(source(f"留言 ||{url}||")))
        events = [json.loads(r.getMessage().split(" ", 1)[1]) for r in logs.records if r.getMessage().startswith("instagram_diagnostic ")]
        self.assertEqual([e["event"] for e in events], ["preview_prepared", "preview_delivery"])
        self.assertEqual({e["trace_id"] for e in events}, {"synthetictrace"})
        self.assertEqual({e["source"] for e in events}, {"kkinstagram"})
        self.assertEqual(events[0]["video_count"], 1)
        self.assertTrue(events[0]["spoiler"])
        self.assertEqual(events[1]["result"], "sent")
        self.assertEqual(events[1]["outcome"], "content")
        self.assertNotIn(video, " ".join(logs.output))
        self.assertNotIn("原文", " ".join(logs.output))
        view = send.await_args.kwargs["view"]
        self.assertEqual([m.media.url for m in gallery(view)], [video])
        self.assertTrue(all(m.spoiler for m in gallery(view)))
        self.assertFalse(next(x for x in view.children if isinstance(x, discord.ui.Container)).spoiler)
        buttons = [x.url for x in view.walk_children() if isinstance(x, discord.ui.Button)]
        self.assertIn(url, buttons)
        self.assertNotIn("kkinstagram", str(view.to_components()))
        download.assert_not_awaited()

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    @patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel", new_callable=AsyncMock)
    async def test_errors_are_v2_with_original_link_and_no_third_party(self, notify, fetch, send, cleanup):
        for error in preview.ERROR_MESSAGES:
            with self.subTest(error=error):
                notify.reset_mock()
                fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", preview_error=error)
                send.return_value = types.SimpleNamespace(id=123)
                self.assertTrue(await preview.handle_instagram_in_message(source(f"說明 ||{URL}||")))
                view = send.await_args.kwargs["view"]
                texts = " ".join(x.content for x in view.walk_children() if isinstance(x, discord.ui.TextDisplay))
                self.assertIn("Instagram預覽失敗 爛meta", texts)
                self.assertNotIn(preview.ERROR_MESSAGES[error], texts)
                self.assertIn("說明", texts)
                self.assertNotIn("kkinstagram", str(view.to_components()))
                self.assertEqual(gallery(view), [])
                notify.assert_awaited_once()
                self.assertIn(error, notify.await_args.kwargs["error"])
                self.assertIn(preview.ERROR_MESSAGES[error], notify.await_args.kwargs["error"])
                self.assertEqual(next(x.url for x in view.walk_children() if getattr(x, "label", None) == "原連結"), URL)

    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_filtered_empty_media_has_explicit_failure(self, fetch, download):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", author_username="demo",
                                           media=[InstagramMedia("image", "https://evil.example/image.jpg")])
        result = await preview.build_instagram_preview(URL)
        self.assertEqual(result.embed.description, preview.INSTAGRAM_INVALID_POST_MESSAGE)
        self.assertEqual(result.preview_error, "no_content")
        download.assert_not_awaited()

    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_failed_image_downloads_log_error_card_and_dropped_count(self, fetch, download):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", trace_id="synthetictrace", fetch_source="cookie",
                                           media=[InstagramMedia("image", u) for u in IMAGES[:2]])
        download.return_value = None
        with self.assertLogs(preview.logger, level="INFO") as logs:
            result = await preview.build_instagram_preview(URL)
        event = json.loads(logs.records[-1].getMessage().split(" ", 1)[1])
        self.assertEqual((event["selected_count"], event["rendered_count"], event["dropped_count"]), (2, 0, 2))
        self.assertEqual(event["outcome"], "error_card")
        self.assertEqual(event["result"], "fetch_failed")
        self.assertEqual(result.outcome, "error_card")

    @patch.object(preview, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_no_download_mode_preserves_four_signed_urls(self, fetch, download):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", media=[InstagramMedia("image", u) for u in IMAGES])
        result = await preview.build_instagram_preview(URL, reupload_image=False, spoiler=True)
        self.assertEqual(result.image_urls, IMAGES[:4])
        self.assertEqual(result.files, [])
        download.assert_not_awaited()

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_long_caption_pages_share_delete_group(self, fetch, send, cleanup):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", text="長文" * 5000)
        send.side_effect = [types.SimpleNamespace(id=n) for n in range(1, 11)]
        self.assertTrue(await preview.handle_instagram_in_message(source(URL)))
        views = [c.kwargs["view"] for c in send.await_args_list]
        self.assertGreater(len(views), 1)
        all_ids = set(range(1, len(views) + 1))
        self.assertTrue(all(set(v.related_message_ids) == all_ids - {v.message.id} for v in views))

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_delivery_failure_keeps_source_and_rolls_back_partial_group(self, fetch, send, cleanup):
        fetch.return_value = InstagramPost(url=URL, shortcode="ABC123", text="長文" * 5000)
        first = types.SimpleNamespace(id=1, delete=AsyncMock())
        failure = discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"),
                                        {"code": 50035, "message": "https://scontent.cdninstagram.com/private.mp4?sig=synthetic-secret"})
        send.side_effect = [first, failure]
        message = source(URL)
        with self.assertLogs(preview.logger, level="INFO") as logs:
            self.assertFalse(await preview.handle_instagram_in_message(message))
        event = next(json.loads(r.getMessage().split(" ", 1)[1]) for r in logs.records
                     if r.getMessage().startswith("instagram_diagnostic ") and '"phase":"send"' in r.getMessage())
        self.assertEqual((event["event"], event["phase"], event["result"], event["error_type"]),
                         ("preview_delivery", "send", "failed", "HTTPException"))
        self.assertEqual((event["http_status"], event["error_code"]), (400, 50035))
        self.assertNotIn("synthetic-secret", " ".join(logs.output))
        cleanup.assert_not_awaited()
        first.delete.assert_awaited_once()
        message.reply.assert_awaited_once()

    @patch.object(preview, "cleanup_source_message", new_callable=AsyncMock)
    @patch.object(preview.asyncio, "to_thread")
    async def test_prepare_exception_keeps_trace_id_without_logging_response_details(self, fetch, cleanup):
        fetch.side_effect = RuntimeError("sessionid=synthetic-secret")
        message = source(URL)
        with self.assertLogs(preview.logger, level="INFO") as logs:
            self.assertFalse(await preview.handle_instagram_in_message(message))
        event = next(json.loads(r.getMessage().split(" ", 1)[1]) for r in logs.records
                     if r.getMessage().startswith("instagram_diagnostic "))
        self.assertEqual(event["phase"], "prepare")
        self.assertEqual(event["trace_id"], fetch.call_args.kwargs["trace_id"])
        self.assertTrue(event["trace_id"])
        self.assertNotIn("synthetic-secret", " ".join(logs.output))
        cleanup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
