import unittest
from unittest.mock import patch

from discord_bot.features.social_preview.facebook_preview import _extract_og_data, _fetch_html_with_aiohttp


class FakeResponse:
    def __init__(self, url, html, *, status=200):
        self.url = url
        self._html = html
        self.status = status

    async def text(self, *, errors):
        return self._html

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **_kwargs):
        self.requested_urls.append(url)
        return next(self._responses)


class FacebookPreviewTests(unittest.TestCase):
    def test_empty_html_does_not_report_placeholder_as_fetched_metadata(self):
        title, description, image_urls, video_urls = _extract_og_data("<html><body></body></html>")

        self.assertEqual(title, "")
        self.assertEqual(description, "")
        self.assertEqual(image_urls, [])
        self.assertEqual(video_urls, [])

    def test_open_graph_title_counts_as_metadata(self):
        html = '<meta property="og:title" content="Real post title">'

        title, _, _, _ = _extract_og_data(html)

        self.assertEqual(title, "Real post title")


class FacebookPreviewAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_tries_next_candidate_when_first_page_has_no_metadata(self):
        first_url = "https://www.facebook.com/post/123"
        second_url = "https://m.facebook.com/post/123"
        session = FakeSession(
            [
                FakeResponse(first_url, "<html><body>login wall</body></html>"),
                FakeResponse(second_url, '<meta property="og:title" content="Real post title">'),
            ]
        )

        with (
            patch(
                "discord_bot.features.social_preview.facebook_preview._build_candidate_urls",
                return_value=[first_url, second_url],
            ),
            patch(
                "discord_bot.features.social_preview.facebook_preview.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            title, _, _, _, final_url, status = await _fetch_html_with_aiohttp(first_url)

        self.assertEqual(title, "Real post title")
        self.assertEqual(final_url, second_url)
        self.assertEqual(status, 200)
        self.assertEqual(session.requested_urls, [first_url, second_url])


if __name__ == "__main__":
    unittest.main()
