import unittest
from unittest.mock import patch

from discord_bot.features.social_preview.threads_fetch import (
    ThreadsMedia,
    ThreadsPost,
    _best_src_from_srcset,
    _extract_dom_text,
    _is_probable_profile_image,
    _video_mime_from_url,
    fetch_threads_post,
)


class FakeNode:
    def __init__(self, name="span", attrs=None, children=None):
        self.name = name
        self.attrs = attrs or {}
        self.parent = None
        self._children = []
        for child in children or []:
            self.add_child(child)

    def add_child(self, child):
        self._children.append(child)
        if isinstance(child, FakeNode):
            child.parent = self

    def get(self, key, default=None):
        return self.attrs.get(key, default)

    @property
    def parents(self):
        current = self.parent
        while current is not None:
            yield current
            current = current.parent

    @property
    def children(self):
        return iter(self._children)

    def get_text(self, sep=" ", strip=False):
        parts = []

        def walk(node):
            for child in node._children:
                if isinstance(child, FakeNode):
                    if child.name == "br":
                        parts.append("\n")
                    else:
                        walk(child)
                else:
                    parts.append(str(child))

        walk(self)
        text = "".join(parts)
        if strip:
            text = " ".join(text.split())
        return text


class FakeSoup:
    def __init__(self, nodes):
        self.nodes = nodes

    def select(self, selector):
        if selector != "span[dir='auto']":
            return []
        return self.nodes


class ThreadsFetchTests(unittest.TestCase):
    def test_best_src_from_srcset_prefers_largest_width(self):
        srcset = ",".join(
            [
                "https://img.example.com/s320.jpg 320w",
                "https://img.example.com/s640.jpg 640w",
                "https://img.example.com/s1080.jpg 1080w",
            ]
        )

        self.assertEqual(
            _best_src_from_srcset(srcset),
            "https://img.example.com/s1080.jpg",
        )

    def test_is_probable_profile_image_detects_threads_avatar_cdn(self):
        item = ThreadsMedia(
            "image",
            "https://scontent.cdninstagram.com/v/t51.82787-19/12345_n.jpg?profile_pic=1",
        )

        self.assertTrue(_is_probable_profile_image(item))

    def test_video_mime_from_url_detects_mp4_with_querystring(self):
        self.assertEqual(
            _video_mime_from_url("https://cdn.example.com/path/video.mp4?token=abc"),
            "video/mp4",
        )

    def test_extract_dom_text_keeps_multiple_lines(self):
        first = FakeNode("span", {"dir": "auto"}, ["第一段"])
        second = FakeNode("span", {"dir": "auto"}, ["第二段"])
        soup = FakeSoup([first, second])
        post = ThreadsPost(url="https://www.threads.com/@demo/post/abc", author_username="demo")

        found = _extract_dom_text(post, soup)

        self.assertTrue(found)
        self.assertEqual(post.text, "第一段\n第二段")

    def test_extract_dom_text_marks_spoiler_ranges(self):
        spoiler = FakeNode("span", {"aria-label": "spoiler"}, ["雷文"])
        line = FakeNode("span", {"dir": "auto"}, ["前半段 ", spoiler, " 後半段"])
        soup = FakeSoup([line])
        post = ThreadsPost(url="https://www.threads.com/@demo/post/abc", author_username="demo")

        found = _extract_dom_text(post, soup)

        self.assertTrue(found)
        self.assertEqual(post.text, "前半段 ||雷文|| 後半段")

    def test_extract_dom_text_skips_inline_translate_control(self):
        translate_button = FakeNode("span", {"role": "button"}, ["Translate"])
        line = FakeNode("span", {"dir": "auto"}, ["做人不要那麼急拔 ", translate_button])
        soup = FakeSoup([line])
        post = ThreadsPost(url="https://www.threads.com/@demo/post/abc", author_username="demo")

        found = _extract_dom_text(post, soup)

        self.assertTrue(found)
        self.assertEqual(post.text, "做人不要那麼急拔")

    def test_extract_dom_text_skips_footer_legal_lines(self):
        content = FakeNode("span", {"dir": "auto"}, ["揠苗助長的故事告訴我們什麼？"])
        footer_1 = FakeNode("span", {"dir": "auto"}, ["© 2026"])
        footer_2 = FakeNode("span", {"dir": "auto"}, ["Threads Terms"])
        footer_3 = FakeNode("span", {"dir": "auto"}, ["Privacy Policy"])
        footer_4 = FakeNode("span", {"dir": "auto"}, ["Cookies Policy"])
        soup = FakeSoup([content, footer_1, footer_2, footer_3, footer_4])
        post = ThreadsPost(url="https://www.threads.com/@demo/post/abc", author_username="demo")

        found = _extract_dom_text(post, soup)

        self.assertTrue(found)
        self.assertEqual(post.text, "揠苗助長的故事告訴我們什麼？")

    @patch("discord_bot.features.social_preview.threads_fetch._try_oembed_fill")
    @patch("discord_bot.features.social_preview.threads_fetch._try_requests")
    def test_fetch_threads_post_clears_fallback_media(
        self,
        mock_try_requests,
        mock_try_oembed_fill,
    ):
        url = "https://www.threads.com/@aa6301102/post/DWS_66xE__F"
        mock_try_requests.return_value = ThreadsPost(
            url=url,
            text="好期待啊啊",
            media=[
                ThreadsMedia(
                    "image",
                    "https://scontent.cdninstagram.com/v/t51.82787-19/12345_n.jpg",
                    150,
                    150,
                )
            ],
        )

        post = fetch_threads_post(url)

        mock_try_oembed_fill.assert_not_called()
        self.assertEqual(post.media, [])
        self.assertEqual(post.text, "好期待啊啊")

    @patch("discord_bot.features.social_preview.threads_fetch._try_oembed_fill")
    @patch("discord_bot.features.social_preview.threads_fetch._try_requests")
    def test_fetch_threads_post_calls_oembed_without_thumbnail_for_text_only_post(
        self,
        mock_try_requests,
        mock_try_oembed_fill,
    ):
        url = "https://www.threads.com/@andy__meme/post/DWTm_hcFNwi"
        mock_try_requests.return_value = ThreadsPost(url=url, text="這篇只有文字")

        post = fetch_threads_post(url)

        mock_try_oembed_fill.assert_called_once_with(mock_try_requests.return_value, allow_thumbnail=False)
        self.assertEqual(post.media, [])
        self.assertEqual(post.text, "這篇只有文字")

    @patch("discord_bot.features.social_preview.threads_fetch._try_oembed_fill")
    @patch("discord_bot.features.social_preview.threads_fetch._try_requests")
    def test_fetch_threads_post_clears_fallback_thumbnail_added_by_oembed(
        self,
        mock_try_requests,
        mock_try_oembed_fill,
    ):
        url = "https://www.threads.com/@andy__meme/post/DWTm_hcFNwi"
        mock_try_requests.return_value = ThreadsPost(url=url)

        def fill_with_avatar(post, *, allow_thumbnail):
            self.assertTrue(allow_thumbnail)
            post.media.append(
                ThreadsMedia(
                    "image",
                    "https://scontent.cdninstagram.com/v/t51.82787-19/12345_n.jpg?profile_pic=1",
                    150,
                    150,
                )
            )

        mock_try_oembed_fill.side_effect = fill_with_avatar

        post = fetch_threads_post(url)

        mock_try_oembed_fill.assert_called_once()
        self.assertEqual(post.media, [])


if __name__ == "__main__":
    unittest.main()
