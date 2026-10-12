from pathlib import Path
import json
import unittest

from discord_bot.features.social_preview import threads_fetch as tf
from discord_bot.features.social_preview.threads_cookie import _parse_cookie_html


URL = "https://www.threads.com/@demo_user/post/Image001"
FIXTURE = (Path(__file__).parent / "fixtures" / "threads_linked_instagram_images.html").read_text(encoding="utf-8")
IMAGES = ["https://scontent.cdninstagram.com/first-large.jpg", "https://scontent.cdninstagram.com/second.jpg"]


def image_node(url=IMAGES[0], width=1080, height=1350):
    return {"image_versions2": {"candidates": [{"url": url, "width": width, "height": height}]}}


def payload_html(node):
    return '<script type="application/json">' + json.dumps(node) + '</script>'


def post_with_linked(linked, **extra):
    return {"code": "Image001", "caption": {"text": "正文"}, "image_versions2": {"candidates": []},
            "text_post_app_info": {"linked_inline_media": linked}, **extra}


class ThreadsLinkedImagesTests(unittest.TestCase):
    def test_anonymous_linked_instagram_carousel_keeps_images_in_post_order(self):
        post = tf._parse_html(URL, FIXTURE)
        self.assertEqual(post.text, "Threads 轉發圖片的正文")
        self.assertEqual([item.url for item in post.media if item.type == "image"], IMAGES)

    def test_cookie_linked_instagram_carousel_uses_same_attached_images(self):
        post = _parse_cookie_html(URL, FIXTURE)
        self.assertEqual(post.text, "Threads 轉發圖片的正文")
        self.assertEqual([item.url for item in post.media if item.type == "image"], IMAGES)

    def test_single_linked_image_and_native_carousel_are_supported(self):
        for node, expected in ((post_with_linked(image_node()), IMAGES[:1]),
                               (post_with_linked(None, carousel_media=[image_node(IMAGES[1]), image_node()]), IMAGES[::-1])):
            for parser in (tf._parse_html, _parse_cookie_html):
                with self.subTest(parser=parser.__name__, node=node):
                    self.assertEqual([item.url for item in parser(URL, payload_html(node)).media if item.type == "image"], expected)

    def test_other_posts_quotes_replies_recommendations_and_profile_are_not_media(self):
        for edge in ("quoted_post", "reply_to", "parent_post", "repost", "recommendations", "related_posts", "user"):
            node = post_with_linked(None)
            node[edge] = {"text_post_app_info": {"linked_inline_media": image_node()}, **image_node()}
            for parser in (tf._parse_html, _parse_cookie_html):
                with self.subTest(edge=edge, parser=parser.__name__):
                    self.assertFalse(parser(URL, payload_html(node)).media)
        for parser in (tf._parse_html, _parse_cookie_html):
            node = post_with_linked(image_node(), code="Other001")
            self.assertFalse(parser(URL, payload_html(node)).media)

    def test_wrong_domain_and_avatar_are_not_selected_over_valid_image(self):
        node = image_node()
        node["image_versions2"]["candidates"][:0] = [
            {"url": "https://example.com/untrusted.jpg", "width": 9999, "height": 9999},
            {"url": "https://scontent.cdninstagram.com/avatar.jpg", "width": 150, "height": 150},
        ]
        for parser in (tf._parse_html, _parse_cookie_html):
            with self.subTest(parser=parser.__name__):
                result = parser(URL, payload_html(post_with_linked(node)))
                self.assertEqual([item.url for item in result.media], IMAGES[:1])

    def test_video_attachment_keeps_video_priority_without_its_poster_image(self):
        video = "https://scontent.cdninstagram.com/attached.mp4"
        node = {**image_node(), "video_versions": [{"url": video, "width": 1080, "height": 1920}]}
        for parser in (tf._parse_html, _parse_cookie_html):
            with self.subTest(parser=parser.__name__):
                post = parser(URL, payload_html(post_with_linked(node)))
                self.assertEqual([(item.type, item.url) for item in post.media], [("video", video)])

    def test_explicitly_private_linked_media_is_not_exported(self):
        linked = {"user": {"is_private": True}, "carousel_media": [image_node()]}
        for parser in (tf._parse_html, _parse_cookie_html):
            with self.subTest(parser=parser.__name__):
                self.assertFalse(parser(URL, payload_html(post_with_linked(linked))).media)


if __name__ == "__main__":
    unittest.main()
