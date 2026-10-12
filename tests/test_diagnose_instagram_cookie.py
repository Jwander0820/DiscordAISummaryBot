import contextlib
import io
import json
import unittest
from unittest.mock import patch

from tools import diagnose_instagram_cookie as diagnostic
from discord_bot.features.social_preview.instagram_fetch import InstagramPost, InstagramMedia

URL = "https://www.instagram.com/p/ABC123"


class DiagnoseInstagramCookieTests(unittest.TestCase):
    def test_config_check_never_uses_network_or_emits_values(self):
        output = io.StringIO()
        with patch.object(diagnostic, "_load_cookie", return_value="sessionid=synthetic-secret; csrftoken=synthetic-csrf"), patch(
            "sys.argv", ["diagnostic", URL, "--check-config"]
        ), patch("discord_bot.features.social_preview.instagram_direct._request_post") as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        fetch.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"result": "cookie_format_valid", "has_sessionid": True, "requests": 0})
        self.assertNotIn("synthetic", output.getvalue())

    def test_missing_cookie_never_uses_network(self):
        with patch.object(diagnostic, "_load_cookie", return_value=""), patch("sys.argv", ["diagnostic", URL]), patch(
            "discord_bot.features.social_preview.instagram_direct._request_post"
        ) as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.main(), 2)
        fetch.assert_not_called()

    def test_one_explicit_request_disables_persistence_and_redacts_content(self):
        output = io.StringIO()
        post = InstagramPost(url=URL, shortcode="ABC123", text="private diagnostic text", media=[InstagramMedia("image", "https://example.com/private")])
        with patch.object(diagnostic, "_load_cookie", return_value="sessionid=synthetic"), patch("sys.argv", ["diagnostic", URL]), patch(
            "discord_bot.features.social_preview.instagram_direct._request_post", return_value=post
        ) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        fetch.assert_called_once_with(URL, "sessionid=synthetic", persist_cookies=False)
        self.assertEqual(json.loads(output.getvalue()), {"result": "success", "has_text": True, "image_count": 1, "video_count": 0})
        self.assertNotIn("private", output.getvalue())
