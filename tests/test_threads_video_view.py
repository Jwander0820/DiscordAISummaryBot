import importlib.util
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
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
from discord_bot.features.social_preview.text import TextSpan

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
    async def test_cancel_during_registration_rolls_back_pages_and_share_index(self):
        from discord_bot.features.social_preview import sender
        views = make_views(video_url=None)
        started = asyncio.Event()
        sent = types.SimpleNamespace(id=1100, delete=AsyncMock())
        message = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=10))
        async def register(messages):
            started.set()
            await asyncio.Event().wait()
        deleted = AsyncMock(return_value=True)
        with patch.object(sender, "send_preview_as_author", AsyncMock(return_value=sent)):
            task = asyncio.create_task(sender.send_preview_group(message, views,
                on_delivered=register, on_deleted=deleted))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        sent.delete.assert_awaited_once()
        deleted.assert_awaited_once_with(1, 10, [1100])
        self.assertTrue(views[0].delivery_complete.is_set())

    async def test_repeated_cancel_waits_for_rollback_deletions_and_index_cleanup(self):
        from discord_bot.db.social_duplicate_repository import ShareEvent
        from discord_bot.features.social_preview import duplicate_service, sender

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(directory) / "shares.db"),
            "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7",
        }):
            service = duplicate_service.SocialDuplicateService()
            views = make_views(text="文" * 9000, video_url=None)
            registered = asyncio.Event()
            deleting_tail, release_tail = asyncio.Event(), asyncio.Event()
            cleaning_index, release_index = asyncio.Event(), asyncio.Event()
            first = ShareEvent("1", "10", "threads", "100", "42", "Video001", POST_URL, "1100", time.time())
            message = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=10))
            messages = [types.SimpleNamespace(id=1100 + i, delete=AsyncMock()) for i in range(len(views))]

            async def register(sent):
                await service.register(first)
                registered.set()
                await asyncio.Event().wait()

            async def delete_tail():
                deleting_tail.set()
                await release_tail.wait()

            async def forget(guild, channel, ids):
                cleaning_index.set()
                await release_index.wait()
                return await service.forget_previews(guild, channel, ids)

            messages[1].delete.side_effect = delete_tail
            deleted = AsyncMock(side_effect=forget)
            task = None
            try:
                await service.rebuild()
                with patch.object(sender, "send_preview_as_author", AsyncMock(side_effect=messages)):
                    task = asyncio.create_task(sender.send_preview_group(message, views,
                        on_delivered=register, on_deleted=deleted))
                    await asyncio.wait_for(registered.wait(), 3)
                    task.cancel()
                    await asyncio.wait_for(deleting_tail.wait(), 3)
                    messages[0].delete.assert_awaited_once()
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "repeated cancellation interrupted rollback")
                    self.assertFalse(views[0].delivery_complete.is_set())
                    deleted.assert_not_awaited()
                    release_tail.set()
                    await asyncio.wait_for(cleaning_index.wait(), 3)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "cancellation interrupted index synchronization")
                    self.assertFalse(views[0].delivery_complete.is_set())
                    release_index.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                deleted.assert_awaited_once_with(1, 10, [item.id for item in messages])
                self.assertTrue(all(view.delivery_complete.is_set() and not view.files for view in views))
                self.assertEqual(service.repository.execute("SELECT COUNT(*) FROM social_share_events"), [(0,)])
                self.assertNotIn(first.key, service.cache)
                repost = ShareEvent("1", "10", "threads", "101", "42", "Video001", POST_URL, "1200", time.time())
                self.assertIsNone(await service.register(repost))
            finally:
                release_tail.set()
                release_index.set()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                await service.close()

    async def test_early_delete_waits_for_all_pages_and_registration(self):
        from discord_bot.features.social_preview import sender
        views = make_views(text="文" * 9000, video_url=None)
        second_started, release = asyncio.Event(), asyncio.Event()
        messages = [types.SimpleNamespace(id=1100 + i, delete=AsyncMock()) for i in range(len(views))]
        channel = types.SimpleNamespace(id=10, fetch_message=AsyncMock(
            side_effect=lambda number: messages[number - 1100]))
        for item in messages:
            item.channel = channel
        order = []
        async def send(message, **kwargs):
            index = views.index(kwargs["view"])
            if index == 1:
                second_started.set()
                await release.wait()
            return messages[index]
        async def registered(sent):
            self.assertEqual(sent, messages)
            order.append("registered")
        async def deleted(guild, channel, ids):
            self.assertEqual(order, ["registered"])
            self.assertEqual(set(ids), {item.id for item in messages})
            order.append("deleted")
            return True
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1), message=messages[0],
            user=types.SimpleNamespace(global_name="tester"),
            response=types.SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))
        with patch.object(sender, "send_preview_as_author", side_effect=send):
            sending = asyncio.create_task(sender.send_preview_group(None, views,
                on_delivered=registered, on_deleted=deleted))
            await asyncio.wait_for(second_started.wait(), 3)
            deleting = asyncio.create_task(views[0]._delete(interaction))
            await asyncio.sleep(0)
            messages[0].delete.assert_not_awaited()
            interaction.response.defer.assert_awaited_once()
            release.set()
            await sending
            await deleting
        self.assertEqual(order, ["registered", "deleted"])

    async def test_rollback_keeps_partial_delete_scope_and_original_failure(self):
        from discord_bot.features.social_preview import sender
        views = make_views(text="文" * 9000, video_url=None)
        forbidden = discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "denied")
        messages = [types.SimpleNamespace(id=1100 + i, delete=AsyncMock(
            side_effect=forbidden if i == 0 else None)) for i in range(len(views))]
        message = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=10))
        original = RuntimeError("synthetic registration failure")
        # Index errors must not replace the original delivery/registration error.
        deleted = AsyncMock(side_effect=OSError("synthetic index failure"))
        with patch.object(sender, "send_preview_as_author", AsyncMock(side_effect=messages)), \
                self.assertLogs("discord_digest_bot", level="WARNING"):
            with self.assertRaises(RuntimeError) as raised:
                await sender.send_preview_group(message, views,
                    on_delivered=AsyncMock(side_effect=original), on_deleted=deleted)
        self.assertIs(raised.exception, original)
        for item in messages:
            item.delete.assert_awaited_once()
        deleted.assert_awaited_once_with(1, 10, [item.id for item in messages[1:]])
        self.assertTrue(all(view.delivery_complete.is_set() and not view.files for view in views))

    async def test_cancellation_during_failure_rollback_is_propagated_after_cleanup(self):
        from discord_bot.features.social_preview import sender
        views = make_views(video_url=None)
        started, release = asyncio.Event(), asyncio.Event()
        async def remove():
            started.set()
            await release.wait()
        sent = types.SimpleNamespace(id=1100, delete=AsyncMock(side_effect=remove))
        message = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), channel=types.SimpleNamespace(id=10))
        deleted = AsyncMock(return_value=True)
        with patch.object(sender, "send_preview_as_author", AsyncMock(return_value=sent)):
            task = asyncio.create_task(sender.send_preview_group(message, views,
                on_delivered=AsyncMock(side_effect=RuntimeError("synthetic failure")), on_deleted=deleted))
            try:
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(views[0].delivery_complete.is_set())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        sent.delete.assert_awaited_once()
        deleted.assert_awaited_once_with(1, 10, [1100])
        self.assertTrue(views[0].delivery_complete.is_set())

    async def test_button_delete_then_repost_does_not_trigger_patrol(self):
        from discord_bot.db.social_duplicate_repository import ShareEvent, SocialDuplicateRepository
        from discord_bot.features.social_preview import duplicate_service

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DB_TYPE": "sqlite", "SQLITE_PATH": str(Path(directory) / "shares.db"),
            "SOCIAL_DUPLICATE_ENABLED": "1", "SOCIAL_DUPLICATE_WINDOW_DAYS": "7",
        }):
            service = duplicate_service.SocialDuplicateService(SocialDuplicateRepository())
            try:
                self.assertTrue(await service.rebuild())
                first = ShareEvent("1", "10", "threads", "100", "42", "Video001", POST_URL, "1100", time.time())
                await service.register(first)
                view, = make_views()
                view.on_deleted = service.forget_previews
                channel = types.SimpleNamespace(id=10)
                interaction = types.SimpleNamespace(
                    guild=types.SimpleNamespace(id=1),
                    response=types.SimpleNamespace(send_message=AsyncMock()),
                    followup=types.SimpleNamespace(send=AsyncMock()),
                    message=types.SimpleNamespace(id=1100, delete=AsyncMock(), channel=channel),
                    user=types.SimpleNamespace(global_name="tester"),
                )
                button = next(item for item in view.walk_children()
                              if isinstance(item, discord.ui.Button) and item.url is None)
                with patch.object(duplicate_service, "social_duplicate_service", service):
                    await button.callback(interaction)
                repost = ShareEvent("1", "10", "threads", "101", "42", "Video001", POST_URL, "1101", time.time())
                self.assertIsNone(await service.register(repost))
                self.assertEqual(service.counts[first.key], 1)
                rows = service.repository._execute("SELECT source_message_id FROM social_share_events")
                self.assertEqual(rows, [("101",)])
                self.assertTrue(await service.rebuild())
                self.assertEqual(service.cache[first.key], repost)
            finally:
                await service.close()

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

    def test_manual_spoiler_masks_body_and_media_but_keeps_header_and_controls_visible(self):
        body = "劇透前 ||內文自己的遮罩|| 劇透後"
        view, = make_views(text=body, spoiler=True)
        self.assertEqual(view.children[0].content, "原留言")
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertFalse(card.spoiler)
        self.assertTrue(media_items(view)[0].spoiler)
        texts = [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)]
        self.assertIn(r"||劇透前 \|\|內文自己的遮罩\|\| 劇透後||", texts)
        self.assertIn(f"**作者**\n[Threads @demo](<{POST_URL}>)", texts)
        controls = next(item for item in card.children if isinstance(item, discord.ui.ActionRow))
        self.assertIsNone(controls.children[0].url)
        self.assertEqual(controls.children[1].url, POST_URL)
        self.assertTrue(any(item.url == POST_URL for item in controls.children))
        self.assertTrue(any(item.url is None for item in controls.children))
        self.assertFalse(any(isinstance(item, discord.ui.ActionRow) for item in view.children))

    def test_unforced_partial_text_spoiler_is_preserved_without_masking_media(self):
        body = "公開文字 ||原文防雷段落|| 其他公開文字"
        view, = make_views(text=body)
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertFalse(card.spoiler)
        self.assertIn(body, [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
        self.assertTrue(all(not item.spoiler for item in media_items(view)))

    def test_native_spans_and_individual_media_flags_serialize_with_manual_override(self):
        spans = [TextSpan("公開 "), TextSpan("雷文 `code` ||字面符號||", True), TextSpan(" 尾段")]
        urls = ["https://scontent.cdninstagram.com/public.jpg", VIDEO_URL]
        for manual in (False, True):
            with self.subTest(manual=manual):
                view, = make_views(text_spans=spans, media_urls=urls, media_spoilers=[False, True], spoiler=manual)
                with handle_message_parameters(view=view, allowed_mentions=discord.AllowedMentions.none()) as params:
                    card = next(c for c in params.payload["components"] if c["type"] == 17)
                    self.assertFalse(card["spoiler"])
                    self.assertIn("Threads", card["components"][0]["content"])
                    body = card["components"][1]["content"]
                    self.assertEqual(body, r"||公開 雷文 \`code\` \|\|字面符號\|\| 尾段||" if manual else
                                     r"公開 ||雷文 \`code\` \|\|字面符號\|\||| 尾段")
                    gallery = next(c for c in card["components"] if c["type"] == 12)
                    self.assertEqual([m["spoiler"] for m in gallery["items"]], [manual, True])

    def test_native_mask_is_closed_on_each_page_and_public_text_stays_public(self):
        spans = [TextSpan("公開😀" * 1400), TextSpan("隱藏\\`||😀\n" * 1600, True), TextSpan("公開結尾" * 1400)]
        expected = [(char, span.spoiler) for span in spans for char in span.text]
        views = make_views(text_spans=spans, commentary="評" * 3990, media_urls=[VIDEO_URL], media_spoilers=[True])
        actual = []
        for view in views:
            texts = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
            self.assertLessEqual(sum(len(t.encode("utf-16-le")) // 2 for t in texts), 4000)
            for card in (item for item in view.children if isinstance(item, discord.ui.Container)):
                for item in card.children[1:]:
                    if not isinstance(item, discord.ui.TextDisplay):
                        continue
                    text, index, masked = item.content, 0, False
                    while index < len(text):
                        if text[index] == "\\":
                            actual.append((text[index + 1], masked))
                            index += 2
                        elif text[index:index + 2] == "||":
                            masked = not masked
                            index += 2
                        else:
                            actual.append((text[index], masked))
                            index += 1
                    self.assertFalse(masked, "Each page must close its own native spoiler")
        self.assertEqual(actual, expected)
        self.assertEqual(sum(len(media_items(view)) for view in views), 1)

    def test_native_media_flags_must_match_media_items(self):
        with self.assertRaisesRegex(ValueError, "flags must match"):
            make_views(media_spoilers=[])

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_native_source_fixtures_reach_discord_with_correct_masks(self, fetch, download, send, cleanup):
        from tests.test_threads_spoilers import VIDEO_HTML, TEXT_HTML, VIDEO_URL as post_video_url, TEXT_URL
        from discord_bot.features.social_preview.threads_fetch import _parse_html
        for url, fixture in ((post_video_url, VIDEO_HTML), (TEXT_URL, TEXT_HTML)):
            for manual in (False, True):
                with self.subTest(url=url, manual=manual):
                    post = _parse_html(url, fixture)
                    fetch.return_value = post
                    download.return_value = b"synthetic image"
                    send.return_value = types.SimpleNamespace(id=123)
                    source = make_source("評論 " + (f"||{url}||" if manual else url))
                    self.assertTrue(await preview_module.handle_threads_in_message(source))
                    view = send.await_args.kwargs["view"]
                    card = next(c for c in view.children if isinstance(c, discord.ui.Container))
                    body = card.children[1].content
                    self.assertFalse(card.spoiler)
                    if url == post_video_url:
                        self.assertEqual(body, "||公開影片說明||" if manual else "公開影片說明")
                    else:
                        self.assertEqual(body, "||公開第一行\n隱藏一\n公開第二行 😀\n隱藏二||" if manual else
                                         "公開第一行\n||隱藏一||\n公開第二行 😀\n||隱藏二||")
                        self.assertEqual(send.await_args.kwargs["files"][0].spoiler, manual)
                    self.assertEqual([m.spoiler for m in media_items(view)], [manual or url == post_video_url])
                    self.assertEqual(view.children[0].content, "評論")
                    self.assertIn(url, [b.url for b in view.walk_children() if isinstance(b, discord.ui.Button)])

    def test_manual_spoiler_closes_safely_across_escape_and_emoji_page_boundaries(self):
        baseline, = make_views(text="", commentary="", spoiler=True)
        header = next(item for item in baseline.children if isinstance(item, discord.ui.Container)).children[0].content
        budget = 4000 - len(header.encode("utf-16-le")) // 2 - 4
        for ending in ("\\", "||", "`", "```\n", "😀", "[link](https://example.com/a_b)"):
            # Put an escape or astral character exactly across the first cut.
            body = ("文" * (budget - 1) + ending + "結尾\n") * 3
            with self.subTest(ending=ending):
                views = make_views(text=body, commentary="", spoiler=True)
                parts = []
                for view in views:
                    card = next(item for item in view.children if isinstance(item, discord.ui.Container))
                    for item in card.children[1:]:
                        if not isinstance(item, discord.ui.TextDisplay):
                            continue
                        self.assertTrue(item.content.startswith("||") and item.content.endswith("||"))
                        inner = item.content[2:-2]
                        self.assertEqual((len(inner) - len(inner.rstrip("\\"))) % 2, 0)
                        parts.append(inner)
                    self.assertLessEqual(sum(len(item.content.encode("utf-16-le")) // 2
                                             for item in view.walk_children()
                                             if isinstance(item, discord.ui.TextDisplay)), 4000)
                self.assertEqual("".join(parts), discord.utils.escape_markdown(body, ignore_links=False))

    def test_manual_spoiler_does_not_create_body_for_media_only_post(self):
        view, = make_views(text="", spoiler=True)
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertFalse(card.spoiler)
        self.assertEqual(len([item for item in card.children if isinstance(item, discord.ui.TextDisplay)]), 1)
        self.assertTrue(all(item.spoiler for item in media_items(view)))

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
                        self.assertFalse(card.spoiler)
                        bodies.extend(item.content[2:-2] if spoiler else item.content
                                      for item in card.children[1:] if isinstance(item, discord.ui.TextDisplay))
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

    async def test_continuation_button_clears_only_confirmed_deleted_share_reference(self):
        from discord_bot.features.social_preview import duplicate_service, sender

        for failed_id in (None, 1100, 1102):
            with self.subTest(failed_id=failed_id):
                view, = make_views()
                view.related_message_ids = [1100, 1102]
                forbidden = discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "denied")
                main = types.SimpleNamespace(delete=AsyncMock(side_effect=forbidden if failed_id == 1100 else None))
                tail = types.SimpleNamespace(delete=AsyncMock(side_effect=forbidden if failed_id == 1102 else None))
                channel = types.SimpleNamespace(id=10, fetch_message=AsyncMock(side_effect=[main, tail]))
                interaction = types.SimpleNamespace(
                    guild=types.SimpleNamespace(id=1),
                    response=types.SimpleNamespace(send_message=AsyncMock()),
                    message=types.SimpleNamespace(id=1101, channel=channel, delete=AsyncMock()),
                    user=types.SimpleNamespace(global_name="tester"),
                )
                service = types.SimpleNamespace(forget_previews=AsyncMock(return_value=True))
                view.on_deleted = service.forget_previews
                with patch.object(duplicate_service, "social_duplicate_service", service):
                    await view._delete(interaction)
                expected = [1101] + [item for item in (1100, 1102) if item != failed_id]
                service.forget_previews.assert_awaited_once_with(1, 10, expected)
                # Automatic source cleanup must never clear the share index.
                service.forget_previews.reset_mock()
                with patch.object(duplicate_service, "social_duplicate_service", service):
                    await sender.cleanup_source_message(
                        types.SimpleNamespace(delete=AsyncMock()), platform="Threads", url=POST_URL)
                service.forget_previews.assert_not_awaited()

    async def test_failed_button_delete_keeps_share_and_database_failure_is_reported(self):
        from discord_bot.features.social_preview import duplicate_service

        for deletion_failed in (True, False):
            with self.subTest(deletion_failed=deletion_failed):
                view, = make_views()
                forbidden = discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "denied")
                interaction = types.SimpleNamespace(
                    guild=types.SimpleNamespace(id=1),
                    response=types.SimpleNamespace(send_message=AsyncMock()),
                    followup=types.SimpleNamespace(send=AsyncMock()),
                    message=types.SimpleNamespace(id=1100, channel=types.SimpleNamespace(id=10),
                        delete=AsyncMock(side_effect=forbidden if deletion_failed else None)),
                    user=types.SimpleNamespace(global_name="tester"),
                )
                service = types.SimpleNamespace(forget_previews=AsyncMock(return_value=False))
                view.on_deleted = service.forget_previews
                with patch.object(duplicate_service, "social_duplicate_service", service):
                    await view._delete(interaction)
                if deletion_failed:
                    service.forget_previews.assert_not_awaited()
                    interaction.followup.send.assert_not_awaited()
                else:
                    service.forget_previews.assert_awaited_once_with(1, 10, [1100])
                    interaction.followup.send.assert_awaited_once()
                    self.assertIn("分享紀錄清理失敗", interaction.followup.send.await_args.args[0])
                    self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_linked_instagram_images_reach_ordered_v2_gallery(self, fetch, download, send, cleanup):
        from tests.test_threads_linked_images import FIXTURE, IMAGES, URL
        from discord_bot.features.social_preview.threads_fetch import _parse_html

        fetch.return_value = _parse_html(URL, FIXTURE)
        download.return_value = b"synthetic image bytes"
        send.return_value = types.SimpleNamespace(id=123)
        source = make_source(f"評論 ||{URL}||")
        self.assertTrue(await preview_module.handle_threads_in_message(source))
        self.assertEqual([call.args[1] for call in download.await_args_list], IMAGES)
        send.assert_awaited_once()
        view = send.await_args.kwargs["view"]
        files = send.await_args.kwargs["files"]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(file.spoiler for file in files))
        self.assertEqual([item.media.url for item in media_items(view)], [f"attachment://{file.filename}" for file in files])
        headers = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
        self.assertTrue(any("[Threads @demo_user](<" in text for text in headers))
        self.assertEqual(view.children[0].content, "評論")
        cleanup.assert_awaited_once()

    @patch.object(preview_module, "cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch.object(preview_module, "_download_bytes", new_callable=AsyncMock)
    @patch.object(preview_module.asyncio, "to_thread")
    async def test_handler_preserves_videos_and_cleans_source_after_all_pages(self, fetch, download, send, cleanup):
        fetch.return_value = ThreadsPost(url=POST_URL, text="正" * 4096, author_username="demo", media=[
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
        self.assertEqual([item.media.url for view in views for item in media_items(view)],
                         [VIDEO_URL, "https://scontent.cdninstagram.com/second.mp4"])
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
                    self.assertFalse(card.spoiler)
                    self.assertIn("||貼文||" if spoiler else "貼文", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
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
