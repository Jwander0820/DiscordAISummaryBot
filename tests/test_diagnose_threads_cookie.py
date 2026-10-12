import contextlib
import io
import json
import unittest
from unittest.mock import patch

from tools import diagnose_threads_cookie as diagnostic
from discord_bot.features.social_preview.threads_fetch import ThreadsPost


class DiagnoseThreadsCookieTests(unittest.TestCase):
    def test_tool_uses_configured_cookie_but_never_prints_sensitive_result_fields(self):
        cookie = "sessionid=synthetic-diagnostic-only"
        post = ThreadsPost(url="https://www.threads.com/@demo/post/abc", text="private post text",
                           debug={"cookie_reason": "target_found", "raw_html": "private account html"})
        output = io.StringIO()
        with patch.dict("os.environ", {"THREADS_COOKIE": cookie}), patch(
            "sys.argv", ["diagnose_threads_cookie.py", post.url]
        ), patch("discord_bot.features.social_preview.threads_cookie.try_cookie_fallback", return_value=post) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 0)
        fetch.assert_called_once_with(post.url, cookie)
        result = json.loads(output.getvalue())
        self.assertEqual(result["result"], "success")
        self.assertTrue(result["has_text"])
        for sensitive in (cookie, post.text, "private account html"):
            self.assertNotIn(sensitive, output.getvalue())

    def test_missing_cookie_reports_configuration_failure_without_network(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"THREADS_COOKIE": ""}), patch(
            "sys.argv", ["diagnose_threads_cookie.py", "https://www.threads.com/@demo/post/abc"]
        ), patch("pathlib.Path.is_file", return_value=False), patch(
            "discord_bot.features.social_preview.threads_cookie.try_cookie_fallback"
        ) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(diagnostic.main(), 2)
        fetch.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"result": "cookie_not_configured"})
