import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from discord_bot.features.social_preview import threads_fetch as tf


URL = "https://www.threads.com/@demo/post/Cookie001"
COOKIE = "sessionid=synthetic-test-only; csrftoken=fake-test-token"
VIDEO = "https://scontent.cdninstagram.com/post.mp4?oh=signature&oe=expiry"
IMAGE = "https://scontent.cdninstagram.com/post.jpg"
EMBED_FILL = tf._try_embed_fill
OEMBED_FILL = tf._try_oembed_fill
AUDIENCE_HTML = (Path(__file__).parent / "fixtures" / "threads_audience_restriction.html").read_text(encoding="utf-8")


def page(payload, kind="application/json"):
    return '<html><script type="' + kind + '">' + json.dumps(payload) + '</script></html>'


def response(body, status=200, **headers):
    return SimpleNamespace(status_code=status, headers=headers,
                           iter_content=Mock(return_value=iter([body.encode()])), close=Mock())


def target(**kwargs):
    return {"code": "Cookie001", "user": {"username": "demo", "full_name": "Demo"},
            "caption": {"text": "目標正文"}, **kwargs}


class ThreadsCookieTests(unittest.TestCase):
    def setUp(self):
        # Never consume a developer's real environment cookie during tests.
        self.env = patch.dict(os.environ, {"THREADS_COOKIE": COOKIE, "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.session.get.return_value = response(page({"data": target()}))
        self.requests = SimpleNamespace(Session=Mock(return_value=self.session), get=Mock())
        self.modules = patch.dict("sys.modules", {"requests": self.requests})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        for name in ("_try_embed_fill", "_try_oembed_fill"):
            p = patch.object(tf, name)
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, anonymous=None, url=URL):
        with patch.object(tf, "_try_requests", return_value=anonymous or tf.ThreadsPost(url=url)):
            return tf.fetch_threads_post(url)

    def test_successful_response_updates_next_cookie_request_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "SOCIAL_PREVIEW_COOKIE_STORE_DIR": directory
        }):
            self.session.get.return_value = response(page({"data": target()}), **{"Set-Cookie": "sessionid=rotated-test; Path=/; Secure"})
            self.fetch()
            self.session.get.return_value = response(page({"data": target()}))
            self.fetch()
            headers = self.session.get.call_args.kwargs["headers"]
            self.assertIn("sessionid=rotated-test", headers["Cookie"])
            self.assertNotIn("synthetic-test-only", headers["Cookie"])
            self.assertEqual(self.session.get.call_count, 2)

    def test_failed_cookie_response_never_saves_server_updates(self):
        with patch("discord_bot.features.social_preview.threads_cookie.save_response_cookies") as save:
            for body, status in (("login", 403), (page({"data": {"code": "Other"}}), 200)):
                self.session.get.return_value = response(body, status, **{"Set-Cookie": "sessionid=bad; Path=/"})
                self.fetch()
            save.assert_not_called()

    def test_deleted_cached_cookie_does_not_send_original_seed_again(self):
        from discord_bot.features.social_preview.cookie_store import prepare_cookie, save_response_cookies
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "SOCIAL_PREVIEW_COOKIE_STORE_DIR": directory
        }):
            snapshot = prepare_cookie("threads", URL, COOKIE)
            save_response_cookies(snapshot, ["sessionid=; Path=/; Max-Age=0", "csrftoken=; Path=/; Max-Age=0"])
            post = self.fetch()
            self.assertEqual(post.preview_error, "cookie_login_required")
            self.session.get.assert_not_called()

    def test_empty_anonymous_result_uses_cookie_once_and_recovers_target(self):
        post = self.fetch(tf.ThreadsPost(url=URL, preview_error="invalid_post"))
        self.assertEqual(post.text, "目標正文")
        self.assertIsNone(post.preview_error)
        self.session.get.assert_called_once()
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, (URL + "?hl=en",))
        self.assertEqual(kwargs["headers"]["Cookie"], COOKIE)
        self.assertEqual(kwargs["headers"]["User-Agent"], tf.UA_MOBILE)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertFalse(self.session.trust_env)
        self.session.get.return_value.close.assert_called_once()
        self.assertNotIn(COOKIE, repr(post))

    def test_redirect_does_not_forward_cookie_and_reports_failure(self):
        for location in ("https://www.threads.com/login", "https://www.threads.com/challenge/",
                         "https://example.com/", "https://scontent.cdninstagram.com/post.jpg"):
            with self.subTest(location=location):
                self.session.get.reset_mock()
                self.session.get.return_value = response("", 302, Location=location)
                post = self.fetch()
                self.assertTrue(post.preview_error.startswith("cookie_"))
                self.session.get.assert_called_once()
                self.requests.get.assert_not_called()
                self.session.get.return_value.iter_content.assert_not_called()
                self.assertNotIn(location, repr(post.debug))

    def test_anonymous_text_or_media_never_uses_cookie(self):
        for post in (tf.ThreadsPost(url=URL, text="匿名正文"),
                     tf.ThreadsPost(url=URL, media=[tf.ThreadsMedia("image", IMAGE)]),
                     tf.ThreadsPost(url=URL, media=[tf.ThreadsMedia("video", VIDEO)])):
            with self.subTest(post=post):
                self.assertIs(self.fetch(post), post)
        self.requests.Session.assert_not_called()

    def test_audience_restriction_notice_does_not_bypass_cookie_fallback(self):
        post = self.fetch(tf._parse_html(URL, AUDIENCE_HTML))
        self.assertEqual(post.text, "目標正文")
        self.session.get.assert_called_once()

    def test_restriction_is_recognized_without_matching_translated_text(self):
        body = AUDIENCE_HTML.replace("This content isn't available to everyone", "此內容目前不向所有人開放").replace(
            "It can't be seen by certain audiences.", "部分用戶無法查看。")
        anonymous = tf._parse_html(URL, body)
        self.assertEqual(anonymous.preview_error, "restricted_content")
        self.assertIsNone(anonymous.text)
        self.assertEqual(self.fetch(anonymous).text, "目標正文")

    def test_restriction_without_cookie_is_an_explicit_failure(self):
        with patch.dict(os.environ, {"THREADS_COOKIE": ""}):
            post = self.fetch(tf._parse_html(URL, AUDIENCE_HTML))
        self.assertEqual(post.preview_error, "restricted_content")
        self.assertIsNone(post.text)
        self.requests.Session.assert_not_called()

    def test_cookie_still_restricted_stops_and_does_not_claim_expiration(self):
        self.session.get.return_value = response(AUDIENCE_HTML)
        post = self.fetch(tf._parse_html(URL, AUDIENCE_HTML))
        self.assertEqual(post.preview_error, "cookie_restricted_content")
        self.assertIsNone(post.text)
        self.session.get.assert_called_once()

    def test_unrelated_route_or_plain_notice_quote_is_not_a_restriction(self):
        for body in (AUDIENCE_HTML.replace("Cookie001", "Other001"),
                     AUDIENCE_HTML.replace("BarcelonaGeoBlockedErrorRoot.entrypoint", "BarcelonaPostRoot.entrypoint"),
                     '<span dir="auto">This content isn\'t available to everyone</span>'):
            with self.subTest(body=body):
                post = tf._parse_html(URL, body)
                self.assertIsNone(post.preview_error)
                self.assertIn("available to everyone", post.text)
                self.assertIs(self.fetch(post), post)
        self.requests.Session.assert_not_called()

    def test_verified_restriction_stops_anonymous_ua_retries(self):
        self.requests.get.return_value = SimpleNamespace(status_code=200, url=URL, text=AUDIENCE_HTML)
        post = tf._try_requests(URL)
        self.assertEqual(post.preview_error, "restricted_content")
        self.assertEqual(post.debug["requests_attempts"], 1)
        self.requests.get.assert_called_once()

    def test_cookie_page_includes_target_posts_linked_instagram_video_only(self):
        payload = target(text_post_app_info={"linked_inline_media": {"code": "Ig001", "video_versions": [{"url": VIDEO}]}},
                         quoted_post=target(code="Quote001", text_post_app_info={"linked_inline_media": {"video_url": VIDEO + "&quote=1"}}))
        self.session.get.return_value = response(page(payload))
        post = self.fetch()
        self.assertEqual(post.text, "目標正文")
        self.assertEqual([item.url for item in post.media], [VIDEO])

    def test_cookie_unset_or_blank_preserves_anonymous_failure(self):
        for value in ("", "   "):
            with self.subTest(value=value), patch.dict(os.environ, {"THREADS_COOKIE": value}):
                empty = tf.ThreadsPost(url=URL, preview_error="invalid_post")
                self.assertIs(self.fetch(empty), empty)
        self.requests.Session.assert_not_called()

    def test_fallback_avatar_is_not_treated_as_usable_anonymous_media(self):
        post = self.fetch(tf.ThreadsPost(url=URL, media=[
            tf.ThreadsMedia("image", "https://scontent.cdninstagram.com/avatar.jpg", 150, 150)]))
        self.assertEqual(post.text, "目標正文")
        self.session.get.assert_called_once()

    def test_anonymous_budget_is_unchanged_and_cookie_never_reaches_other_requests(self):
        self.requests.get.return_value = response("", 503)
        self.requests.get.return_value.url = URL
        self.requests.get.return_value.text = ""
        with patch.object(tf, "_try_embed_fill", EMBED_FILL), patch.object(tf, "_try_oembed_fill", OEMBED_FILL), patch.object(tf.time, "sleep"):
            post = tf.fetch_threads_post(URL)
        self.assertEqual(post.text, "目標正文")
        self.assertEqual(self.requests.get.call_count, 8)
        self.session.get.assert_called_once()
        for call in self.requests.get.call_args_list:
            self.assertNotIn("Cookie", call.kwargs["headers"])
        self.assertEqual(post.debug["requests_attempts"], 6)
        self.assertEqual(post.debug["cookie_attempts"], 1)

    def test_share_resolution_stays_anonymous_and_cookie_uses_canonical_https_host(self):
        canonical = URL.replace("https://www.threads.com", "http://threads.net")
        with patch.object(tf, "_resolve_threads_share_url", return_value=canonical) as resolve:
            post = self.fetch(url="https://www.threads.com/share/share-token")
        resolve.assert_called_once_with("https://www.threads.com/share/share-token")
        self.assertEqual(self.session.get.call_args.args, (URL + "?hl=en",))
        self.assertEqual(post.url, URL)

    def test_invalid_cookie_is_rejected_before_request_without_exposing_value(self):
        values = ["Cookie: " + COOKIE, "sessionid=fake\r\nX-Test: injection", "sessionid=假資料", "x=" + "a" * 16384]
        for value in values:
            with self.subTest(kind=values.index(value)), patch.dict(os.environ, {"THREADS_COOKIE": value}), self.assertLogs("discord_digest_bot") as logs:
                post = self.fetch()
                self.assertEqual(post.preview_error, "cookie_config_invalid")
                self.assertNotIn(value, repr(post) + repr(logs.output))
        self.requests.Session.assert_not_called()

    def test_http_failures_stop_without_reading_response_or_retrying(self):
        for status, reason in ((401, "cookie_login_required"), (403, "cookie_login_required"),
                               (429, "cookie_rate_limited"), (503, "cookie_fetch_failed")):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.session.get.return_value = response("secret-login-body", status)
                post = self.fetch()
                self.assertEqual(post.preview_error, reason)
                self.session.get.assert_called_once()
                self.session.get.return_value.iter_content.assert_not_called()
                self.session.get.return_value.close.assert_called_once()

    def test_login_and_challenge_forms_are_not_preview_content(self):
        for body in ('<input type="password">', '<form action="/checkpoint/">',
                     '<input name="approvals_code">', '<form action="/accounts/login/?next=/">'):
            with self.subTest(body=body):
                self.session.get.return_value = response(body + page({"data": target()}))
                self.assertEqual(self.fetch().preview_error, "cookie_login_required")

    def test_javascript_login_keywords_do_not_misclassify_valid_post(self):
        self.session.get.return_value = response('<script>"login checkpoint challenge"</script>' + page(target()))
        self.assertEqual(self.fetch().text, "目標正文")

    def test_transport_and_stream_exceptions_never_expose_cookie_or_body(self):
        for failure in ("request", "stream"):
            with self.subTest(failure=failure), self.assertLogs("discord_digest_bot") as logs, patch.object(tf, "_save_debug_html") as save:
                self.session.get.reset_mock(side_effect=True)
                self.session.get.return_value = response("private-account-page")
                error = RuntimeError(COOKIE + " private-account-page")
                if failure == "request":
                    self.session.get.side_effect = error
                else:
                    self.session.get.return_value.iter_content.side_effect = error
                post = self.fetch()
                self.assertEqual(post.preview_error, "cookie_fetch_failed")
                self.session.get.assert_called_once()
                self.assertNotIn(COOKIE, repr(post) + repr(logs.output))
                self.assertNotIn("private-account-page", repr(post) + repr(logs.output))
                save.assert_not_called()
                if failure == "stream":
                    self.session.get.return_value.close.assert_called_once()

    def test_html_size_limit_stops_stream_and_closes_response(self):
        res = response("")
        res.iter_content.return_value = iter([b"a" * tf.MAX_EMBED_HTML_BYTES, b"x", b"never-read"])
        self.session.get.return_value = res
        self.assertEqual(self.fetch().preview_error, "cookie_fetch_failed")
        self.assertEqual(next(res.iter_content.return_value), b"never-read")
        res.close.assert_called_once()

    def test_only_target_caption_and_media_are_extracted(self):
        self.session.get.return_value = response(
            '<span dir="auto">帳號個人資訊</span><meta property="og:description" content="錯誤內容">' + page({
                "recommendations": [target(code="Other001", caption={"text": "推薦內容"})],
                "post": target(carousel_media=[
                    {"image_versions2": {"candidates": [{"url": IMAGE, "width": 800, "height": 1000}]}},
                    {"code": "CarouselChild", "video_versions": [
                        {"url": VIDEO, "width": 1080, "height": 1920},
                        {"url": VIDEO + "&low=1", "width": 320, "height": 480}]},
                ], quoted_post=target(code="Quoted001", caption={"text": "引用內容"}, video_url=VIDEO + "&quote=1")),
            }))
        post = self.fetch()
        self.assertEqual(post.text, "目標正文")
        self.assertEqual(post.author_name, "Demo")
        self.assertEqual([m.url for m in post.media], [IMAGE, VIDEO])
        for unwanted in ("帳號個人資訊", "推薦內容", "引用內容", "錯誤內容"):
            self.assertNotIn(unwanted, repr(post))

    def test_unbound_dom_json_and_private_post_do_not_become_a_preview(self):
        bodies = [
            '<span dir="auto">個人資訊</span>' + page(target(code="Other001")),
            page({"caption": {"text": "未綁定的內容"}}),
            page(target(user={"username": "demo", "is_private": True})),
            '<html><script type="application/json">invalid json</script></html>',
        ]
        for body in bodies:
            with self.subTest(body=body), patch.object(tf, "_save_debug_html") as save:
                self.session.get.return_value = response(body)
                post = self.fetch()
                self.assertEqual(post.preview_error, "cookie_no_content")
                self.assertIsNone(post.text)
                save.assert_not_called()

    def test_json_ld_is_accepted_only_with_matching_permalink(self):
        data = [{"@type": "SocialMediaPosting", "url": URL + "Other", "articleBody": "其他貼文"},
                {"@type": "SocialMediaPosting", "mainEntityOfPage": {"@id": URL},
                 "articleBody": "JSON-LD 正文", "image": [IMAGE], "video": [{"contentUrl": VIDEO}]}]
        self.session.get.return_value = response(page(data, "application/ld+json"))
        post = self.fetch()
        self.assertEqual(post.text, "JSON-LD 正文")
        self.assertEqual([m.url for m in post.media], [VIDEO])  # Unordered JSON-LD image is the video cover.

    def test_unsafe_media_and_avatars_are_rejected(self):
        for media in ({"video_url": "http://127.0.0.1/private.mp4"},
                      {"image_versions2": {"candidates": [{"url": "https://example.com/untrusted.jpg"}]}},
                      {"image_versions2": {"candidates": [{"url": IMAGE, "width": 150, "height": 150}]}}):
            with self.subTest(media=media):
                self.session.get.return_value = response(page(target(caption=None, **media)))
                post = self.fetch()
                self.assertEqual(post.preview_error, "cookie_no_content")
                self.assertEqual(post.media, [])

    def test_authenticated_html_is_never_saved_even_on_success(self):
        with patch.object(tf, "_save_debug_html") as save:
            self.assertEqual(self.fetch().text, "目標正文")
            save.assert_not_called()

    def test_no_content_reports_safe_diagnostic_reason_without_page_or_cookie(self):
        self.session.get.return_value = response('<html><body>private-account-page<script>opaque bootstrap</script></body></html>')
        with self.assertLogs("discord_digest_bot") as logs:
            post = self.fetch()
        self.assertEqual(post.debug["cookie_reason"], "json_missing")
        self.assertEqual(post.debug["cookie_json_count"], 0)
        self.assertEqual(post.debug["cookie_matched_nodes"], 0)
        self.assertIn("reason=json_missing", repr(logs.output))
        self.assertNotIn("private-account-page", repr(logs.output))
        self.assertNotIn(COOKIE, repr(logs.output))

    def test_large_bootstrap_before_post_does_not_exhaust_target_search(self):
        # Authenticated pages contain large bootstraps before Relay post data.
        bootstrap = {"settings": [{"flag": i} for i in range(21000)]}
        self.session.get.return_value = response(page(bootstrap) + page({"data": target()}))
        post = self.fetch()
        self.assertEqual(post.text, "目標正文")
        self.assertIsNone(post.preview_error)
        self.session.get.assert_called_once()

    def test_large_shared_script_before_target_is_still_parsed(self):
        payload = {"settings": list(range(22000)), "data": target()}
        self.session.get.return_value = response(page(payload))
        self.assertEqual(self.fetch().text, "目標正文")

    def test_route_with_matching_url_does_not_hide_nested_post(self):
        self.session.get.return_value = response(page({"route": {
            "url": URL, "actorID": "123", "rootView": {"props": {"post": target()}}
        }}))
        post = self.fetch()
        self.assertEqual(post.text, "目標正文")
        self.assertEqual(post.debug["cookie_viewer_hint"], "present")

    def test_escaped_post_code_is_found_after_bootstrap(self):
        payload = page({"data": target()}).replace("Cookie001", "\\u0043ookie001")
        self.session.get.return_value = response(page({"settings": list(range(22000))}) + payload)
        self.assertEqual(self.fetch().text, "目標正文")

    def test_matching_route_does_not_bind_unrelated_child_content(self):
        self.session.get.return_value = response(page({"url": URL, "data": target(code="Other001")}))
        post = self.fetch()
        self.assertEqual(post.preview_error, "cookie_no_content")
        self.assertIsNone(post.text)

    def test_container_search_budget_remains_bounded(self):
        self.session.get.return_value = response(page({"rows": [{}, {}, {}, target()]}))
        with patch("discord_bot.features.social_preview.threads_cookie.MAX_COOKIE_PARSE_NODES", 3):
            post = self.fetch()
        self.assertEqual(post.preview_error, "cookie_no_content")
        self.assertEqual(post.debug["cookie_reason"], "parse_budget_exhausted")

    def test_private_target_does_not_fall_through_to_nested_content(self):
        self.session.get.return_value = response(page(target(
            user={"is_private": True}, nested=target(caption={"text": "私人內容"})
        )))
        post = self.fetch()
        self.assertEqual(post.debug["cookie_reason"], "private_post")
        self.assertEqual(post.preview_error, "cookie_no_content")
        self.assertIsNone(post.text)

    def test_image_or_video_only_post_is_usable_without_caption(self):
        for media, expected in (({"video_versions": [{"url": VIDEO}]}, VIDEO),
                                ({"image_versions2": {"candidates": [{"url": IMAGE}]}}, IMAGE)):
            with self.subTest(expected=expected):
                self.session.get.return_value = response(page(target(caption=None, **media)))
                post = self.fetch()
                self.assertIsNone(post.preview_error)
                self.assertIsNone(post.text)
                self.assertEqual([item.url for item in post.media], [expected])

    def test_untrusted_or_non_post_input_never_receives_cookie(self):
        from discord_bot.features.social_preview.threads_cookie import try_cookie_fallback

        for url in ("https://example.com/@demo/post/Cookie001", "https://www.threads.com/login",
                    "https://www.threads.com@example.com/@demo/post/Cookie001",
                    "https://www.threads.com:8443/@demo/post/Cookie001",
                    "https://www.threads.com/@demo/post/Cookie001/extra"):
            with self.subTest(url=url):
                self.assertIsNotNone(try_cookie_fallback(url, COOKIE).preview_error)
        self.requests.Session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
