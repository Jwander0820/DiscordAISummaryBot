import unittest
import re
from urllib.parse import urlparse, urlunparse

from discord_bot.features.social_preview.text import extract_message_commentary


THREADS_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@[^\s/]+/post/[A-Za-z0-9_\-]+(?:\?[^\s<>()|]+)?(?:#[^\s<>()|]+)?",
    re.IGNORECASE,
)
FACEBOOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|mbasic\.)?(?:facebook\.com|fb\.watch)/[^\s<>()]+",
    re.IGNORECASE,
)


def sanitize_threads_url(url: str) -> str:
    clean = url.rstrip(").,>|")
    parsed = urlparse(clean)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))


class SocialPreviewTextTests(unittest.TestCase):
    def test_extract_message_commentary_keeps_text_around_threads_url(self):
        url = "https://www.threads.com/@aa6301102/post/DWS_66xE__F"
        content = f"一起來看 {url} 超好笑"

        commentary = extract_message_commentary(
            content,
            target_url=url,
            url_pattern=THREADS_URL_RE,
            sanitize_url=sanitize_threads_url,
        )

        self.assertEqual(commentary, "一起來看 超好笑")

    def test_extract_message_commentary_removes_empty_spoiler_wrappers(self):
        url = "https://www.threads.com/@aa6301102/post/DWS_66xE__F"
        content = f"一起來看 ||{url}|| 超好笑"

        commentary = extract_message_commentary(
            content,
            target_url=url,
            url_pattern=THREADS_URL_RE,
            sanitize_url=sanitize_threads_url,
        )

        self.assertEqual(commentary, "一起來看 超好笑")

    def test_extract_message_commentary_preserves_existing_spoiler_text(self):
        url = "https://www.threads.com/@aa6301102/post/DWS_66xE__F"
        content = f"||一起來看 {url} 超好笑||"

        commentary = extract_message_commentary(
            content,
            target_url=url,
            url_pattern=THREADS_URL_RE,
            sanitize_url=sanitize_threads_url,
        )

        self.assertEqual(commentary, "||一起來看 超好笑||")

    def test_extract_message_commentary_returns_empty_when_only_url(self):
        url = "https://www.threads.com/@aa6301102/post/DWS_66xE__F"

        commentary = extract_message_commentary(
            url,
            target_url=url,
            url_pattern=THREADS_URL_RE,
            sanitize_url=sanitize_threads_url,
        )

        self.assertEqual(commentary, "")

    def test_extract_message_commentary_keeps_punctuation_after_facebook_url(self):
        raw_url = "https://www.facebook.com/watch/?v=12345,"
        target_url = "https://www.facebook.com/watch/?v=12345"
        content = f"這篇也太扯了 {raw_url} 真的"

        commentary = extract_message_commentary(
            content,
            target_url=target_url,
            url_pattern=FACEBOOK_URL_RE,
            sanitize_url=lambda raw: raw.rstrip(").,>|"),
        )

        self.assertEqual(commentary, "這篇也太扯了, 真的")

    def test_extract_message_commentary_drops_threads_tracking_query_suffix(self):
        target_url = "https://www.threads.com/@jun._.10.05/post/DW7pi5CE7vI"
        raw_url = f"{target_url}?xmt=AQF0_f6uuagLpW597A47oru048sXb8KjRbu01PZB2rGdig"
        content = f"這篇超荒謬 {raw_url} 快看"

        commentary = extract_message_commentary(
            content,
            target_url=target_url,
            url_pattern=THREADS_URL_RE,
            sanitize_url=sanitize_threads_url,
        )

        self.assertEqual(commentary, "這篇超荒謬 快看")


if __name__ == "__main__":
    unittest.main()
