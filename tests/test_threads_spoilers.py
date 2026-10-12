import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from discord_bot.features.social_preview import threads_fetch as tf
from discord_bot.features.social_preview.threads_cookie import _parse_cookie_html
from discord_bot.features.social_preview.text import TextSpan

FIXTURES = Path(__file__).parent / "fixtures"
VIDEO_HTML = (FIXTURES / "threads_native_video_spoiler.html").read_text(encoding="utf-8")
TEXT_HTML = (FIXTURES / "threads_native_text_spoiler.html").read_text(encoding="utf-8")
VIDEO_URL = "https://www.threads.com/@demo/post/NativeVideo001"
TEXT_URL = "https://www.threads.com/@demo/post/NativeText001"
MEDIA_URL = "https://scontent.cdninstagram.com/spoiler.mp4"


def structured(payload):
    return '<script type="application/json">' + json.dumps(payload) + '</script>'


def fixture_node(page):
    return json.loads(BeautifulSoup(page, "html.parser").find("script").string)["data"]["post"]


class ThreadsNativeSpoilerTests(unittest.TestCase):
    def test_structured_video_spoiler_keeps_caption_public_without_extra_requests(self):
        post = tf._parse_html(VIDEO_URL, VIDEO_HTML)
        self.assertEqual(post.text, "公開影片說明")
        self.assertIsNone(post.text_spans)
        self.assertEqual([(m.type, m.url, m.spoiler) for m in post.media], [("video", MEDIA_URL, True)])
        with patch.object(tf, "_try_requests", return_value=post), patch.object(tf, "_try_embed_fill") as embed, patch.object(tf, "_try_oembed_fill") as oembed:
            self.assertIs(tf.fetch_threads_post(VIDEO_URL), post)
        embed.assert_not_called()
        oembed.assert_not_called()

    def test_structured_text_fragments_override_flat_metadata_without_masking_image(self):
        post = tf._parse_html(TEXT_URL, TEXT_HTML)
        self.assertEqual(post.text, "公開第一行\n隱藏一\n公開第二行 😀\n隱藏二")
        self.assertEqual(post.text_spans, [TextSpan("公開第一行\n"), TextSpan("隱藏一", True),
                                           TextSpan("\n公開第二行 😀\n"), TextSpan("隱藏二", True)])
        self.assertEqual([m.spoiler for m in post.media], [False])

    def test_structured_spoilers_work_for_text_only_and_keep_literal_markdown(self):
        node = fixture_node(TEXT_HTML)
        node.pop("image_versions2")
        node["caption"]["text"] = "公開 `code` ||字面符號||"
        node["text_post_app_info"]["text_fragments"]["fragments"] = [
            {"plaintext": "公開 "}, {"plaintext": "`code` ||字面符號||", "styling_info": {"is_spoiler": True}}]
        post = tf._parse_html(TEXT_URL, structured(node))
        self.assertEqual(post.text, node["caption"]["text"])
        self.assertEqual(post.text_spans, [TextSpan("公開 "), TextSpan("`code` ||字面符號||", True)])
        self.assertEqual(post.media, [])

    def test_incomplete_native_fragments_mask_caption_instead_of_exposing_it(self):
        node = fixture_node(TEXT_HTML)
        node["text_post_app_info"]["text_fragments"]["fragments"].append({"fragment_type": "mention"})
        post = tf._parse_html(TEXT_URL, structured(node))
        self.assertEqual(post.text_spans, [TextSpan(node["caption"]["text"], True)])

    def test_only_target_and_explicit_attachments_inherit_media_flag(self):
        root = {"code": "NativeVideo001", "text_post_app_info": {"is_spoiler_media": False}, "carousel_media": [
            {"code": "ImageChild", "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/a.jpg"}]}},
            {"code": "VideoChild", "text_post_app_info": {"is_spoiler_media": True}, "video_url": MEDIA_URL},
        ]}
        unrelated = fixture_node(TEXT_HTML)
        unrelated["code"] = "OtherPost"
        root["quoted_post"] = unrelated
        root["direct_replies"] = [dict(root, code="OtherVideo")]
        page = structured({"posts": [unrelated, root]})
        post = tf._parse_html(VIDEO_URL, page)
        self.assertEqual([m.spoiler for m in post.media], [False, True])
        self.assertIsNone(post.text_spans)
        root["text_post_app_info"]["is_spoiler_media"] = True
        self.assertTrue(all(m.spoiler for m in tf._parse_html(VIDEO_URL, structured(root)).media))

    def test_duplicate_media_keeps_known_spoiler_and_ignores_truthy_strings(self):
        root = {"code": "NativeVideo001", "carousel_media": [
            {"video_url": MEDIA_URL, "is_spoiler_media": "false"},
            {"video_url": MEDIA_URL, "is_spoiler_media": True},
        ]}
        post = tf._parse_html(VIDEO_URL, structured(root))
        self.assertEqual([(m.url, m.spoiler) for m in post.media], [(MEDIA_URL, True)])
        root["carousel_media"] = root["carousel_media"][:1]
        self.assertFalse(tf._parse_html(VIDEO_URL, structured(root)).media[0].spoiler)

    def test_cookie_parser_preserves_native_metadata_without_network(self):
        for url, page in ((VIDEO_URL, VIDEO_HTML), (TEXT_URL, TEXT_HTML)):
            with self.subTest(url=url):
                post = _parse_cookie_html(url, page)
                self.assertIsNone(post.preview_error)
                self.assertEqual(post.media[0].spoiler, url == VIDEO_URL)
                if url == TEXT_URL:
                    self.assertEqual([s.spoiler for s in post.text_spans], [False, True, False, True])

    def test_embed_only_spoilers_are_bound_to_the_correct_caption_and_media(self):
        root = BeautifulSoup(VIDEO_HTML, "html.parser").select_one(".Embed")
        root.select_one(".SoloMediaContainer").append(BeautifulSoup(
            '<video src="https://scontent.cdninstagram.com/public.mp4"></video>', "html.parser"))
        wrong = str(root).replace("NativeVideo001", "OtherPost").replace("public.mp4", "other.mp4")
        post = tf._parse_html(VIDEO_URL, wrong + str(root))
        self.assertEqual([(m.url.rsplit('/', 1)[-1], m.spoiler) for m in post.media], [("spoiler.mp4", True), ("public.mp4", False)])
        self.assertEqual(post.text, "公開影片說明")

    def test_existing_embed_fallback_restores_native_ranges_over_flat_text(self):
        page = str(BeautifulSoup(TEXT_HTML, "html.parser").select_one(".Embed"))
        post = tf.ThreadsPost(url=TEXT_URL, text="沒有標記的舊正文", media=[tf.ThreadsMedia("image", "https://scontent.cdninstagram.com/public.jpg")])
        response = Mock(status_code=200)
        response.iter_content.return_value = [page.encode("utf-8")]
        get = Mock(return_value=response)
        with patch.dict("sys.modules", {"requests": SimpleNamespace(get=get)}):
            tf._try_embed_fill(post)
        self.assertEqual([s.spoiler for s in post.text_spans], [False, True, False, True])
        self.assertNotIn("舊正文", post.text)
        get.assert_called_once()
        response.close.assert_called_once()

    def test_dom_reveal_button_keeps_spoiler_text_but_drops_unrelated_controls(self):
        page = '<span dir="auto">公開<div role="button"><span data-text-fragment="spoiler"><div><span>隱藏</span></div></span></div>結尾<button>Translate</button></span>'
        post = tf._parse_html(TEXT_URL, page)
        self.assertEqual(post.text, "公開隱藏結尾")
        self.assertEqual(post.text_spans, [TextSpan("公開"), TextSpan("隱藏", True), TextSpan("結尾")])

    def test_dom_visibility_styles_and_other_post_markers_do_not_mask_public_text(self):
        page = '<span dir="auto" style="overflow: hidden; filter: none">公開內容</span>'
        post = tf._parse_html(TEXT_URL, page)
        self.assertEqual(post.text, "公開內容")
        self.assertIsNone(post.text_spans)
        other = TEXT_HTML.replace("NativeText001", "OtherPost")
        target = '<meta property="og:description" content="目標公開正文">'
        post = tf._parse_html(TEXT_URL, other.replace('<script type="application/ld+json">{"@type":"SocialMediaPosting","articleBody":"沒有防雷範圍的扁平舊正文"}</script>', '') + target)
        self.assertIsNone(post.text_spans)
        self.assertEqual(post.text, "目標公開正文")

    def test_dom_media_marker_survives_structured_media_precedence(self):
        node = fixture_node(VIDEO_HTML)
        node["text_post_app_info"]["is_spoiler_media"] = False
        embed = str(BeautifulSoup(VIDEO_HTML, "html.parser").select_one(".Embed"))
        post = tf._parse_html(VIDEO_URL, structured(node) + embed)
        self.assertEqual(len(post.media), 1)
        self.assertTrue(post.media[0].spoiler)

    def test_embed_image_mask_does_not_mask_other_images(self):
        root = BeautifulSoup(VIDEO_HTML, "html.parser").select_one(".Embed")
        video = root.select_one("video")
        video.replace_with(BeautifulSoup('<img src="https://scontent.cdninstagram.com/hidden.jpg">', "html.parser"))
        root.select_one(".SoloMediaContainer").append(BeautifulSoup('<img src="https://scontent.cdninstagram.com/public.jpg">', "html.parser"))
        post = tf._parse_html(VIDEO_URL, str(root))
        self.assertEqual([m.spoiler for m in post.media], [True, False])


if __name__ == "__main__":
    unittest.main()
