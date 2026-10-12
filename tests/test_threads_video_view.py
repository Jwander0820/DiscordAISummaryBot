import importlib.util
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import isolated_discord_suite

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

if importlib.util.find_spec("discord") is None or importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("Components V2 tests require discord.py and aiohttp")

import discord
from discord_bot.features.social_preview import threads_preview as preview_module
from discord_bot.features.social_preview.threads_fetch import ThreadsMedia, ThreadsPost

load_tests = isolated_discord_suite(__name__)

# Other suites install a process-wide fake Discord module. The load_tests hook
# runs this entire suite with the real SDK before importing its V2 classes.
if getattr(discord, "__version__", None):
    from discord.http import handle_message_parameters
    from discord_bot.features.social_preview.preview_view import build_social_preview_views


POST_URL = "https://www.threads.com/@demo/post/Video001"
VIDEO_URL = "https://scontent.cdninstagram.com/first.mp4?signature=" + "a" * 2100


def make_views(**overrides):
    args = dict(
        original_url=POST_URL, video_url=VIDEO_URL, title="Threads @demo", author="作者",
        text="貼文內容", commentary="原留言", spoiler=False, timeout=60,
    )
    args.update(overrides)
    return build_social_preview_views(**args)


def media_items(view):
    return [entry for item in view.walk_children() if isinstance(item, discord.ui.MediaGallery) for entry in item.items]


def make_source(content):
    return types.SimpleNamespace(
        author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
        channel=types.SimpleNamespace(name="general"), content=content, reply=AsyncMock(),
    )


