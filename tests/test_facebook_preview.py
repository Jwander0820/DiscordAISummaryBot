import unittest
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord
from tests.support import isolated_discord_suite

load_tests = isolated_discord_suite(__name__)

from discord_bot.features.social_preview.facebook_preview import (
    _build_candidate_urls,
    _extract_og_data,
    _extract_page_data,
    _fetch_html_with_aiohttp,
    _fetch_og_data,
    build_facebook_preview,
    FacebookPreviewUnavailable,
    extract_facebook_urls,
    handle_facebook_in_message,
)


from discord_bot.features.social_preview.facebook_fetch import FacebookMedia, FacebookPostData


def post_data(title, description, images, videos, url, status=200):
    return FacebookPostData(url=url, title=title, description=description,
                            image_urls=images, video_urls=videos, status=status)


class FakeResponse:
    def __init__(self, url, html, *, status=200, headers=None):
        self.url = url
        self._html = html
        self.status = status
        self.headers = headers or {}
        self.content = types.SimpleNamespace(iter_chunked=self._chunks)

    async def _chunks(self, size):
        body = self._html.encode("utf-8")
        for start in range(0, len(body), size):
            yield body[start:start + size]

    async def text(self, *, errors):
        return self._html

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **_kwargs):
        self.requested_urls.append(url)
        return next(self._responses)


class FacebookPreviewTests(unittest.TestCase):
    def test_extract_urls_supports_mobile_redirect_host(self):
        content = "看看 https://lm.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Fshare%2Fp%2Fabc"

        self.assertEqual(
            extract_facebook_urls(content),
            ["https://lm.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Fshare%2Fp%2Fabc"],
        )

    def test_build_candidates_uses_resolved_content_path(self):
        candidates = _build_candidate_urls("https://www.facebook.com/example/posts/123?story_fbid=123")

        self.assertIn("https://m.facebook.com/example/posts/123?story_fbid=123", candidates)
        self.assertIn("https://mbasic.facebook.com/example/posts/123?story_fbid=123", candidates)

    def test_redirect_host_is_not_rewritten_before_resolution(self):
        url = "https://lm.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Fshare%2Fp%2Fabc"

        self.assertEqual(_build_candidate_urls(url), [url])

    def test_empty_html_does_not_report_placeholder_as_fetched_metadata(self):
        title, description, image_urls, video_urls = _extract_og_data("<html><body></body></html>")

        self.assertEqual(title, "")
        self.assertEqual(description, "")
        self.assertEqual(image_urls, [])
        self.assertEqual(video_urls, [])

    def test_open_graph_title_counts_as_metadata(self):
        html = '<meta property="og:title" content="Real post title">'

        title, _, _, _ = _extract_og_data(html)

        self.assertEqual(title, "Real post title")

    def test_reel_og_video_replaces_structured_cover_only_media(self):
        url = "https://www.facebook.com/reel/123/"
        video = "https://video.xx.fbcdn.net/reel.mp4"
        html = ('<script type="application/json">{"id":"123","image":{"uri":"https://scontent.xx.fbcdn.net/cover.jpg"}}</script>'
                f'<meta property="og:video" content="{video}">')
        post = _extract_page_data(html, url)
        self.assertEqual([(m.type, m.url) for m in post.ordered_media], [("video", video)])


class FacebookPreviewAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cookie_env = patch.dict("os.environ", {"FACEBOOK_COOKIE": ""})
        cookie_env.start()
        self.addCleanup(cookie_env.stop)

    async def test_cookie_adds_public_image_after_anonymous_failure(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        authenticated = FacebookPostData(url=url, description="public body", image_urls=["https://scontent.xx.fbcdn.net/image.jpg"], public=True)
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
            "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp", new=AsyncMock(side_effect=RuntimeError("age wall"))
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock(return_value=authenticated)) as cookie:
            result = await _fetch_og_data(url)
        self.assertEqual(result.description, "public body")
        self.assertEqual(result.image_urls, authenticated.image_urls)
        cookie.assert_awaited_once_with(url, "xs=synthetic-test")

    async def test_title_only_anonymous_page_still_attempts_cookie_for_image_post(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        image = "https://scontent.xx.fbcdn.net/image.jpg"
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
            "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp",
            new=AsyncMock(return_value=post_data("Facebook", "", [], [], url, 200))
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie",
                 new=AsyncMock(return_value=FacebookPostData(url=url, image_urls=[image], public=True))) as cookie:
            result = await _fetch_og_data(url)
        self.assertEqual(result.image_urls, [image])
        cookie.assert_awaited_once()

    async def test_resolved_share_target_is_kept_for_cookie_when_anonymous_has_no_metadata(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        share = "https://www.facebook.com/share/p/test/"
        target = "https://www.facebook.com/demo/posts/pfbidTEST"
        session = FakeSession([FakeResponse(share, "", status=302, headers={"Location": target}),
                               FakeResponse(target, "<html>no metadata</html>")])
        with patch("discord_bot.features.social_preview.facebook_preview._build_candidate_urls", side_effect=lambda u: [u]), patch(
            "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession", return_value=session
        ):
            with self.assertRaises(FacebookPreviewUnavailable) as error:
                await _fetch_html_with_aiohttp(share)
        self.assertEqual(error.exception.target_url, target)
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
            "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp", new=AsyncMock(side_effect=error.exception)
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock(return_value=FacebookPostData(url=target, description="public", public=True))) as cookie:
            await _fetch_og_data(share)
        cookie.assert_awaited_once_with(target, "xs=synthetic-test")

    async def test_successful_anonymous_video_or_image_uses_no_cookie(self):
        cases = (("https://www.facebook.com/reel/123/", ["https://video.xx.fbcdn.net/video.mp4"]),
                 ("https://www.facebook.com/demo/posts/pfbidTEST", []))
        for url, videos in cases:
            with self.subTest(url=url), patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
                "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp",
                new=AsyncMock(return_value=post_data("title", "body", ["https://scontent.xx.fbcdn.net/cover.jpg"], videos, url, 200))
            ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock()) as cookie:
                await _fetch_og_data(url)
            cookie.assert_not_awaited()

    async def test_cookie_failure_preserves_anonymous_video_cover_and_original_text(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        url = "https://www.facebook.com/reel/123/"
        original = post_data("original title", "original body", ["https://scontent.xx.fbcdn.net/cover.jpg"], [], url)
        for reason in ("cookie_login_required", "not_public", "public_unverified", "cookie_rate_limited"):
            with self.subTest(reason=reason), patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
                "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp", new=AsyncMock(return_value=original)
            ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock(return_value=FacebookPostData(url=url, reason=reason))) as cookie:
                self.assertEqual(await _fetch_og_data(url), original)
            cookie.assert_awaited_once()

    async def test_final_failure_reports_cookie_status_instead_of_old_anonymous_error(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        failed = FacebookPostData(url=url, reason="cookie_request_rejected", debug={"cookie_status": 400})
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
            "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp",
            new=AsyncMock(side_effect=FacebookPreviewUnavailable("anonymous login wall"))
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie",
                 new=AsyncMock(return_value=failed)):
            with self.assertRaises(FacebookPreviewUnavailable) as error:
                await _fetch_og_data(url)
        self.assertIn("cookie_request_rejected", str(error.exception))
        self.assertIn("400", str(error.exception))
        self.assertNotIn("xs=synthetic-test", str(error.exception))
        self.assertEqual(error.exception.target_url, url)
        self.assertEqual(error.exception.reason, "cookie_request_rejected")
        self.assertEqual(error.exception.status, 400)

    async def test_cookie_can_add_mp4_to_anonymous_cover_without_replacing_body(self):
        from discord_bot.features.social_preview.facebook_fetch import FacebookPostData
        url = "https://www.facebook.com/reel/123/"
        mp4 = "https://video.xx.fbcdn.net/video.mp4"
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": "xs=synthetic-test"}), patch(
            "discord_bot.features.social_preview.facebook_preview._fetch_html_with_aiohttp",
            new=AsyncMock(return_value=post_data("title", "body", ["https://scontent.xx.fbcdn.net/cover.jpg"], [], url, 200))
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock(return_value=FacebookPostData(url=url, video_urls=[mp4], public=True))):
            result = await _fetch_og_data(url)
        self.assertEqual(result.description, "body")
        self.assertEqual(result.video_urls, [mp4])

    async def test_invalid_video_sources_keep_cover_without_downloading_video(self):
        url = "https://www.facebook.com/reel/123/"
        cover = "https://scontent.xx.fbcdn.net/cover.jpg"
        for video in ("https://video.xx.fbcdn.net/video.mpd", "https://video.xx.fbcdn.net/video.m3u8", "blob:local-video"):
            with self.subTest(video=video), patch(
                "discord_bot.features.social_preview.facebook_preview._fetch_og_data", new=AsyncMock(return_value=post_data("title", "body", [cover], [video], url))
            ), patch("discord_bot.features.social_preview.facebook_preview._download_bytes", new=AsyncMock(return_value=b"image")) as download:
                post = await build_facebook_preview(url)
            self.assertIsNone(post.video_url)
            self.assertEqual(len(post.files), 1)
            download.assert_awaited_once()
            self.assertEqual(download.await_args.args[1], cover)

    async def test_anonymous_html_size_limit_stops_fetch(self):
        url = "https://www.facebook.com/reel/123/"
        session = FakeSession([FakeResponse(url, "x" * 21)])
        with patch("discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession", return_value=session), patch(
            "discord_bot.features.social_preview.facebook_preview.MAX_FACEBOOK_HTML_BYTES", 20
        ):
            with self.assertRaisesRegex(FacebookPreviewUnavailable, "大小上限"):
                await _fetch_html_with_aiohttp(url)

    async def test_target_reel_json_supplies_mp4_when_open_graph_only_has_cover(self):
        url = "https://www.facebook.com/reel/1602403008023321/"
        body = (Path(__file__).parent / "fixtures" / "facebook_reel_video.html").read_text(encoding="utf-8")
        session = FakeSession([FakeResponse(url, body)])
        with patch("discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession", return_value=session):
            post = await _fetch_html_with_aiohttp(url)
        description, images, videos = post.description, post.image_urls, post.video_urls
        self.assertEqual(videos, ["https://video.xx.fbcdn.net/target-hd.mp4?oh=fake-signature&oe=fake-expiry"])
        self.assertEqual(images, ["https://scontent.xx.fbcdn.net/cover.jpg"])
        self.assertEqual(description, "公開影片正文")
        self.assertEqual(session.requested_urls, [url])

    @patch("discord_bot.features.social_preview.facebook_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._fetch_og_data", new_callable=AsyncMock)
    async def test_v2_text_images_and_video_keep_commentary_and_spoiler(self, fetch, download, send, cleanup):
        url = "https://www.facebook.com/share/p/test/"
        images = [f"https://scontent.xx.fbcdn.net/{index}.jpg" for index in range(6)]
        video = "https://video.xx.fbcdn.net/first.mp4?signature=" + "a" * 1000
        for has_images, has_video in ((False, False), (True, False), (False, True), (True, True)):
            for spoiler in (False, True):
                with self.subTest(has_images=has_images, has_video=has_video, spoiler=spoiler):
                    send.reset_mock()
                    cleanup.reset_mock()
                    download.reset_mock()
                    fetch.return_value = post_data("貼文標題", "貼文正文", images if has_images else [],
                                          [video, "https://video.xx.fbcdn.net/second.mp4"] if has_video else [],
                                          "https://www.facebook.com/post/canonical")
                    download.return_value = b"image bytes"
                    send.return_value = types.SimpleNamespace(id=123)
                    message = types.SimpleNamespace(
                        author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
                        channel=types.SimpleNamespace(name="general"),
                        content="我的留言 " + (f"||{url}||" if spoiler else url),
                    )
                    self.assertTrue(await handle_facebook_in_message(message))
                    send.assert_awaited_once()
                    kwargs = send.await_args.kwargs
                    self.assertFalse(set(kwargs) & {"content", "embed"})
                    view = kwargs["view"]
                    self.assertEqual(view.children[0].content, "我的留言")
                    card = next(item for item in view.children if isinstance(item, discord.ui.Container))
                    self.assertFalse(card.spoiler)
                    self.assertIn("||貼文標題\n貼文正文||" if spoiler else "貼文正文", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
                    if spoiler:
                        self.assertNotIn("貼文標題", card.children[0].content)
                        self.assertIn("Facebook", card.children[0].content)
                    media = [item for child in view.walk_children() if isinstance(child, discord.ui.MediaGallery) for item in child.items]
                    expected = [f"attachment://{file.filename}" for file in kwargs.get("files", [])]
                    if has_video:
                        expected.append(video)
                    self.assertEqual([item.media.url for item in media], expected)
                    self.assertTrue(all(item.spoiler == spoiler for item in media))
                    self.assertEqual([call.args[1] for call in download.await_args_list], images[:4] if has_images and not has_video else [])
                    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
                    self.assertIsNone(buttons[0].url)
                    self.assertEqual(buttons[1].url, url)
                    self.assertEqual(len(buttons), 2)  # Long signed URL stays out of buttons.
                    cleanup.assert_awaited_once_with(message, platform="Facebook", url=url)

    @patch("discord_bot.features.social_preview.facebook_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._download_bytes", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._fetch_og_data", new_callable=AsyncMock)
    async def test_unsafe_or_rejected_images_do_not_become_external_gallery_fallback(self, fetch, download, send, cleanup):
        url = "https://www.facebook.com/post/123"
        safe = "https://scontent.xx.fbcdn.net/image.jpg"
        fetch.return_value = post_data("Title", "Text", ["https://127.0.0.1/private.jpg", safe], ["https://example.com/video.mp4"], url)
        download.return_value = None
        send.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"), content=url,
        )
        self.assertTrue(await handle_facebook_in_message(message))
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args[1], safe)
        self.assertEqual(set(send.await_args.kwargs), {"view"})
        self.assertFalse(any(isinstance(item, discord.ui.MediaGallery) for item in send.await_args.kwargs["view"].walk_children()))

    @patch("discord_bot.features.social_preview.facebook_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._fetch_og_data", new_callable=AsyncMock)
    async def test_long_body_is_preserved_and_every_page_deletes_the_group(self, fetch, send, cleanup):
        url = "https://www.facebook.com/post/123"
        body = "😀全文" * 2500
        fetch.return_value = post_data("標題", body, [], [], url)
        send.side_effect = [types.SimpleNamespace(id=index) for index in range(10)]
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"), content="留言 " + url,
        )
        self.assertTrue(await handle_facebook_in_message(message))
        views = [call.kwargs["view"] for call in send.await_args_list]
        self.assertGreater(len(views), 1)
        parts = []
        for view in views:
            card = next(item for item in view.children if isinstance(item, discord.ui.Container))
            parts.extend(item.content for item in card.children[1:] if isinstance(item, discord.ui.TextDisplay))
            self.assertLessEqual(sum(len(item.content.encode("utf-16-le")) // 2 for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)), 4000)
            self.assertEqual(view.related_message_ids, [other.message.id for other in views if other is not view])
        self.assertEqual("".join(parts), body)
        cleanup.assert_awaited_once()

    @patch("discord_bot.features.social_preview.facebook_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview.build_facebook_preview", new_callable=AsyncMock)
    async def test_failed_spoiler_preview_masks_failure_but_keeps_commentary_and_controls(self, build, send, cleanup):
        url = "https://www.facebook.com/post/123"
        build.side_effect = RuntimeError("login wall")
        send.return_value = types.SimpleNamespace(id=123)
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"), content=f"留言 ||{url}||",
        )
        with self.assertLogs("discord_digest_bot", level="ERROR"):
            self.assertTrue(await handle_facebook_in_message(message))
        view = send.await_args.kwargs["view"]
        self.assertEqual(view.children[0].content, "留言")
        card = next(item for item in view.children if isinstance(item, discord.ui.Container))
        self.assertFalse(card.spoiler)
        self.assertIn("||Facebook預覽失敗 爛meta||", [item.content for item in card.children if isinstance(item, discord.ui.TextDisplay)])
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in card.children))

    @patch("discord_bot.features.social_preview.facebook_preview.cleanup_source_message", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.sender.send_preview_as_author", new_callable=AsyncMock)
    @patch("discord_bot.features.social_preview.facebook_preview._fetch_og_data", new_callable=AsyncMock)
    async def test_partial_transmission_is_rolled_back_without_cleaning_source(self, fetch, send, cleanup):
        url = "https://www.facebook.com/post/123"
        fetch.return_value = post_data("標題", "文" * 6000, [], [], url)
        first = types.SimpleNamespace(id=123, delete=AsyncMock())
        send.side_effect = [first, RuntimeError("sending failed")]
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="demo", global_name="demo"),
            channel=types.SimpleNamespace(name="general"), content=url, reply=AsyncMock(),
        )
        with patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel", new_callable=AsyncMock) as notify:
            self.assertFalse(await handle_facebook_in_message(message))
        notify.assert_awaited_once()
        self.assertIn("send_failed", notify.await_args.kwargs["error"])
        self.assertEqual(message.reply.await_args.args[0], "Facebook預覽失敗 爛meta")
        first.delete.assert_awaited_once()
        cleanup.assert_not_awaited()
        self.assertEqual(send.await_count, 2)

    async def test_fetch_tries_mobile_candidate_after_www_access_wall(self):
        share_url = "https://www.facebook.com/share/p/14kUyp62jLg/"
        mobile_url = "https://m.facebook.com/share/p/14kUyp62jLg/"
        session = FakeSession(
            [
                FakeResponse(share_url, '<form id="login_form"></form>'),
                FakeResponse(mobile_url, '<meta property="og:title" content="Mobile post">'),
            ]
        )

        with patch(
            "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
            return_value=session,
        ):
            post = await _fetch_html_with_aiohttp(share_url)

        self.assertEqual(post.title, "Mobile post")
        self.assertEqual(post.url, mobile_url)
        self.assertEqual(post.status, 200)
        self.assertEqual(session.requested_urls, [share_url, mobile_url])

    async def test_fetch_follows_facebook_redirect_to_final_post(self):
        share_url = "https://www.facebook.com/share/p/abc"
        final_url = "https://www.facebook.com/example/posts/123"
        session = FakeSession(
            [
                FakeResponse(share_url, "", status=302, headers={"Location": final_url}),
                FakeResponse(final_url, '<meta property="og:title" content="Resolved post">'),
            ]
        )

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview._build_candidate_urls",
                return_value=[share_url],
            ),
            patch(
                "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            post = await _fetch_html_with_aiohttp(share_url)

        self.assertEqual(post.title, "Resolved post")
        self.assertEqual(post.url, final_url)
        self.assertEqual(post.status, 200)
        self.assertEqual(session.requested_urls, [share_url, final_url])

    async def test_fetch_retries_hosts_using_resolved_post_path(self):
        share_url = "https://www.facebook.com/share/p/abc"
        final_url = "https://www.facebook.com/example/posts/123"
        mobile_final_url = "https://m.facebook.com/example/posts/123"
        session = FakeSession(
            [
                FakeResponse(share_url, "", status=302, headers={"Location": final_url}),
                FakeResponse(final_url, "<html><body>no metadata</body></html>"),
                FakeResponse(mobile_final_url, '<meta property="og:title" content="Mobile post">'),
            ]
        )

        with patch(
            "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
            return_value=session,
        ):
            post = await _fetch_html_with_aiohttp(share_url)

        self.assertEqual(post.title, "Mobile post")
        self.assertEqual(post.url, mobile_final_url)
        self.assertEqual(post.status, 200)
        self.assertEqual(session.requested_urls, [share_url, final_url, mobile_final_url])

    async def test_fetch_rejects_redirect_to_external_domain(self):
        share_url = "https://lm.facebook.com/l.php?u=https%3A%2F%2Fexample.com"
        session = FakeSession(
            [FakeResponse(share_url, "", status=302, headers={"Location": "https://example.com/private"})]
        )

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview._build_candidate_urls",
                return_value=[share_url],
            ),
            patch(
                "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "非 Facebook 網域"):
                await _fetch_html_with_aiohttp(share_url)

        self.assertEqual(session.requested_urls, [share_url])

    async def test_fetch_rejects_login_wall(self):
        url = "https://www.facebook.com/share/p/private"
        login_url = "https://www.facebook.com/login/?next=%2Fshare%2Fp%2Fprivate"
        session = FakeSession(
            [
                FakeResponse(url, "", status=302, headers={"Location": login_url}),
                FakeResponse(login_url, '<form id="login_form"></form>'),
            ]
        )

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview._build_candidate_urls",
                return_value=[url],
            ),
            patch(
                "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "登入或存取限制"):
                await _fetch_html_with_aiohttp(url)

    async def test_fetch_tries_next_candidate_when_first_page_has_no_metadata(self):
        first_url = "https://www.facebook.com/post/123"
        second_url = "https://m.facebook.com/post/123"
        session = FakeSession(
            [
                FakeResponse(first_url, "<html><body>login wall</body></html>"),
                FakeResponse(second_url, '<meta property="og:title" content="Real post title">'),
            ]
        )

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview._build_candidate_urls",
                return_value=[first_url, second_url],
            ),
            patch(
                "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            post = await _fetch_html_with_aiohttp(first_url)

        self.assertEqual(post.title, "Real post title")
        self.assertEqual(post.url, second_url)
        self.assertEqual(post.status, 200)
        self.assertEqual(session.requested_urls, [first_url, second_url])

    async def test_handler_uses_threads_style_fallback_preview_instead_of_reply(self):
        url = "https://www.facebook.com/share/p/14kUyp62jLg/"
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(bot=False, nick="Jwander", global_name="Jwander"),
            channel=types.SimpleNamespace(name="general"),
            content=url,
            reply=AsyncMock(),
        )
        sent_message = types.SimpleNamespace(id=123)

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview.build_facebook_preview",
                new=AsyncMock(side_effect=FacebookPreviewUnavailable(
                    "Facebook 頁面要求登入或存取限制", reason="login_required", status=403)),
            ),
            patch(
                "discord_bot.features.social_preview.sender.send_preview_as_author",
                new=AsyncMock(return_value=sent_message),
            ) as send_preview,
            patch(
                "discord_bot.features.social_preview.facebook_preview.cleanup_source_message",
                new=AsyncMock(),
            ) as cleanup_source,
            patch("discord_bot.features.social_preview.failure_notifications.forward_notify_to_channel",
                  new_callable=AsyncMock) as notify,
        ):
            handled = await handle_facebook_in_message(message)

        self.assertTrue(handled)
        notify.assert_awaited_once()
        self.assertIn("login_required", notify.await_args.kwargs["error"])
        self.assertIn("http_status=403", notify.await_args.kwargs["record"]["summary"])
        message.reply.assert_not_awaited()
        send_preview.assert_awaited_once()
        sent_kwargs = send_preview.await_args.kwargs
        view = sent_kwargs["view"]
        self.assertFalse(set(sent_kwargs) & {"content", "embed", "files"})
        self.assertTrue(view.has_components_v2())
        self.assertIn("Facebook預覽失敗 爛meta", [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)])
        self.assertTrue(any(getattr(item, "url", None) == url for item in view.walk_children()))
        cleanup_source.assert_awaited_once_with(message, platform="Facebook", url=url)


if __name__ == "__main__":
    unittest.main()
