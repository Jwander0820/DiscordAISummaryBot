import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import isolated_discord_suite
load_tests = isolated_discord_suite(__name__)

import discord
from discord_bot.features.social_preview import media_gallery as gallery
from discord_bot.features.social_preview.threads_fetch import ThreadsMedia, ThreadsPost
from discord_bot.features.social_preview.instagram_fetch import InstagramMedia, InstagramPost
from discord_bot.features.social_preview.facebook_fetch import FacebookMedia, FacebookPostData
from discord_bot.features.social_preview import threads_preview, instagram_preview, facebook_preview

IMAGES = [f"https://scontent.cdninstagram.com/{n}.jpg" for n in range(7)]
VIDEOS = [f"https://video.xx.fbcdn.net/{n}.mp4?signature=synthetic" for n in range(5)]


def media_items(view):
    return [media for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery) for media in item.items]


class MediaGalleryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_spoiler_flags_stay_aligned_when_image_download_fails(self):
        items = [ThreadsMedia("image", IMAGES[0], spoiler=True), ThreadsMedia("image", IMAGES[1]),
                 ThreadsMedia("video", VIDEOS[0], spoiler=True), ThreadsMedia("image", IMAGES[2], spoiler=True)]
        download = AsyncMock(side_effect=[None, b"public", b"protected"])
        result = await gallery.prepare_gallery(None, items, downloader=download, prefix="native")
        self.assertEqual(result.media_spoilers, [False, True, True])
        self.assertEqual(result.media_urls, ["attachment://native_2.jpg", VIDEOS[0], "attachment://SPOILER_native_4.jpg"])
        self.assertEqual([f.spoiler for f in result.files], [False, True])
        self.assertEqual([c.args[1] for c in download.await_args_list], [IMAGES[0], IMAGES[1], IMAGES[2]])
        for file in result.files:
            file.close()
        result = await gallery.prepare_gallery(None, items, downloader=AsyncMock(), prefix="native", reupload_images=False)
        self.assertEqual(result.media_spoilers, [True, False, True, True])
        result = await gallery.prepare_gallery(None, items, downloader=AsyncMock(), prefix="native", reupload_images=False, spoiler=True)
        self.assertEqual(result.media_spoilers, [True] * 4)

    async def test_media_only_cards_skip_body_and_all_platforms_share_one_activity_log(self):
        for module, post_class, media_class, platform, url in [
            (threads_preview, ThreadsPost, ThreadsMedia, "threads", "https://www.threads.com/@asca_jp/post/DeZM55BjZt_"),
            (instagram_preview, InstagramPost, InstagramMedia, "instagram", "https://www.instagram.com/p/ABC123"),
            (facebook_preview, FacebookPostData, FacebookMedia, "facebook", "https://www.facebook.com/demo/posts/pfbidTEST"),
        ]:
            for kind, media_url in (("image", IMAGES[0]), ("video", VIDEOS[0])):
                for spoiler in (False, True):
                    with self.subTest(platform=platform, kind=kind, spoiler=spoiler):
                        extra = {"shortcode": "ABC123"} if platform == "instagram" else {}
                        post = post_class(url=url, media=[media_class(kind, media_url)], **extra)
                        fetch_patch = patch.object(module, "_fetch_og_data", new=AsyncMock(return_value=post)) if platform == "facebook" else patch.object(module.asyncio, "to_thread", new=AsyncMock(return_value=post))
                        message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="Jwander", global_name="demo"),
                                                        channel=types.SimpleNamespace(name="justaway"), content=f"||{url}||" if spoiler else url)
                        with fetch_patch, patch.object(module, "_download_bytes", new=AsyncMock(return_value=b"synthetic image")), patch.object(
                            module, "cleanup_source_message", new=AsyncMock()
                        ), patch("discord_bot.features.social_preview.sender.send_preview_as_author", new=AsyncMock(return_value=types.SimpleNamespace(id=123))) as send, self.assertLogs("discord_digest_bot", level="INFO") as logs:
                            self.assertTrue(await getattr(module, f"handle_{platform}_in_message")(message))
                        self.assertEqual([r.getMessage() for r in logs.records],
                                         [f"Jwander 在 justaway 貼了 {platform.capitalize()} url {url}"])
                        view = send.await_args.kwargs["view"]
                        texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
                        self.assertEqual(len(texts), 1)  # Header only; no empty/placeholder body.
                        self.assertNotIn("無文字內容", texts[0])
                        self.assertTrue(all(text.strip() for text in texts))
                        self.assertEqual(len(media_items(view)), 1)
                        self.assertEqual(media_items(view)[0].spoiler, spoiler)
                        self.assertIn(url, [item.url for item in view.walk_children() if isinstance(item, discord.ui.Button)])

    def test_selection_keeps_first_four_distinct_trusted_complete_media(self):
        unsafe = [ThreadsMedia("image", "https://evil.example/a.jpg"),
                  ThreadsMedia("video", VIDEOS[0] + "&bytestart=0"),
                  ThreadsMedia("video", "https://video.xx.fbcdn.net/manifest.mpd")]
        valid = [ThreadsMedia("image", IMAGES[0]), ThreadsMedia("video", VIDEOS[0]),
                 ThreadsMedia("image", IMAGES[1]), ThreadsMedia("video", VIDEOS[1])]
        self.assertEqual(gallery.select_media([*unsafe, valid[0], *valid, ThreadsMedia("image", IMAGES[2])]), valid)

    def test_existing_direct_video_formats_and_signed_encoding_are_preserved(self):
        items = [ThreadsMedia("video", "https://scontent.cdninstagram.com/video" + ext + "?signature=a%2Bb")
                 for ext in ("%2Emp4", ".m4v", ".webm", ".mov")]
        self.assertEqual(gallery.select_media(items), items)

    async def test_image_budget_exhaustion_keeps_video_in_fourth_slot(self):
        download = AsyncMock(side_effect=[b"a" * 8, b"b" * 7])
        media = [ThreadsMedia("image", u) for u in IMAGES[:3]] + [ThreadsMedia("video", VIDEOS[0])]
        with patch.object(gallery, "MAX_GALLERY_BYTES", 15), patch.object(gallery, "MAX_IMAGE_DOWNLOAD_BYTES", 10):
            result = await gallery.prepare_gallery(None, media, downloader=download, prefix="test", spoiler=True)
        self.assertEqual([c.args[1] for c in download.await_args_list], IMAGES[:2])
        self.assertEqual([c.kwargs["max_bytes"] for c in download.await_args_list], [10, 7])
        self.assertEqual(result.media_urls, ["attachment://SPOILER_test_1.jpg", "attachment://SPOILER_test_2.jpg", VIDEOS[0]])
        self.assertTrue(all(f.spoiler for f in result.files))
        for file in result.files:
            file.close()

    async def test_failed_image_preserves_order_without_fetching_fifth(self):
        download = AsyncMock(side_effect=[None, b"second", b"fourth"])
        media = [ThreadsMedia("image", IMAGES[0]), ThreadsMedia("image", IMAGES[1]),
                 ThreadsMedia("video", VIDEOS[0]), ThreadsMedia("image", IMAGES[2]), ThreadsMedia("image", IMAGES[3])]
        result = await gallery.prepare_gallery(None, media, downloader=download, prefix="test")
        self.assertEqual([c.args[1] for c in download.await_args_list], IMAGES[:3])
        self.assertEqual(result.media_urls, ["attachment://test_2.jpg", VIDEOS[0], "attachment://test_4.jpg"])
        for file in result.files:
            file.close()

    async def test_all_image_posts_keep_four_download_attempts(self):
        download = AsyncMock(side_effect=[None, b"second", None, b"fourth"])
        result = await gallery.prepare_gallery(None, [ThreadsMedia("image", u) for u in IMAGES], downloader=download, prefix="test")
        self.assertEqual([c.args[1] for c in download.await_args_list], IMAGES[:4])
        self.assertEqual([f.filename for f in result.files], ["test_2.jpg", "test_4.jpg"])
        for file in result.files:
            file.close()

    async def test_exception_closes_prepared_image_files(self):
        download = AsyncMock(side_effect=[b"image", RuntimeError("download failed")])
        created = []
        real_file = discord.File
        def make_file(*args, **kwargs):
            file = real_file(*args, **kwargs)
            created.append(file)
            return file
        with patch.object(gallery.discord, "File", side_effect=make_file):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                await gallery.prepare_gallery(None, [ThreadsMedia("image", u) for u in IMAGES[:2]], downloader=download, prefix="test")
        self.assertTrue(created[0].fp.closed)

    async def test_every_platform_displays_first_four_mixed_media_in_source_order(self):
        cases = [
            (threads_preview, ThreadsPost, ThreadsMedia, "threads", "https://www.threads.com/@demo/post/ABC123"),
            (instagram_preview, InstagramPost, InstagramMedia, "instagram", "https://www.instagram.com/p/ABC123"),
            (facebook_preview, FacebookPostData, FacebookMedia, "facebook", "https://www.facebook.com/demo/posts/pfbidTEST"),
        ]
        orders = [
            [("image", IMAGES[0]), ("image", IMAGES[1]), ("video", VIDEOS[0]), ("image", IMAGES[2]), ("image", IMAGES[3])],
            [("video", VIDEOS[0]), ("image", IMAGES[0]), ("video", VIDEOS[1]), ("video", VIDEOS[2]), ("video", VIDEOS[3])],
        ]
        for module, post_class, media_class, platform, url in cases:
            for order in orders:
                for spoiler in (False, True):
                    with self.subTest(platform=platform, order=order, spoiler=spoiler):
                        extra = {"shortcode": "ABC123"} if platform == "instagram" else {}
                        post = post_class(url=url, media=[media_class(*item) for item in order], **extra)
                        fetch_patch = patch.object(module, "_fetch_og_data", new=AsyncMock(return_value=post)) if platform == "facebook" else patch.object(module.asyncio, "to_thread", new=AsyncMock(return_value=post))
                        message = types.SimpleNamespace(author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                                                        channel=types.SimpleNamespace(name="general"), content="留言 " + (f"||{url}||" if spoiler else url))
                        with fetch_patch, patch.object(module, "_download_bytes", new=AsyncMock(return_value=b"synthetic image")) as download, patch.object(
                            module, "cleanup_source_message", new=AsyncMock()
                        ) as cleanup, patch("discord_bot.features.social_preview.sender.send_preview_as_author", new=AsyncMock(return_value=types.SimpleNamespace(id=123))) as send:
                            self.assertTrue(await getattr(module, f"handle_{platform}_in_message")(message))
                        kwargs = send.await_args.kwargs
                        files = iter(kwargs.get("files", []))
                        expected = [f"attachment://{next(files).filename}" if kind == "image" else value for kind, value in order[:4]]
                        items = media_items(kwargs["view"])
                        self.assertEqual([item.media.url for item in items], expected)
                        self.assertEqual([call.args[1] for call in download.await_args_list], [value for kind, value in order[:4] if kind == "image"])
                        self.assertTrue(all(item.spoiler == spoiler for item in items))
                        self.assertEqual(kwargs["view"].children[0].content, "留言")
                        cleanup.assert_awaited_once()
                        for file in kwargs.get("files", []):
                            file.close()

    async def test_every_platform_url_mode_keeps_mixed_order_without_download(self):
        order = [("image", IMAGES[0]), ("video", VIDEOS[0]), ("image", IMAGES[1]), ("video", VIDEOS[1]), ("image", IMAGES[2])]
        for module, cls, media_cls, name, url in [
            (threads_preview, ThreadsPost, ThreadsMedia, "threads", "https://www.threads.com/@demo/post/ABC123"),
            (instagram_preview, InstagramPost, InstagramMedia, "instagram", "https://www.instagram.com/p/ABC123"),
            (facebook_preview, FacebookPostData, FacebookMedia, "facebook", "https://www.facebook.com/demo/posts/pfbidTEST"),
        ]:
            with self.subTest(platform=name):
                extra = {"shortcode": "ABC123"} if name == "instagram" else {}
                post = cls(url=url, media=[media_cls(*item) for item in order], **extra)
                fetch_patch = patch.object(module, "_fetch_og_data", new=AsyncMock(return_value=post)) if name == "facebook" else patch.object(module.asyncio, "to_thread", new=AsyncMock(return_value=post))
                with fetch_patch, patch.object(module, "_download_bytes", new=AsyncMock()) as download:
                    result = await getattr(module, f"build_{name}_preview")(url, reupload_image=False)
                self.assertEqual(result.media_urls, [value for kind, value in order[:4]])
                self.assertEqual(result.image_urls, IMAGES[:2])
                download.assert_not_awaited()
