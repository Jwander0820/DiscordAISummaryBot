import json
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from discord_bot.features.social_preview.facebook_cookie import try_facebook_cookie


URL = "https://www.facebook.com/demo/posts/pfbidTEST"
COOKIE = "c_user=synthetic-user; xs=synthetic-test-only"
IMAGE = "https://scontent.xx.fbcdn.net/photo.jpg"


def html_post(**extra):
    post = {"url": URL, "privacy_scope": {"label": "Public"}, "message": {"text": "公開圖片正文"},
            "attachments": [{"styles": {"attachment": {"media": {"__typename": "Photo", "image": {"uri": IMAGE}}}}}], **extra}
    return '<script type="application/json">' + json.dumps(post) + '</script>'


class Response:
    def __init__(self, body="", status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.chunks = chunks
        self.closed = False
        self.reads = 0
        self.content = types.SimpleNamespace(iter_chunked=self.iter_chunked)

    async def iter_chunked(self, size):
        for chunk in self.chunks if self.chunks is not None else [self.body.encode("utf-8")]:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True


class Session:
    def __init__(self, response):
        self.get = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class FacebookCookieTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.response = Response(html_post())
        self.session = Session(self.response)
        self.client_patch = patch("discord_bot.features.social_preview.facebook_cookie.aiohttp.ClientSession", return_value=self.session)
        self.client = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_successful_response_rotates_cookie_for_next_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "SOCIAL_PREVIEW_COOKIE_STORE_DIR": directory
        }):
            self.session.get.return_value = Response(html_post(), headers={"Set-Cookie": "xs=rotated-test; Domain=.facebook.com; Path=/; Secure"})
            await try_facebook_cookie(URL, COOKIE)
            self.session.get.return_value = Response(html_post())
            await try_facebook_cookie(URL, COOKIE)
            header = self.session.get.call_args.kwargs["headers"]["Cookie"]
            self.assertIn("xs=rotated-test", header)
            self.assertNotIn("synthetic-test-only", header)
            self.assertEqual(self.session.get.call_count, 2)

    async def test_failed_or_private_response_never_saves_server_updates(self):
        with patch("discord_bot.features.social_preview.facebook_cookie.save_response_cookies") as save:
            for body, status in (("login", 403), (html_post(privacy_scope={"label": "Friends"}), 200), ("empty", 200)):
                self.session.get.return_value = Response(body, status, headers={"Set-Cookie": "xs=bad; Path=/"})
                await try_facebook_cookie(URL, COOKIE)
            save.assert_not_called()

    async def test_deleted_cached_cookie_does_not_send_original_seed_again(self):
        from discord_bot.features.social_preview.cookie_store import prepare_cookie, save_response_cookies
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "SOCIAL_PREVIEW_COOKIE_STORE_DIR": directory
        }):
            snapshot = prepare_cookie("facebook", URL, COOKIE)
            save_response_cookies(snapshot, ["c_user=; Path=/; Max-Age=0", "xs=; Path=/; Max-Age=0"])
            post = await try_facebook_cookie(URL, COOKIE)
            self.assertEqual(post.reason, "cookie_login_required")
            self.session.get.assert_not_called()

    async def test_one_request_uses_only_canonical_facebook_host_and_target_public_content(self):
        post = await try_facebook_cookie(URL.replace("https://www", "http://m"), COOKIE)
        self.assertEqual(post.description, "公開圖片正文")
        self.assertEqual(post.image_urls, [IMAGE])
        self.assertTrue(post.public)
        self.assertEqual(post.debug["cookie_attempts"], 1)
        self.assertEqual(post.debug["cookie_status"], 200)
        self.session.get.assert_called_once()
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, (URL,))
        self.assertEqual(kwargs["headers"]["Cookie"], COOKIE)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(self.client.call_args.kwargs["trust_env"])
        self.assertTrue(self.response.closed)
        self.assertNotIn(COOKIE, repr(post))

    async def test_authenticated_document_sends_navigation_headers(self):
        await try_facebook_cookie(URL, COOKIE)
        headers = self.session.get.call_args.kwargs["headers"]
        self.assertEqual(headers.get("Sec-Fetch-Site"), "none")
        self.assertEqual(headers.get("Sec-Fetch-Mode"), "navigate")
        self.assertEqual(headers.get("Sec-Fetch-Dest"), "document")

    async def test_public_description_only_scope_can_supply_image(self):
        self.session.get.return_value = Response(html_post(privacy_scope={"description": "所有人"}))
        post = await try_facebook_cookie(URL, COOKIE)
        self.assertTrue(post.public)
        self.assertEqual(post.image_urls, [IMAGE])

    async def test_invalid_cookie_is_rejected_before_any_request(self):
        for cookie in ("", "Cookie: " + COOKIE, "a=1\r\nInjected: yes", "a=中文", "x=" + "a" * 16384):
            with self.subTest(kind=cookie[:10]):
                self.assertEqual((await try_facebook_cookie(URL, cookie)).reason, "cookie_config_invalid")
        self.client.assert_not_called()

    async def test_untrusted_or_unresolved_targets_never_receive_cookie(self):
        for url in ("https://example.com/reel/123", "https://www.facebook.com/login", "https://www.facebook.com/share/v/token/",
                    "https://www.facebook.com:8443/reel/123", "https://www.facebook.com@example.com/reel/123"):
            with self.subTest(url=url):
                self.assertEqual((await try_facebook_cookie(url, COOKIE)).reason, "cookie_target_unresolved")
        self.client.assert_not_called()

    async def test_redirects_never_forward_cookie_even_to_another_facebook_page(self):
        for location in ("https://www.facebook.com/reel/456", "https://example.com/", "https://www.facebook.com/login/",
                         "https://video.xx.fbcdn.net/video.mp4"):
            with self.subTest(location=location):
                response = Response("sensitive html", status=302, headers={"Location": location})
                self.session.get.reset_mock()
                self.session.get.return_value = response
                self.assertFalse((await try_facebook_cookie(URL, COOKIE)).has_content)
                self.session.get.assert_called_once()
                self.assertEqual(response.reads, 0)
                self.assertTrue(response.closed)

    async def test_http_failures_do_not_read_body_or_retry(self):
        for status, expected in ((401, "cookie_login_required"), (403, "cookie_login_required"),
                                 (429, "cookie_rate_limited"), (503, "cookie_fetch_failed")):
            with self.subTest(status=status):
                response = Response("sensitive html", status=status)
                self.session.get.reset_mock()
                self.session.get.return_value = response
                self.assertEqual((await try_facebook_cookie(URL, COOKIE)).reason, expected)
                self.session.get.assert_called_once()
                self.assertEqual(response.reads, 0)

    async def test_bad_request_only_keeps_bounded_fixed_hint_and_never_retries(self):
        for body, expected in (("Bad Request " + COOKIE, "bad_request"),
                               ("Request header or cookie too large " + COOKIE, "headers_too_large"),
                               ("Security check " + COOKIE, "access_challenge"),
                               ('<input type="password">' + COOKIE, "login_required"),
                               ("sensitive unknown body " + COOKIE, "unclassified")):
            with self.subTest(expected=expected):
                self.session.get.reset_mock()
                self.session.get.return_value = Response(body, status=400)
                with self.assertLogs("discord_digest_bot") as logs:
                    post = await try_facebook_cookie(URL, COOKIE)
                self.session.get.assert_called_once()
                self.assertEqual(post.reason, "cookie_request_rejected")
                self.assertEqual(post.debug["cookie_error_hint"], expected)
                self.assertNotIn(COOKIE, repr(post) + repr(logs.output))
                self.assertNotIn("sensitive unknown body", repr(post) + repr(logs.output))
        response = Response(status=400, chunks=[b"x" * (64 * 1024), b"never read"])
        self.session.get.return_value = response
        post = await try_facebook_cookie(URL, COOKIE)
        self.assertEqual(response.reads, 1)
        self.assertEqual(post.debug["response_bytes"], 64 * 1024)

    async def test_private_unknown_and_other_post_data_never_become_a_preview(self):
        for changes in ({"privacy_scope": {"label": "Friends"}}, {"privacy_scope": None},
                        {"is_private": True}, {"url": "https://www.facebook.com/demo/posts/pfbidOTHER"}):
            self.session.get.return_value = Response(html_post(**changes))
            with self.assertLogs("discord_digest_bot") as logs:
                post = await try_facebook_cookie(URL, COOKIE)
            self.assertFalse(post.has_content)
            self.assertNotIn("公開圖片正文", repr(post) + repr(logs.output))
            self.assertNotIn(COOKIE, repr(post) + repr(logs.output))

    async def test_login_form_stops_even_if_unrelated_post_data_exists(self):
        self.session.get.return_value = Response('<form id="login_form"><input type="password"></form>' + html_post())
        self.assertEqual((await try_facebook_cookie(URL, COOKIE)).reason, "cookie_login_required")

    async def test_body_size_limit_stops_stream(self):
        response = Response(chunks=[b"x" * 20, b"y", b"never-read"])
        self.session.get.return_value = response
        with patch("discord_bot.features.social_preview.facebook_cookie.MAX_FACEBOOK_HTML_BYTES", 20):
            result = await try_facebook_cookie(URL, COOKIE)
            self.assertEqual(result.reason, "response_too_large")
            self.assertEqual(result.debug["response_bytes"], 21)
        self.assertEqual(response.reads, 2)
        self.assertTrue(response.closed)

    async def test_document_slightly_above_four_mib_can_reach_target_parser(self):
        self.session.get.return_value = Response(" " * (4 * 1024 * 1024 + 1) + html_post())
        post = await try_facebook_cookie(URL, COOKIE)
        self.assertEqual(post.image_urls, [IMAGE])
        self.assertGreater(post.debug["response_bytes"], 4 * 1024 * 1024)

    async def test_transport_and_stream_failures_never_log_cookie_or_html(self):
        for location in ("transport", "stream"):
            self.session.get.reset_mock(side_effect=True)
            error = RuntimeError(COOKIE + " sensitive html")
            if location == "transport":
                self.session.get.side_effect = error
            else:
                self.session.get.return_value = Response(chunks=[error])
            with self.assertLogs("discord_digest_bot") as logs:
                post = await try_facebook_cookie(URL, COOKIE)
            self.assertEqual(post.reason, "cookie_fetch_failed")
            self.assertNotIn(COOKIE, repr(post) + repr(logs.output))
            self.assertNotIn("sensitive html", repr(post) + repr(logs.output))


if __name__ == "__main__":
    unittest.main()
