import html
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from discord_bot.features.social_preview import instagram_video_fallback as fallback

URL = "https://www.instagram.com/reel/ABC123"
VIDEO = "https://scontent.cdninstagram.com/video.mp4?sig=synthetic&expires=123"


def response(status=200, body="", **headers):
    return SimpleNamespace(status_code=status, headers=headers,
                           iter_content=Mock(return_value=iter([body.encode()])), close=Mock())


def metadata(canonical=URL, video=VIDEO):
    return (f'<meta property="og:url" content="{html.escape(canonical)}">'
            f'<meta property="og:video" content="{html.escape(video)}">')


class InstagramVideoFallbackTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        session_patch = patch("requests.Session", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def test_cdn_redirect_is_returned_without_following_or_reading_media(self):
        reply = response(302, Location=VIDEO)
        self.session.get.return_value = reply
        with patch.dict(os.environ, {"INSTAGRAM_COOKIE": "sessionid=synthetic-secret",
                                     "HTTPS_PROXY": "https://proxy.invalid", "NETRC": "secret-netrc"}):
            self.assertEqual(fallback.resolve_instagram_video_fallback(URL), VIDEO)
        self.session.get.assert_called_once()
        call = self.session.get.call_args
        self.assertEqual(call.args, ("https://www.kkinstagram.com/reels/ABC123/",))
        self.assertEqual(set(call.kwargs["headers"]), {"User-Agent", "Accept"})
        self.assertFalse(self.session.trust_env)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        reply.iter_content.assert_not_called()
        reply.close.assert_called_once()

    def test_html_video_requires_matching_post_identity(self):
        for canonical in (URL, "https://www.kkinstagram.com/reels/ABC123/",
                          "https://www.instagram.com/author/reel/ABC123/"):
            self.session.get.return_value = response(body=metadata(canonical), **{"Content-Type": "text/html; charset=utf-8"})
            self.assertEqual(fallback.resolve_instagram_video_fallback(URL), VIDEO)
        for canonical in ("https://www.instagram.com/reel/WRONG", "https://evil.example/reel/ABC123",
                          "https://user@www.instagram.com/reel/ABC123", ""):
            self.session.get.return_value = response(body=metadata(canonical), **{"Content-Type": "text/html"})
            self.assertIsNone(fallback.resolve_instagram_video_fallback(URL))

    def test_redirect_rejects_untrusted_and_partial_media_urls(self):
        for video in ("https://evil.example/video.mp4", "http://scontent.cdninstagram.com/video.mp4",
                      "https://scontent.cdninstagram.com.evil.example/video.mp4",
                      "https://user@scontent.cdninstagram.com/video.mp4",
                      "https://scontent.cdninstagram.com:8443/video.mp4",
                      "https://scontent.cdninstagram.com/video.m3u8",
                      "https://scontent.cdninstagram.com/video.mpd", VIDEO + "&byteStart=0",
                      VIDEO + "&byteEnd=12", VIDEO + "&range=", "/video.mp4",
                      "https://www.kkinstagram.com/reels/ABC123/"):
            with self.subTest(video=video):
                self.session.get.reset_mock()
                self.session.get.return_value = response(302, Location=video)
                self.assertIsNone(fallback.resolve_instagram_video_fallback(URL))
                self.session.get.assert_called_once()

    def test_error_status_and_video_body_are_never_read(self):
        for status, content_type in ((404, "text/html"), (429, "text/html"),
                                     (500, "text/html"), (200, "video/mp4")):
            reply = response(status, **{"Content-Type": content_type})
            self.session.get.return_value = reply
            self.assertIsNone(fallback.resolve_instagram_video_fallback(URL))
            reply.iter_content.assert_not_called()
            reply.close.assert_called_once()

    def test_html_limit_stops_before_later_chunks(self):
        consumed = []

        def chunks():
            for chunk in (b"x" * 20, b"x", b"must-not-read"):
                consumed.append(chunk)
                yield chunk

        reply = response(**{"Content-Type": "text/html"})
        reply.iter_content.return_value = chunks()
        self.session.get.return_value = reply
        with patch.object(fallback, "MAX_FALLBACK_HTML_BYTES", 20):
            self.assertIsNone(fallback.resolve_instagram_video_fallback(URL))
        self.assertEqual(consumed, [b"x" * 20, b"x"])
        reply.close.assert_called_once()

    def test_exception_logs_do_not_include_signed_url_or_credentials(self):
        self.session.get.side_effect = RuntimeError(VIDEO + " sessionid=synthetic-secret")
        with self.assertLogs(fallback.logger, level="INFO") as logs:
            self.assertIsNone(fallback.resolve_instagram_video_fallback(URL))
        self.assertIn("request_failed", " ".join(logs.output))
        self.assertNotIn("synthetic", " ".join(logs.output))
