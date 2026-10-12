import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

from tools import diagnose_facebook_cookie as diagnostic
from discord_bot.features.social_preview.facebook_fetch import FacebookPostData


class DiagnoseFacebookCookieTests(unittest.TestCase):
    def test_config_check_uses_no_network_and_exposes_no_values_or_names(self):
        cookie = "c_user=synthetic-user; xs=synthetic-secret; private-name=hidden"
        output = io.StringIO()
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": cookie}), patch("sys.argv", [
            "diagnose_facebook_cookie.py", "https://www.facebook.com/reel/123", "--check-config"
        ]), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock()) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        fetch.assert_not_awaited()
        result = json.loads(output.getvalue())
        self.assertEqual(result["cookie_pairs"], 3)
        self.assertTrue(result["has_c_user"] and result["has_xs"])
        self.assertEqual(result["requests"], 0)
        for value in (cookie, "synthetic-user", "synthetic-secret", "private-name", "hidden"):
            self.assertNotIn(value, output.getvalue())

    def test_diagnostic_uses_fake_config_but_only_outputs_allowlisted_status(self):
        cookie = "c_user=synthetic; xs=fake-diagnostic-only"
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        post = FacebookPostData(url=url, description="sensitive body", image_urls=["https://example.com/private-image"],
                                public=True, debug={"raw_html": "private html", "cookie_status": 200})
        output = io.StringIO()
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": cookie}), patch("sys.argv", ["diagnose_facebook_cookie.py", url]), patch(
            "discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock(return_value=post)
        ) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        fetch.assert_awaited_once_with(url, cookie, persist_cookies=False)
        result = json.loads(output.getvalue())
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["image_count"], 1)
        for value in (cookie, post.description, post.image_urls[0], "private html"):
            self.assertNotIn(value, output.getvalue())

    def test_missing_config_performs_no_request(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"FACEBOOK_COOKIE": ""}), patch("sys.argv", ["diagnose_facebook_cookie.py", "https://www.facebook.com/reel/123"]), patch(
            "pathlib.Path.is_file", return_value=False
        ), patch("discord_bot.features.social_preview.facebook_cookie.try_facebook_cookie", new=AsyncMock()) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 2)
        fetch.assert_not_awaited()
        self.assertEqual(json.loads(output.getvalue()), {"result": "cookie_not_configured"})
