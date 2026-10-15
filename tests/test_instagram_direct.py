import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from discord_bot.features.social_preview import instagram_direct as direct
from discord_bot.features.social_preview import instagram_fetch
from discord_bot.features.social_preview.instagram_fetch import normalize_instagram_url

URL = "https://www.instagram.com/p/ABC123"
IMAGES = [f"https://scontent.cdninstagram.com/photo-{n}.jpg" for n in range(6)]
COOKIE = "sessionid=synthetic-ig; csrftoken=synthetic-csrf"


def target(**kwargs):
    return {"code": "ABC123", "user": {"username": "demo_user", "full_name": "Demo", "is_private": False},
            "caption": {"text": "target caption"}, "carousel_media": [
                {"image_versions2": {"candidates": [{"url": u, "width": 1080, "height": 1350}]}} for u in IMAGES], **kwargs}


def page(node):
    return '<script type="application/json">' + json.dumps(node) + '</script>'


def og_page(code="ABC123"):
    return f'<meta property="og:url" content="https://www.instagram.com/demo_user/p/{code}/"><meta property="og:image" content="{IMAGES[0]}"><meta property="og:description" content="public caption">'


def response(body="", status=200, **headers):
    return SimpleNamespace(status_code=status, headers=headers, iter_content=Mock(return_value=iter([body.encode()])), close=Mock())


class InstagramDirectTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"INSTAGRAM_COOKIE": "", "THREADS_COOKIE": "do-not-use", "FACEBOOK_COOKIE": "do-not-use",
                                     "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        network = patch("requests.Session", return_value=self.session)
        network.start()
        self.addCleanup(network.stop)

    def test_bound_carousel_preserves_order_and_ignores_other_posts(self):
        payload = {"recommendations": [target(code="OTHER")], "data": target(), "other": target(code="WRONG")}
        post = direct.parse_instagram_page(URL, page(payload))
        self.assertEqual([m.url for m in post.media], IMAGES)
        self.assertEqual(post.author_username, "demo_user")
        self.assertEqual(post.text, "target caption")

    def test_bound_open_graph_extracts_caption_and_author_with_both_parsers(self):
        for parser in (instagram_fetch.BeautifulSoup, None):
            for caption in ('"hello world"', 'hello without quotes'):
                with self.subTest(parser=parser, caption=caption), patch.object(instagram_fetch, "BeautifulSoup", parser):
                    html = f"""
                    <meta property="og:url" content="{URL}/">
                    <meta property="og:title" content="Demo User (@demo.user) on Instagram">
                    <meta property="og:description" content='12 likes, 1 comments - demo.user on Instagram: {caption}'>
                    <meta property="twitter:image" content="{IMAGES[0]}">
                    """
                    post = direct.parse_instagram_page(URL, html)
                    self.assertEqual(post.author_username, "demo.user")
                    self.assertEqual(post.text, caption.strip('"'))
                    self.assertEqual([m.url for m in post.media], IMAGES[:1])
                    self.assertIsNone(post.preview_error)

    def test_bound_open_graph_filters_profile_images(self):
        html = og_page() + '<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.2885-19/avatar.jpg">'
        post = direct.parse_instagram_page(URL, html)
        self.assertEqual([m.url for m in post.media], IMAGES[:1])

    def test_api_preserves_caption_and_selects_best_carousel_candidates(self):
        video = "https://scontent.cdninstagram.com/large.mp4"
        children = [
            {"image_versions2": {"candidates": [
                {"url": IMAGES[0], "width": 320, "height": 320},
                {"url": IMAGES[1], "width": 1080, "height": 1080},
            ]}},
            {"thumbnail_url": IMAGES[2], "video_versions": [
                {"url": "https://scontent.cdninstagram.com/small.mp4", "width": 320, "height": 320},
                {"url": video, "width": 720, "height": 1280},
            ]},
        ]
        for caption in ("api caption", {"text": "api caption"}):
            with self.subTest(caption=caption):
                post = direct.parse_instagram_api(URL, json.dumps({"items": [target(caption=caption, carousel_media=children)]}))
                self.assertEqual(post.author_username, "demo_user")
                self.assertEqual(post.author_name, "Demo")
                self.assertEqual(post.text, "api caption")
                self.assertEqual([(m.type, m.url) for m in post.media], [("image", IMAGES[1]), ("video", video)])

    def test_real_caption_precedes_accessibility_description(self):
        caption = "台北RH🫵😍🫵\n\nTシャツ\nThe Rolling Stones\n\n📷\n@nishimaki_tkmi"
        for fields in ({"caption": {"text": caption}},
                       {"caption": None, "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]}}):
            node = target(**fields, accessibility_caption="Photo by 優里. 可能是文字的圖像.")
            for post in (direct.parse_instagram_page(URL, page(node)), direct.parse_instagram_api(URL, json.dumps({"items": [node]}))):
                self.assertEqual(post.text, caption)
                self.assertEqual(len(post.media), 6)

    def test_accessibility_description_alone_is_not_post_body(self):
        post = direct.parse_instagram_page(URL, page(target(caption=None, accessibility_caption="Photo by Demo. May be an image of text.")))
        self.assertIsNone(post.text)
        self.assertEqual(len(post.media), 6)

    def test_http_400_checkpoint_is_classified_without_private_response_logging(self):
        self.session.get.return_value = response(json.dumps({"status": "fail", "message": "checkpoint_required", "checkpoint_url": "https://www.instagram.com/challenge/secret"}), status=400)
        with self.assertLogs(direct.logger, level="INFO") as logs:
            post = direct._request_post(URL, COOKIE, persist_cookies=False)
        self.assertEqual(post.preview_error, "checkpoint_required")
        self.assertIn("checkpoint_required", " ".join(logs.output))
        self.assertNotIn("secret", " ".join(logs.output))

    def test_public_reel_uses_video_fallback_after_checkpoint_without_cookie_leak(self):
        url = "https://www.instagram.com/reel/ABC123"
        video = "https://scontent.cdninstagram.com/fallback.mp4?signature=synthetic"
        self.session.get.side_effect = [response(og_page()), response(json.dumps({"message": "checkpoint_required", "status": "fail"}), status=400)]
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), patch.object(direct, "resolve_instagram_video_fallback", return_value=video, create=True) as fallback:
            post = direct.fetch_instagram_direct(url, persist_cookies=False)
        self.assertEqual([(m.type, m.url) for m in post.media], [("video", video)])
        self.assertEqual(post.text, "public caption")
        fallback.assert_called_once_with(url, trace_id=post.trace_id)

    def test_cookie_video_is_preferred_over_third_party_fallback(self):
        url = "https://www.instagram.com/reel/ABC123"
        video = "https://scontent.cdninstagram.com/official.mp4"
        node = target(carousel_media=None, video_versions=[{"url": video}])
        self.session.get.side_effect = [response(og_page()), response(json.dumps({"items": [node]}))]
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), patch.object(direct, "resolve_instagram_video_fallback") as fallback:
            post = direct.fetch_instagram_direct(url, persist_cookies=False)
        self.assertEqual([(m.type, m.url) for m in post.media], [("video", video)])
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["Cookie"], COOKIE)
        fallback.assert_not_called()

    def test_public_reel_video_skips_cookie_and_third_party(self):
        video = "https://scontent.cdninstagram.com/official.mp4"
        self.session.get.return_value = response(og_page() + f'<meta property="og:video" content="{video}">')
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), patch.object(direct, "resolve_instagram_video_fallback") as fallback:
            post = direct.fetch_instagram_direct("https://www.instagram.com/reel/ABC123")
        self.assertEqual([(m.type, m.url) for m in post.media], [("video", video)])
        self.session.get.assert_called_once()
        self.assertNotIn("Cookie", self.session.get.call_args.kwargs["headers"])
        fallback.assert_not_called()

    def test_public_fallback_failure_keeps_cover_and_caption(self):
        self.session.get.return_value = response(og_page())
        with patch.object(direct, "resolve_instagram_video_fallback", return_value=None) as fallback:
            post = direct.fetch_instagram_direct("https://www.instagram.com/reel/ABC123")
        self.assertEqual([m.url for m in post.media], IMAGES[:1])
        self.assertEqual(post.text, "public caption")
        self.assertIsNone(post.preview_error)
        fallback.assert_called_once()

    def test_private_rate_limited_not_found_and_unbound_posts_skip_third_party(self):
        url = "https://www.instagram.com/reel/ABC123"
        for authenticated in (response(status=404), response(status=429),
                              response(json.dumps({"items": [target(user={"is_private": True})]}))):
            self.session.get.side_effect = [response(og_page()), authenticated]
            with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), patch.object(direct, "resolve_instagram_video_fallback") as fallback:
                direct.fetch_instagram_direct(url, persist_cookies=False)
                fallback.assert_not_called()
        self.session.get.side_effect = None
        for body, link in ((og_page(), URL), (og_page("WRONG"), url),
                           (page(target(carousel_media=None, video_url="https://scontent.cdninstagram.com/official.mp4")), url)):
            self.session.get.return_value = response(body)
            with patch.object(direct, "resolve_instagram_video_fallback") as fallback:
                direct.fetch_instagram_direct(link)
                fallback.assert_not_called()

    def test_http_400_error_body_is_bounded_and_failed_cookies_are_not_saved(self):
        consumed = []

        def chunks():
            for chunk in (b"x" * 20, b"x", b"must-not-read"):
                consumed.append(chunk)
                yield chunk

        reply = response(status=400, **{"Set-Cookie": "sessionid=poison; Path=/"})
        reply.iter_content.return_value = chunks()
        self.session.get.return_value = reply
        with patch.object(direct, "MAX_ERROR_BYTES", 20), patch.object(direct, "save_response_cookies") as save:
            post = direct._request_post(URL, COOKIE)
        self.assertEqual(post.preview_error, "request_rejected")
        self.assertEqual(consumed, [b"x" * 20, b"x"])
        save.assert_not_called()
        reply.close.assert_called_once()

    def test_checkpoint_to_kk_logs_correlated_stages_without_sensitive_data(self):
        url = "https://www.instagram.com/reel/ABC123"
        video = "https://scontent.cdninstagram.com/video.mp4?sig=synthetic-private-signature"
        self.session.get.side_effect = [response(og_page()),
            response(json.dumps({"message": "checkpoint_required", "checkpoint_url": "sensitive-checkpoint-body"}), status=400),
            response(status=302, Location=video)]
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), self.assertLogs(direct.logger, level="DEBUG") as logs:
            post = direct.fetch_instagram_direct(url, persist_cookies=False)
        events = [json.loads(r.getMessage().split(" ", 1)[1]) for r in logs.records if r.getMessage().startswith("instagram_diagnostic ")]
        stages = [e for e in events if e["event"] == "fetch_stage"]
        self.assertEqual([e["stage"] for e in stages], ["anonymous", "cookie", "kkinstagram"])
        self.assertEqual([e["http_status"] for e in stages], [200, 400, 302])
        self.assertEqual([e["result"] for e in stages], ["success", "checkpoint_required", "success"])
        self.assertEqual({e["trace_id"] for e in events}, {post.trace_id})
        self.assertEqual({e["shortcode"] for e in events}, {"ABC123"})
        self.assertTrue(all(e["elapsed_ms"] >= 0 for e in stages))
        complete, = [e for e in events if e["event"] == "fetch_complete"]
        self.assertEqual((complete["source"], complete["cookie_result"], complete["kk_result"], complete["outcome"]),
                         ("kkinstagram", "checkpoint_required", "success", "video"))
        self.assertEqual(complete["video_count"], 1)
        self.assertEqual(post.fetch_source, "kkinstagram")
        for value in (COOKIE, video, url, "sensitive-checkpoint-body", "synthetic-private-signature", "public caption"):
            self.assertNotIn(value, " ".join(logs.output))

    def test_fetch_summary_distinguishes_cookie_success_cover_fallback_and_blocked_attempts(self):
        url = "https://www.instagram.com/reel/ABC123"
        video = "https://scontent.cdninstagram.com/official.mp4"
        scenarios = [
            ([response(page(target(carousel_media=None, video_url=video)))], "anonymous", "not_attempted", "not_attempted", "video"),
            ([response(og_page()), response(json.dumps({"items": [target(carousel_media=None, video_url=video)]}))], "cookie", "success", "not_attempted", "video"),
            ([response(og_page()), response(status=400), response(status=500)], "anonymous", "request_rejected", "unavailable", "images_only"),
            ([response(og_page()), response(status=429)], "anonymous", "rate_limited", "not_attempted", "images_only"),
            ([response(status=404)], "anonymous", "not_attempted", "not_attempted", "error"),
            ([response(og_page()), response(json.dumps({"items": [target(user={"is_private": True})]}))], "cookie", "private_or_unverified", "not_attempted", "error"),
        ]
        trace_ids = set()
        for replies, source, cookie_result, kk_result, outcome in scenarios:
            with self.subTest(source=source, cookie_result=cookie_result, kk_result=kk_result):
                self.session.get.reset_mock()
                self.session.get.side_effect = replies
                with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}), self.assertLogs(direct.logger, level="DEBUG") as logs:
                    post = direct.fetch_instagram_direct(url, persist_cookies=False)
                complete, = [json.loads(r.getMessage().split(" ", 1)[1]) for r in logs.records
                             if '"event":"fetch_complete"' in r.getMessage()]
                self.assertEqual((complete["source"], complete["cookie_result"], complete["kk_result"], complete["outcome"]),
                                 (source, cookie_result, kk_result, outcome))
                self.assertEqual(self.session.get.call_count, len(replies))
                self.assertEqual(post.trace_id, complete["trace_id"])
                self.assertNotIn(post.trace_id, trace_ids)
                trace_ids.add(post.trace_id)

    def test_timeout_has_safe_diagnostic_reason(self):
        import requests
        self.session.get.side_effect = requests.Timeout(COOKIE)
        with self.assertLogs(direct.logger, level="INFO") as logs:
            post = direct._request_post(URL, COOKIE, persist_cookies=False)
        self.assertEqual(post.preview_error, "fetch_failed")
        self.assertIn('"reason":"timeout"', " ".join(logs.output))
        self.assertNotIn(COOKIE, " ".join(logs.output))

    def test_graph_sidecar_chooses_best_image_and_skips_video_poster(self):
        node = target(carousel_media=None, edge_sidecar_to_children={"edges": [
            {"node": {"display_resources": [
                {"src": IMAGES[2], "config_width": 320, "config_height": 320},
                {"src": IMAGES[0], "config_width": 1080, "config_height": 1350},
            ]}},
            {"node": {"video_url": "https://scontent.cdninstagram.com/reel.mp4", "display_url": IMAGES[1]}},
        ]})
        post = direct.parse_instagram_page(URL, page(node))
        self.assertEqual([(m.type, m.url) for m in post.media], [("image", IMAGES[0]), ("video", "https://scontent.cdninstagram.com/reel.mp4")])

    def test_anonymous_and_cookie_mixed_carousel_preserve_source_order(self):
        video = "https://scontent.cdninstagram.com/third.mp4?signature=synthetic"
        node = target()
        node["carousel_media"].insert(2, {"video_versions": [{"url": video}], "display_url": IMAGES[5]})
        expected = [("image", IMAGES[0]), ("image", IMAGES[1]), ("video", video),
                    *[("image", image) for image in IMAGES[2:]]]
        for post in (direct.parse_instagram_page(URL, page(node)),
                     direct.parse_instagram_api(URL, json.dumps({"items": [node]}))):
            self.assertEqual([(m.type, m.url) for m in post.media], expected)

    def test_og_video_does_not_treat_cover_as_first_carousel_image(self):
        video = "https://scontent.cdninstagram.com/reel.mp4"
        post = direct.parse_instagram_page(URL, og_page() + f'<meta property="og:video" content="{video}">')
        self.assertEqual([(m.type, m.url) for m in post.media], [("video", video)])

    def test_private_or_unknown_authenticated_owner_is_rejected(self):
        for owner in ({"is_private": True}, {}, {"is_private": "false"}):
            post = direct.parse_instagram_api(URL, json.dumps({"items": [target(user=owner)]}))
            self.assertEqual(post.preview_error, "private_or_unverified")
            self.assertEqual(post.media, [])
        self.assertEqual(direct.parse_instagram_page(URL, page(target(user={"is_private": True}))).preview_error, "private_or_unverified")

    def test_wrong_post_and_untrusted_media_cannot_produce_a_preview(self):
        self.assertTrue(direct.parse_instagram_api(URL, json.dumps({"items": [target(code="OTHER")]})).preview_error)
        node = target(caption=None, carousel_media=[{"display_url": "https://example.com/pic.jpg"},
                                                  {"display_url": "https://scontent.cdninstagram.com/t51.2885-19/avatar.jpg"}])
        self.assertTrue(direct.parse_instagram_page(URL, page(node)).preview_error)
        self.assertTrue(direct.parse_instagram_page(URL, og_page("OTHER")).preview_error)

    def test_public_og_is_bound_to_username_permalink(self):
        post = direct.parse_instagram_page(URL, og_page())
        self.assertEqual([m.url for m in post.media], IMAGES[:1])
        self.assertEqual(post.author_username, "demo_user")
        self.assertEqual(post.preview_level, "metadata")

    def test_anonymous_complete_carousel_never_sends_cookie(self):
        self.session.get.return_value = response(page(target()))
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}):
            post = direct.fetch_instagram_direct(URL)
        self.assertEqual(len(post.media), 6)
        self.session.get.assert_called_once()
        self.assertNotIn("Cookie", self.session.get.call_args.kwargs["headers"])
        self.assertFalse(self.session.trust_env)

    def test_complete_mixed_post_emits_no_info_diagnostics(self):
        node = target()
        node["carousel_media"].append({"display_url": IMAGES[0]})
        node["carousel_media"].append({"display_url": "https://scontent.cdninstagram.com/seventh.jpg"})
        node["carousel_media"].append({"video_url": "https://scontent.cdninstagram.com/last.mp4"})
        self.session.get.return_value = response(page(node))
        with self.assertNoLogs(direct.logger, level="INFO"):
            post = direct.fetch_instagram_direct(URL)
        self.assertEqual((sum(m.type == "image" for m in post.media), sum(m.type == "video" for m in post.media)), (7, 1))
        self.session.get.assert_called_once()

    def test_cookie_get_is_once_same_host_and_returns_full_carousel(self):
        self.session.get.side_effect = [response(og_page()), response(json.dumps({"items": [target()]}))]
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}):
            post = direct.fetch_instagram_direct(URL)
        self.assertEqual(len(post.media), 6)
        self.assertEqual(self.session.get.call_count, 2)
        call = self.session.get.call_args
        self.assertTrue(call.args[0].startswith("https://www.instagram.com/api/v1/media/"))
        self.assertEqual(call.kwargs["headers"]["Cookie"], COOKIE)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])

    def test_only_instagram_seed_is_used(self):
        self.session.get.return_value = response(og_page())
        direct.fetch_instagram_direct(URL)
        self.session.get.assert_called_once()
        self.assertNotIn("Cookie", self.session.get.call_args.kwargs["headers"])

    def test_cookie_failure_keeps_available_public_metadata(self):
        for status in (302, 403, 429, 500):
            with self.subTest(status=status), patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}):
                self.session.get.side_effect = [response(og_page()), response(status=status, Location="https://evil.example/")]
                post = direct.fetch_instagram_direct(URL)
                self.assertEqual([m.url for m in post.media], IMAGES[:1])
                self.assertIsNone(post.preview_error)

    def test_rate_limit_and_not_found_do_not_retry(self):
        for status, error in ((429, "rate_limited"), (404, "not_found")):
            with self.subTest(status=status), patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}):
                self.session.get.reset_mock()
                self.session.get.return_value = response(status=status)
                self.assertEqual(direct.fetch_instagram_direct(URL).preview_error, error)
                self.session.get.assert_called_once()

    def test_cookie_redirect_and_challenge_fail_without_following(self):
        for reply, expected in ((response(status=302, Location="https://evil.example/"), "login_required"),
                                (response(json.dumps({"status": "fail", "message": "challenge_required"})), "checkpoint_required")):
            self.session.get.side_effect = [response("empty"), reply]
            with patch.dict(os.environ, {"INSTAGRAM_COOKIE": COOKIE}):
                self.assertEqual(direct.fetch_instagram_direct(URL).preview_error, expected)

    def test_invalid_cookie_is_not_sent_and_limits_stop_large_responses(self):
        for value in ("bad\nheader=yes", "not-a-cookie", "a=" + "x" * 16384):
            self.session.get.reset_mock()
            self.assertEqual(direct._request_post(URL, value).preview_error, "cookie_config_invalid")
            self.session.get.assert_not_called()
        reply = response("x" * 21)
        self.session.get.return_value = reply
        with patch.object(direct, "MAX_HTML_BYTES", 20):
            self.assertEqual(direct.fetch_instagram_direct(URL).preview_error, "fetch_failed")
        reply.close.assert_called_once()

    def test_rotation_persists_only_on_success_and_is_never_logged(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "INSTAGRAM_COOKIE": COOKIE, "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "SOCIAL_PREVIEW_COOKIE_STORE_DIR": directory,
        }):
            self.session.get.side_effect = [response("empty"), response(json.dumps({"items": [target()]}), **{"Set-Cookie": "sessionid=rotated-ig; Domain=.instagram.com; Path=/"}),
                                            response("empty"), response(status=403, **{"Set-Cookie": "sessionid=poison; Path=/"})]
            with self.assertLogs(direct.logger, level="INFO") as logs:
                direct.fetch_instagram_direct(URL)
                direct.fetch_instagram_direct(URL)
            self.assertIn("sessionid=rotated-ig", self.session.get.call_args.kwargs["headers"]["Cookie"])
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["instagram.cookies"])
            self.assertNotIn("rotated-ig", " ".join(logs.output))
            self.assertNotIn(COOKIE, " ".join(logs.output))

    def test_transport_exception_does_not_leak_credentials(self):
        self.session.get.side_effect = RuntimeError(COOKIE)
        with self.assertLogs(direct.logger, level="INFO") as logs:
            self.assertEqual(direct._request_post(URL, COOKIE).preview_error, "fetch_failed")
        self.assertNotIn(COOKIE, " ".join(logs.output))

    def test_url_canonicalization_rejects_foreign_host_and_extra_paths(self):
        self.assertEqual(normalize_instagram_url("http://instagr.am/p/ABC123/?mdxt=tracking"), URL)
        for value in ("https://www.instagram.com.evil.test/p/ABC123", "https://user@www.instagram.com/p/ABC123", URL + "/extra"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_instagram_url(value)
