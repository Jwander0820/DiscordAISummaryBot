"""All IDs/accounts here are fictitious fixtures; no X requests are made."""
import unittest

from discord_bot.features.social_preview.duplicate_identity import post_identity
from discord_bot.features.social_preview.x_links import extract_first_x_url, x_post_identity

STATUS_ID = "1234567890123456789"
CANONICAL = f"https://x.com/example_user/status/{STATUS_ID}"


class XIdentityTests(unittest.TestCase):
    def test_tracking_domain_username_and_media_aliases_share_status_identity(self):
        for raw in (
            CANONICAL, CANONICAL + "?s=46", CANONICAL + "?utm_source=test#part",
            CANONICAL + "/photo/1", CANONICAL + "/video/2/?t=test",
            f"https://www.x.com/Example_User/status/{STATUS_ID}",
            f"https://mobile.x.com/renamed_user/status/{STATUS_ID}",
            f"http://twitter.com/example_user/status/{STATUS_ID}?ref_src=test",
            f"https://www.twitter.com/example_user/status/{STATUS_ID}",
            f"https://mobile.twitter.com/example_user/status/{STATUS_ID}",
            f"https://x.com/i/status/{STATUS_ID}", f"https://x.com/i/web/status/{STATUS_ID}",
        ):
            with self.subTest(raw=raw):
                identity = post_identity("x", raw)
                self.assertEqual(identity[0], STATUS_ID)
                self.assertTrue(identity[1].startswith("https://x.com/"))
                self.assertNotIn("?", identity[1])
                self.assertNotIn("#", identity[1])
                self.assertNotIn("/photo/", identity[1])
                self.assertNotIn("/video/", identity[1])
        self.assertEqual(x_post_identity(CANONICAL + "?s=46"), (STATUS_ID, CANONICAL))

    def test_untrusted_or_non_status_urls_are_not_recorded(self):
        for raw in (
            "https://x.com/example_user", "https://x.com/search?q=example", "https://x.com/intent/post",
            f"https://x.com.evil.invalid/example_user/status/{STATUS_ID}",
            f"https://example.invalid/x.com/example_user/status/{STATUS_ID}",
            f"https://example.invalid/?next={CANONICAL}",
            f"https://user@x.com/example_user/status/{STATUS_ID}",
            f"https://x.com:4430/example_user/status/{STATUS_ID}",
            f"https://x.com:invalid/example_user/status/{STATUS_ID}",
            "https://x.com/example_user/status/0", "https://x.com/example_user/status/0123",
            "https://x.com/example_user/status/not-an-id", CANONICAL + "extra",
            f"https://x.com/example_user/status/{STATUS_ID}/likes",
            f"https://fxtwitter.com/example_user/status/{STATUS_ID}", "https://t.co/Example",
            f"ftp://x.com/example_user/status/{STATUS_ID}", None,
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(x_post_identity(raw))

    def test_extracts_from_discord_markdown_spoilers_and_punctuation(self):
        for content in (
            f"comment {CANONICAL}?s=46", f"<{CANONICAL}?s=46>", f"||{CANONICAL}?s=46||",
            f"[example]({CANONICAL}?s=46)", f"({CANONICAL})", f"{CANONICAL}。",
            f"https://example.invalid unrelated {CANONICAL}",
        ):
            with self.subTest(content=content):
                self.assertEqual(extract_first_x_url(content), CANONICAL)

    def test_only_first_valid_status_in_a_source_message(self):
        self.assertEqual(extract_first_x_url(f"https://x.com/example_user {CANONICAL} {CANONICAL}?s=46"), CANONICAL)
        self.assertEqual(extract_first_x_url(CANONICAL + " https://x.com/another/status/2234567890123456789"), CANONICAL)
        self.assertIsNone(extract_first_x_url(f"https://example.invalid/?next={CANONICAL}"))
        self.assertIsNone(extract_first_x_url("https://x.com/example_user"))

    def test_platform_key_remains_separate_from_other_social_identities(self):
        self.assertIsNone(post_identity("threads", CANONICAL))
        other = f"https://x.com/example_user/status/2234567890123456789"
        self.assertNotEqual(x_post_identity(CANONICAL)[0], x_post_identity(other)[0])
