import unittest

from discord_bot.threads_fetch import _parse_html


class ParseThreadsHtmlTests(unittest.TestCase):
    def test_text_only_post_ignores_meta_image_fallback(self):
        html = """
        <html><head>
          <meta property="og:description" content="純文字貼文" />
          <meta property="og:image" content="https://cdn.example.com/avatar.jpg" />
          <meta property="og:image:width" content="1080" />
          <meta property="og:image:height" content="1080" />
        </head></html>
        """

        post = _parse_html(
            "https://www.threads.net/@tester/post/ABC123",
            html,
        )

        self.assertEqual(post.text, "純文字貼文")
        self.assertEqual(post.media, [])

    def test_video_post_keeps_meta_thumbnail_and_video(self):
        html = """
        <html><head>
          <meta property="og:description" content="影片貼文" />
          <meta property="og:image" content="https://cdn.example.com/video-thumb.jpg" />
          <meta property="og:video" content="https://cdn.example.com/video.mp4" />
        </head></html>
        """

        post = _parse_html(
            "https://www.threads.net/@tester/post/VID123",
            html,
        )

        self.assertEqual(post.text, "影片貼文")
        self.assertEqual([media.type for media in post.media], ["image", "video"])
        self.assertEqual(post.media[0].url, "https://cdn.example.com/video-thumb.jpg")
        self.assertEqual(post.media[1].url, "https://cdn.example.com/video.mp4")


if __name__ == "__main__":
    unittest.main()