class SocialPreviewViewTests(unittest.IsolatedAsyncioTestCase):
    def test_real_sdk_payload_has_one_external_video_and_no_legacy_fields_or_uploads(self):
        view, = make_views()
        with handle_message_parameters(view=view, allowed_mentions=discord.AllowedMentions.none()) as params:
            self.assertEqual(params.payload["flags"] & (1 << 15), 1 << 15)
            self.assertFalse(set(params.payload) & {"content", "embeds", "attachments"})
            self.assertFalse(params.files)
            self.assertFalse(params.multipart)
            self.assertEqual(params.payload["allowed_mentions"]["parse"], [])
            items = media_items(view)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].media.url, VIDEO_URL)
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertIsNone(buttons[0].url)
        self.assertEqual(str(buttons[0].emoji), "🗑️")
        self.assertEqual(buttons[1].url, POST_URL)
        self.assertEqual([button.url for button in buttons if button.url], [POST_URL])
        self.assertTrue(view.is_dispatchable())

    def test_author_title_commentary_and_canonical_link_are_preserved(self):
        canonical = "https://www.threads.com/@actual/post/Other123"
        view, = make_views(original_url=canonical, video_url="https://scontent.cdninstagram.com/first.mp4")
        self.assertEqual(view.children[0].content, "原留言")
        texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        self.assertIn(f"**作者**\n[Threads @demo](<{canonical}>)", texts)
        self.assertIn("貼文內容", texts)
        links = {item.label: item.url for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.url}
        self.assertEqual(links["原連結"], canonical)
        self.assertEqual(links["影片直連"], "https://scontent.cdninstagram.com/first.mp4")
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([button.url for button in buttons], [None, canonical, links["影片直連"]])

    def test_threads_username_underscores_are_not_escaped_inside_link_label(self):
        for username in ("bucky_su", "a_b_c", "a__b", "_leading_", "name.with.dots"):
            with self.subTest(username=username):
                url = f"https://www.threads.com/@{username}/post/DeY0uPKj92t"
                view, = make_views(original_url=url, video_url=None, title=f"Threads @{username}", author=f"@{username}")
                texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
                self.assertTrue(any(f"[Threads @{username}](<{url}>)" in text for text in texts))

    def test_link_label_escapes_delimiters_but_keeps_ordinary_punctuation(self):
        title = "Name_One [special]\\test\nnext"
        view, = make_views(title=title, author="*name*")
        header = next(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay) and "Name_One" in item.content)
        self.assertIn(r"[Name_One \[special\]\\test next](<", header)
        self.assertIn(r"**\*name\***", header)
        self.assertTrue(header.endswith(f"(<{POST_URL}>)"))

    def test_native_spoiler_hides_post_even_with_embedded_spoiler_markers(self):
        body = "劇透前 ||內文自己的遮罩|| 劇透後"
        view, = make_views(text=body, spoiler=True)
        self.assertEqual(view.children[0].content, "原留言")
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertTrue(card.spoiler)
        self.assertTrue(media_items(view)[0].spoiler)
        self.assertIn(body, [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
        controls = next(item for item in view.children if isinstance(item, discord.ui.ActionRow))
        self.assertIsNone(controls.children[0].url)
        self.assertEqual(controls.children[1].url, POST_URL)
        self.assertTrue(any(item.url == POST_URL for item in controls.children))
        self.assertTrue(any(item.url is None for item in controls.children))
        self.assertFalse(any(isinstance(item, discord.ui.ActionRow) for item in card.children))

    def test_long_commentary_and_body_survive_utf16_safe_splitting(self):
        commentary = "😀評" * 1500
        body = "🎥正文" * 1200
        for spoiler in (False, True):
            with self.subTest(spoiler=spoiler):
                views = make_views(commentary=commentary, text=body, spoiler=spoiler)
                comments, bodies = [], []
                for view in views:
                    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
                    self.assertIsNone(buttons[0].url)
                    self.assertEqual(buttons[1].url, POST_URL)
                    texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
                    self.assertLessEqual(sum(len(text.encode("utf-16-le")) // 2 for text in texts), 4000)
                    self.assertLessEqual(view.content_length(), 4000)
                    comments.extend(item.content for item in view.children if isinstance(item, discord.ui.TextDisplay))
                    for card in (item for item in view.children if isinstance(item, discord.ui.Container)):
                        self.assertEqual(card.spoiler, spoiler)
                        bodies.extend(item.content for item in card.children[1:] if isinstance(item, discord.ui.TextDisplay))
                self.assertEqual("".join(comments), commentary)
                self.assertEqual("".join(bodies), body)
                self.assertEqual([item.media.url for view in views for item in media_items(view)], [VIDEO_URL])

    def test_exact_boundary_and_astral_body_do_not_produce_empty_pages(self):
        for size in (0, 3900, 3999, 4000, 4001, 8000):
            with self.subTest(size=size):
                views = make_views(commentary="評" * size, text="😀")
                self.assertEqual(sum(len(media_items(view)) for view in views), 1)
                self.assertTrue(all(view.content_length() > 0 for view in views))
                self.assertTrue(all(view.content_length() <= 4000 for view in views))

    async def test_timeout_disables_delete_but_keeps_card_media_and_links(self):
        view, = make_views(spoiler=True)
        view.message = types.SimpleNamespace(edit=AsyncMock())
        before_url = media_items(view)[0].media.url
        await view.on_timeout()
        view.message.edit.assert_awaited_once()
        self.assertIs(view.message.edit.await_args.kwargs["view"], view)
        self.assertEqual(media_items(view)[0].media.url, before_url)
        for item in view.walk_children():
            if isinstance(item, discord.ui.Button):
                self.assertEqual(item.disabled, item.url is None)
        self.assertEqual(view.message.edit.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_delete_button_removes_related_messages_and_tolerates_missing_one(self):
        view, = make_views()
        view.related_message_ids = [12, 13]
        missing = discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "missing")
        related = types.SimpleNamespace(delete=AsyncMock())
        channel = types.SimpleNamespace(fetch_message=AsyncMock(side_effect=[missing, related]))
        interaction = types.SimpleNamespace(
            response=types.SimpleNamespace(send_message=AsyncMock()),
            message=types.SimpleNamespace(delete=AsyncMock(), channel=channel),
            user=types.SimpleNamespace(global_name="tester"),
        )
        button = next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.url is None)
        await button.callback(interaction)
        interaction.message.delete.assert_awaited_once()
        related.delete.assert_awaited_once()
        self.assertEqual([call.args[0] for call in channel.fetch_message.await_args_list], [12, 13])
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_already_deleted_preview_and_timeout_do_not_raise(self):
        view, = make_views()
        missing = discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "missing")
        view.message = types.SimpleNamespace(edit=AsyncMock(side_effect=missing))
        await view.on_timeout()
        interaction = types.SimpleNamespace(
            response=types.SimpleNamespace(send_message=AsyncMock()),
            message=types.SimpleNamespace(delete=AsyncMock(side_effect=missing), channel=None),
            user=types.SimpleNamespace(global_name="tester"),
        )
        await view._delete(interaction)

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_linked_instagram_images_reach_first_image_v2_upload(self, fetch, download, send, cleanup):
        from tests.test_threads_linked_images import FIXTURE, IMAGES, URL
        from discord_bot.features.social_preview.threads_fetch import _parse_html

        fetch.return_value = _parse_html(URL, FIXTURE)
        download.return_value = b"synthetic image bytes"
        send.return_value = types.SimpleNamespace(id=123)
        source = make_source(f"評論 ||{URL}||")
        self.assertTrue(await preview_module.handle_threads_in_message(source))
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args[1], IMAGES[0])
        send.assert_awaited_once()
        view = send.await_args.kwargs["view"]
        files = send.await_args.kwargs["files"]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].spoiler)
        self.assertEqual([item.media.url for item in media_items(view)], [f"attachment://{files[0].filename}"])
        headers = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        self.assertTrue(any("[Threads @demo_user](<" in text for text in headers))
        self.assertEqual(view.children[0].content, "評論")
        cleanup.assert_awaited_once()

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_handler_uses_only_first_video_and_cleans_source_after_all_pages(self, fetch, download, send, cleanup):
        fetch.return_value = ThreadsPost(url=POST_URL, text="正" * 4096, author_username="demo", media=[
            ThreadsMedia("image", "https://scontent.cdninstagram.com/poster.jpg"),
            ThreadsMedia("video", VIDEO_URL), ThreadsMedia("video", "https://scontent.cdninstagram.com/second.mp4"),
        ])
        sent = []

        async def send_page(message, **kwargs):
            cleanup.assert_not_awaited()
            self.assertEqual(set(kwargs), {"view"})
            item = types.SimpleNamespace(id=len(sent) + 10, delete=AsyncMock())
            sent.append(item)
            return item

        send.side_effect = send_page
        source = make_source("評" * 3900 + " " + POST_URL)
        self.assertTrue(await preview_module.handle_threads_in_message(source))
        download.assert_not_awaited()
        views = [call.kwargs["view"] for call in send.await_args_list]
        self.assertGreater(len(views), 1)
        self.assertEqual([item.media.url for view in views for item in media_items(view)], [VIDEO_URL])
        for view in views:
            self.assertEqual(view.related_message_ids, [item.id for item in sent if item.id != view.message.id])
        cleanup.assert_awaited_once_with(source, platform="Threads", url=POST_URL)

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_partial_send_failure_removes_partial_card_and_preserves_source(self, fetch, send, cleanup):
        fetch.return_value = ThreadsPost(url=POST_URL, text="正" * 4096, media=[ThreadsMedia("video", VIDEO_URL)])
        sent = types.SimpleNamespace(id=10, delete=AsyncMock())
        send.side_effect = [sent, RuntimeError("simulated send failure")]
        source = make_source(POST_URL)
        with self.assertLogs("discord_digest_bot", level="ERROR"):
            self.assertFalse(await preview_module.handle_threads_in_message(source))
        cleanup.assert_not_awaited()
        sent.delete.assert_awaited_once()
        source.reply.assert_awaited_once()
        self.assertTrue(send.await_args_list[0].kwargs["view"].is_finished())

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_image_and_text_posts_use_v2_gallery_and_native_spoiler(self, fetch, download, send, cleanup):
        for has_image in (False, True):
            for spoiler in (False, True):
                with self.subTest(has_image=has_image, spoiler=spoiler):
                    send.reset_mock()
                    download.reset_mock()
                    fetch.return_value = ThreadsPost(url=POST_URL, text="貼文", media=(
                        [ThreadsMedia("image", "https://scontent.cdninstagram.com/image.jpg")] if has_image else []
                    ))
                    download.return_value = b"image bytes"
                    send.side_effect = None
                    send.return_value = types.SimpleNamespace(id=12)
                    source = make_source("留言 " + (f"||{POST_URL}||" if spoiler else POST_URL))
                    self.assertTrue(await preview_module.handle_threads_in_message(source))
                    first = send.await_args_list[0].kwargs
                    self.assertFalse(set(first) & {"embed", "content"})
                    view = first["view"]
                    self.assertTrue(view.has_components_v2())
                    self.assertEqual(view.children[0].content, "留言")
                    card = next(item for item in view.children if isinstance(item, discord.ui.Container))
                    self.assertEqual(card.spoiler, spoiler)
                    self.assertIn("貼文", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
                    self.assertEqual(download.await_count, int(has_image))
                    if has_image:
                        files = send.await_args_list[-1].kwargs["files"]
                        self.assertEqual(len(files), 1)
                        self.assertEqual(files[0].spoiler, spoiler)
                        self.assertEqual(media_items(view)[0].media.url, f"attachment://{files[0].filename}")
                        self.assertEqual(media_items(view)[0].spoiler, spoiler)
                    else:
                        self.assertEqual(media_items(view), [])
                    self.assertEqual(send.await_count, 1)
