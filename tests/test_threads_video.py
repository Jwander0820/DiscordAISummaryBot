import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from discord_bot.features.social_preview import threads_fetch as tf


POST_URL = "https://www.threads.com/@demo/post/Video001"
FIRST_VIDEO = "https://scontent.cdninstagram.com/first.mp4?oh=signature&oe=expiry"
EMBED_HTML = (Path(__file__).parent / "fixtures" / "threads_embed_video.html").read_text(encoding="utf-8")
LINKED_IG_HTML = (Path(__file__).parent / "fixtures" / "threads_linked_instagram_video.html").read_text(encoding="utf-8")


def structured_html(payload):
    return (
        '<html><meta property="og:image" content="https://scontent.cdninstagram.com/poster.jpg">'
        '<script type="application/json" data-sjs>' + json.dumps(payload) + '</script></html>'
    )


class ThreadsVideoFetchTests(unittest.TestCase):
    def setUp(self):
        cookie_env = patch.dict("os.environ", {"THREADS_COOKIE": ""})
        cookie_env.start()
        self.addCleanup(cookie_env.stop)

    def test_linked_inline_instagram_video_belongs_to_target_threads_post(self):
        post = tf._parse_html(POST_URL, LINKED_IG_HTML)
        self.assertEqual(post.text, "Threads 轉發的正文")
        self.assertIsNotNone(tf.first_direct_video(post.media))
        self.assertEqual(tf.first_direct_video(post.media).url,
                         "https://instagram.example.fna.fbcdn.net/linked-reel.mp4?oh=test-signature&oe=test-expiry")

    def test_linked_inline_video_does_not_cross_post_or_quote_boundaries(self):
        inline = {"code": "IG001", "video_versions": [{"url": FIRST_VIDEO}]}
        for payload in (
            {"code": "Other001", "text_post_app_info": {"linked_inline_media": inline}},
            {"code": "Video001", "__typename": "ThreadsPost", "quoted_post": {"text_post_app_info": {"linked_inline_media": inline}}},
            {"code": "Video001", "__typename": "ThreadsPost", "linked_inline_media": inline},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(tf.first_direct_video(tf._parse_html(POST_URL, structured_html(payload)).media))

    def test_native_video_precedes_linked_inline_video(self):
        payload = {"code": "Video001", "video_versions": [{"url": FIRST_VIDEO}],
                   "text_post_app_info": {"linked_inline_media": {"code": "IG001", "video_url": FIRST_VIDEO + "&linked=1"}}}
        self.assertEqual(tf.first_direct_video(tf._parse_html(POST_URL, structured_html(payload)).media).url, FIRST_VIDEO)

    def test_structured_video_is_found_alongside_poster(self):
        page = structured_html({"data": {"post": {"code": "Video001", "video_versions": [
            {"url": FIRST_VIDEO, "width": 720, "height": 1280},
        ]}}})
        post = tf._parse_html(POST_URL, page)
        self.assertEqual([m.url for m in post.media if m.type == "video"], [FIRST_VIDEO])

    def test_carousel_order_is_not_rendition_order_or_recommendation_order(self):
        second = "https://scontent.cdninstagram.com/second.mp4"
        page = structured_html({"items": [
            {"code": "Other001", "video_versions": [{"url": second}]},
            {"code": "Video001", "video_versions": [{"url": second}], "carousel_media": [
                {"image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/photo.jpg"}]}},
                {"code": "Child001", "video_versions": [
                    {"url": "https://scontent.cdninstagram.com/low.mp4", "width": 320, "height": 480},
                    {"url": FIRST_VIDEO, "width": 720, "height": 1280},
                ]},
                {"video_versions": [{"url": second, "width": 1920, "height": 1080}]},
            ], "quoted_post": {"code": "Quote001", "video_url": second}},
        ]})
        post = tf._parse_html(POST_URL, page)
        self.assertEqual([m.url for m in post.media if m.type == "video"], [FIRST_VIDEO, second])

    def test_unowned_reply_and_foreign_videos_are_ignored(self):
        page = structured_html({"items": [
            {"video_url": FIRST_VIDEO},
            {"code": "Other001", "video_url": FIRST_VIDEO},
            {"code": "Video001", "reply_to": {"video_url": FIRST_VIDEO},
             "quoted_post": {"video_url": FIRST_VIDEO}, "user": {"video_url": FIRST_VIDEO}},
        ]})
        self.assertFalse(any(m.type == "video" for m in tf._parse_html(POST_URL, page).media))

    def test_permalink_identity_and_escaped_signed_url(self):
        escaped = FIRST_VIDEO.replace("/", r"\/").replace("&", r"\u0026")
        for key in ("playable_url", "video_url"):
            with self.subTest(key=key):
                page = structured_html({"post": {"permalink": POST_URL, key: escaped}})
                videos = [m.url for m in tf._parse_html(POST_URL, page).media if m.type == "video"]
                self.assertEqual(videos, [FIRST_VIDEO])

    def test_malformed_scripts_and_unsafe_candidates_do_not_mask_safe_video(self):
        page = '<script type="application/json">{broken</script>' + structured_html({
            "code": "Video001", "video_versions": [
                {"url": "https://attacker.example/video.mp4", "width": 9000, "height": 9000},
                {"url": "blob:https://www.threads.com/local", "width": 9000, "height": 9000},
                {"url": "https://scontent.cdninstagram.com/stream.m3u8", "width": 9000, "height": 9000},
                {"url": FIRST_VIDEO, "width": 720, "height": 1280},
            ],
        })
        self.assertEqual([m.url for m in tf._parse_html(POST_URL, page).media if m.type == "video"], [FIRST_VIDEO])

    def test_embed_fallback_fills_video_and_caption_with_one_anonymous_request(self):
        post = tf.ThreadsPost(url=POST_URL, media=[tf.ThreadsMedia("image", "https://scontent.cdninstagram.com/poster.jpg")])
        response = Mock(status_code=200, url=POST_URL + "/embed", text=EMBED_HTML)
        response.headers = {"Content-Type": "text/html"}
        response.iter_content.return_value = [EMBED_HTML.encode("utf-8")]
        get = Mock(return_value=response)
        with patch.object(tf, "_try_requests", return_value=post), patch.object(tf, "_try_oembed_fill") as oembed, patch.dict(
            "sys.modules", {"requests": SimpleNamespace(get=get)}
        ):
            result = tf.fetch_threads_post(POST_URL)
        self.assertEqual([m.url for m in result.media if m.type == "video"], [FIRST_VIDEO])
        self.assertEqual([m.type for m in result.media], ["video"])
        self.assertEqual(result.text, "影片貼文\n第二行")
        self.assertEqual(result.url, POST_URL)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], POST_URL + "/embed")
        self.assertIn("Discordbot", get.call_args.kwargs["headers"]["User-Agent"])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        oembed.assert_not_called()
        response.close.assert_called_once()

    def test_existing_video_does_not_make_an_extra_embed_request(self):
        post = tf.ThreadsPost(url=POST_URL, text="正文", media=[tf.ThreadsMedia("video", FIRST_VIDEO)])
        get = Mock(side_effect=AssertionError("extra request"))
        with patch.object(tf, "_try_requests", return_value=post), patch.dict("sys.modules", {"requests": SimpleNamespace(get=get)}):
            self.assertIs(tf.fetch_threads_post(POST_URL), post)
        get.assert_not_called()

    def test_complete_image_carousel_does_not_replace_order_with_embed_video(self):
        post = tf.ThreadsPost(url=POST_URL, text="正文", media=[tf.ThreadsMedia("image", "https://scontent.cdninstagram.com/photo.jpg")],
                              debug={"media_source": "structured_json"})
        with patch.object(tf, "_try_requests", return_value=post), patch.object(tf, "_try_embed_fill") as embed:
            self.assertIs(tf.fetch_threads_post(POST_URL), post)
        embed.assert_not_called()

    def test_og_video_fallback_drops_cover_image(self):
        body = f'<meta property="og:image" content="https://scontent.cdninstagram.com/cover.jpg"><meta property="og:video" content="{FIRST_VIDEO}">'
        post = tf._parse_html(POST_URL, body)
        self.assertEqual([(m.type, m.url) for m in post.media], [("video", FIRST_VIDEO)])

    def test_dom_mixed_order_keeps_one_rendition_per_video(self):
        image1, image2 = "https://scontent.cdninstagram.com/one.jpg", "https://scontent.cdninstagram.com/two.jpg"
        media = (f'<img src="{image1}"><video><source src="{FIRST_VIDEO}">'
                 '<source src="https://scontent.cdninstagram.com/alternate.mp4"></video>'
                 f'<img src="{image2}">')
        wrappers = [f'<a href="{POST_URL}/media">{media}</a>',
                    f'<div class="Embed"><div class="LinkContainer"><a href="{POST_URL}"></a></div><div class="SoloMediaContainer">{media}</div></div>']
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper):
                post = tf._parse_html(POST_URL, wrapper)
                self.assertEqual([(m.type, m.url) for m in post.media], [("image", image1), ("video", FIRST_VIDEO), ("image", image2)])

    def test_embed_fallback_failure_or_wrong_post_preserves_original(self):
        for status, page in ((302, EMBED_HTML), (403, EMBED_HTML), (200, EMBED_HTML.replace("Video001", "Other001"))):
            with self.subTest(status=status):
                post = tf.ThreadsPost(url=POST_URL, text="原文", media=[tf.ThreadsMedia("image", "https://scontent.cdninstagram.com/photo.jpg")])
                response = Mock(status_code=status, url=POST_URL + "/embed", text=page)
                response.headers = {"Content-Type": "text/html"}
                response.iter_content.return_value = [page.encode("utf-8")]
                with patch.object(tf, "_try_requests", return_value=post), patch.dict("sys.modules", {"requests": SimpleNamespace(get=Mock(return_value=response))}):
                    result = tf.fetch_threads_post(POST_URL)
                self.assertEqual(result.text, "原文")
                self.assertEqual([m.type for m in result.media], ["image"])

    def test_embed_html_limit_closes_response_without_reading_more_chunks(self):
        post = tf.ThreadsPost(url=POST_URL, text="原文")
        response = Mock(status_code=200)
        def chunks(_size):
            yield b"x" * (tf.MAX_EMBED_HTML_BYTES + 1)
            raise AssertionError("must stop reading oversized HTML")
        response.iter_content.side_effect = chunks
        with patch.dict("sys.modules", {"requests": SimpleNamespace(get=Mock(return_value=response))}):
            tf._try_embed_fill(post)
        self.assertEqual(post.debug["embed_error"], "html_too_large")
        self.assertEqual(post.media, [])
        response.close.assert_called_once()

    def test_first_direct_video_ignores_manifests_and_keeps_escaped_path(self):
        video = "https://scontent.cdninstagram.com/first%2Emp4?oh=signed&oe=expiry"
        items = [tf.ThreadsMedia("video", "https://scontent.cdninstagram.com/list.m3u8"),
                 tf.ThreadsMedia("video", video), tf.ThreadsMedia("video", FIRST_VIDEO)]
        self.assertEqual(tf.first_direct_video(items).url, video)


if __name__ == "__main__":
    unittest.main()
