"""Fictitious video IDs only; parsing never requests YouTube."""
import unittest

from discord_bot.features.social_preview.duplicate_identity import post_identity
from discord_bot.features.social_preview.youtube_links import extract_first_youtube_url, youtube_post_identity

VIDEO = "DemoVid0001"
URL = f"https://youtu.be/{VIDEO}"


class YouTubeIdentityTests(unittest.TestCase):
    def test_video_aliases_and_tracking_normalize_to_short_url(self):
        for raw in (URL, URL + "?si=example&t=42#part", URL + "/?list=example&index=3",
                    f"http://www.youtu.be/{VIDEO}", f"https://youtube.com/watch?v={VIDEO}",
                    f"https://www.youtube.com/watch?si=example&v={VIDEO}&t=42s&list=example",
                    f"https://m.youtube.com/watch?v={VIDEO}", f"https://music.youtube.com/watch?v={VIDEO}",
                    f"https://www.youtube.com/shorts/{VIDEO}?si=example",
                    f"https://youtube.com/live/{VIDEO}/?feature=share",
                    f"https://youtube.com/embed/{VIDEO}?start=42"):
            with self.subTest(raw=raw):
                self.assertEqual(post_identity("youtube", raw), (VIDEO, URL))

    def test_rejects_untrusted_ambiguous_and_non_video_urls(self):
        for raw in (None, "", "https://youtube.com", "https://youtube.com/@example",
                    "https://youtube.com/playlist?list=example", "https://youtube.com/clip/ExampleClip",
                    "https://youtube.com/results?search_query=example", "https://youtube.com/redirect?q=" + URL,
                    "https://example.invalid/?next=" + URL, f"https://youtube.com.evil.invalid/watch?v={VIDEO}",
                    f"https://user@youtu.be/{VIDEO}", f"https://youtu.be:invalid/{VIDEO}",
                    f"https://youtu.be:444/{VIDEO}", f"ftp://youtu.be/{VIDEO}",
                    f"https://youtu.be/{VIDEO}/extra", URL + "extra", URL[:-1], URL + ";extra",
                    "https://youtube.com/watch?v=", f"https://youtube.com/watch?v={VIDEO}&v=OtherVid002",
                    f"https://youtube.com/watch?v={VIDEO}&v=", f"https://youtube.com/watch?v={VIDEO}&" + "x=1&" * 70):
            with self.subTest(raw=raw):
                self.assertIsNone(youtube_post_identity(raw))

    def test_markdown_spoilers_punctuation_and_first_valid_video(self):
        for content in (f"comment {URL}?si=example", f"<{URL}>", f"||{URL}?t=42||",
                        f"[影片]({URL}?si=example)", f"({URL})", URL + "。",
                        f"https://youtube.com/@example {URL} https://youtu.be/OtherVid002"):
            with self.subTest(content=content):
                self.assertEqual(extract_first_youtube_url(content), URL)
        self.assertIsNone(extract_first_youtube_url("https://example.invalid/?next=" + URL))

    def test_case_sensitive_keys_and_platform_routing(self):
        self.assertNotEqual(youtube_post_identity(URL)[0], youtube_post_identity(URL.lower())[0])
        self.assertIsNone(post_identity("x", URL))
        self.assertIsNone(post_identity("threads", URL))
