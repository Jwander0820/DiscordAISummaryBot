import unittest
from unittest.mock import patch

from discord_bot.features.social_preview.instagram_fetch import (
    InstagramMedia,
    _is_probable_profile_image,
    _native_embed_proxy_url,
    fetch_instagram_post,
    instagram_media_id_from_shortcode,
    normalize_instagram_url,
)


class InstagramFetchTests(unittest.TestCase):
    def test_normalize_instagram_url_removes_tracking(self):
        url = "https://instagram.com/p/ABC123/?igsh=demo&utm_source=copy#fragment"

        self.assertEqual(
            normalize_instagram_url(url),
            "https://www.instagram.com/p/ABC123",
        )

    def test_reels_url_uses_reels_proxy_path(self):
        self.assertEqual(
            _native_embed_proxy_url("https://www.instagram.com/reels/ABC123/"),
            "https://www.kkinstagram.com/reels/ABC123/",
        )

    @patch("discord_bot.features.social_preview.instagram_direct.fetch_instagram_direct")
    def test_fetch_instagram_post_uses_first_party_fetcher(self, fetch):
        self.assertIs(fetch_instagram_post("https://www.instagram.com/reel/ABC123/?mdxt=tracking"), fetch.return_value)
        fetch.assert_called_once_with("https://www.instagram.com/reel/ABC123", trace_id=None)
        fetch.reset_mock()
        fetch_instagram_post("https://www.instagram.com/reel/ABC123/?mdxt=tracking", trace_id="synthetictrace")
        fetch.assert_called_once_with("https://www.instagram.com/reel/ABC123", trace_id="synthetictrace")

    def test_shortcode_can_be_converted_to_media_id(self):
        self.assertEqual(instagram_media_id_from_shortcode("ABC"), "66")

    def test_is_probable_profile_image_detects_profile_marker(self):
        media = InstagramMedia("image", "https://cdn.example.com/avatar.jpg?profile_pic=1")

        self.assertTrue(_is_probable_profile_image(media))


if __name__ == "__main__":
    unittest.main()
