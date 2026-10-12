import json
from pathlib import Path
import unittest

from unittest.mock import patch
from discord_bot.features.social_preview.facebook_fetch import (
    direct_facebook_mp4, extract_facebook_post, facebook_post_identity,
)


URL = "https://www.facebook.com/reel/1602403008023321/"
FIXTURE = (Path(__file__).parent / "fixtures" / "facebook_reel_video.html").read_text(encoding="utf-8")
MP4 = "https://video.xx.fbcdn.net/native.mp4?oh=synthetic&oe=synthetic"
IMAGE = "https://scontent.xx.fbcdn.net/photo.jpg"


def page(data):
    return '<script type="application/json">' + json.dumps(data) + '</script>'


class FacebookFetchTests(unittest.TestCase):
    def test_video_and_post_identifiers_are_bound_to_facebook_urls(self):
        for url, expected in ((URL, ("video", "1602403008023321")),
                              ("https://m.facebook.com/watch/?v=123&rdid=tracking", ("video", "123")),
                              ("https://www.facebook.com/user/videos/123/", ("video", "123")),
                              ("https://www.facebook.com/user/posts/pfbidTEST/", ("post", "pfbidTEST")),
                              ("https://www.facebook.com/permalink.php?story_fbid=123&id=456", ("post", "123"))):
            with self.subTest(url=url):
                self.assertEqual(facebook_post_identity(url), expected)
        for url in ("https://example.com/reel/123/", "https://facebook.com@example.com/reel/123/",
                    "https://www.facebook.com:8443/reel/123/", "https://www.facebook.com/share/v/token/"):
            self.assertIsNone(facebook_post_identity(url))

    def test_native_hd_preferred_with_sd_fallback_without_changing_signed_query(self):
        node = {"id": "1602403008023321", "videoDeliveryLegacyFields": {
            "browser_native_hd_url": MP4.replace("native", "hd"), "browser_native_sd_url": MP4.replace("native", "sd")}}
        self.assertEqual(extract_facebook_post(page(node), URL).video_urls, [MP4.replace("native", "hd")])
        node["videoDeliveryLegacyFields"]["browser_native_hd_url"] = "https://video.xx.fbcdn.net/dash.mpd"
        self.assertEqual(extract_facebook_post(page(node), URL).video_urls, [MP4.replace("native", "sd")])
        escaped = MP4.replace("&", "&amp;").replace("/", r"\/")
        self.assertEqual(direct_facebook_mp4(escaped), MP4)

    def test_progressive_urls_supported_but_manifests_are_not(self):
        node = {"id": "1602403008023321", "videoDeliveryResponseFragment": {"videoDeliveryResponseResult": {
            "progressive_urls": [
                {"progressive_url": MP4.replace("native", "sd"), "metadata": {"quality": "SD"}},
                {"progressive_url": MP4, "metadata": {"quality": "HD"}},
            ], "dash_manifest_urls": [{"manifest_url": "https://video.xx.fbcdn.net/video.mpd"}]
        }}}
        self.assertEqual(extract_facebook_post(page(node), URL).video_urls, [MP4])

    def test_only_first_attached_video_is_selected_in_post_order(self):
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        node = {"url": url, "attachments": [{"styles": {"attachment": {"media": {
            "__typename": "Video", "id": str(index), "browser_native_hd_url": MP4.replace("native", str(index))
        }}}} for index in (2, 1)]}
        self.assertEqual(extract_facebook_post(page(node), url).video_urls, [MP4.replace("native", "2")])

    def test_unbound_recommendations_and_quoted_videos_are_excluded(self):
        for node in (
            {"id": "other", "browser_native_hd_url": MP4},
            {"id": "1602403008023321", "recommendations": [{"browser_native_hd_url": MP4}]},
            {"id": "1602403008023321", "attached_story": {"video": {"browser_native_hd_url": MP4}}},
            {"id": "1602403008023321", "quoted_post": {"browser_native_hd_url": MP4}},
            {"url": URL, "video": {"id": "another-video", "browser_native_hd_url": MP4}},
        ):
            with self.subTest(node=node):
                self.assertEqual(extract_facebook_post(page(node), URL).video_urls, [])

    def test_invalid_media_urls_are_rejected(self):
        for url in ("blob:https://www.facebook.com/id", "https://video.xx.fbcdn.net/video.m3u8",
                    "https://video.xx.fbcdn.net/video.mpd", "https://example.com/video.mp4",
                    "https://fbcdn.net.example.com/video.mp4", "http://video.xx.fbcdn.net/video.mp4",
                    "https://127.0.0.1/video.mp4", "https://user:password@video.xx.fbcdn.net/video.mp4",
                    MP4 + "&bytestart=0&byteend=999", MP4 + "&range=0-999", MP4 + "x" * 8192):
            with self.subTest(url=url[:100]):
                self.assertIsNone(direct_facebook_mp4(url))

    def test_authenticated_content_requires_explicit_target_public_scope(self):
        base = {"id": "1602403008023321", "message": {"text": "private text"}, "browser_native_hd_url": MP4}
        for privacy in ({}, {"is_public": False}, {"is_private": True}, {"privacy_scope": {"label": "Friends"}},
                        {"privacy_scope": {"label": "Custom"}}, {"privacy_scope": {"label": "unrecognized locale"}}):
            with self.subTest(privacy=privacy):
                post = extract_facebook_post(page({**base, **privacy}), URL, require_public=True)
                self.assertFalse(post.has_content)
                self.assertNotIn("private text", repr(post))
        for privacy in ({"is_public": True}, {"privacy_scope": {"label": "Public"}}, {"privacy_scope": {"label": "所有人"}}):
            self.assertEqual(extract_facebook_post(page({**base, **privacy}), URL, require_public=True).video_urls, [MP4])

    def test_post_body_public_word_and_unbound_og_never_prove_privacy(self):
        body = '<meta property="og:description" content="account private data">' + page({
            "id": "1602403008023321", "message": {"text": "Public"}, "browser_native_hd_url": MP4
        })
        self.assertFalse(extract_facebook_post(body, URL, require_public=True).has_content)

    def test_known_nonpublic_scope_overrides_conflicting_public_flag(self):
        node = {"id": "1602403008023321", "is_public": True,
                "privacy_scope": {"label": "Friends"}, "browser_native_hd_url": MP4}
        self.assertFalse(extract_facebook_post(page(node), URL, require_public=True).has_content)

    def test_description_only_privacy_scope_is_strict_and_does_not_override_nonpublic_label(self):
        base = {"id": "1602403008023321", "browser_native_hd_url": MP4}
        for scope in ({"description": "Public"}, {"label": None, "description": "所有人"},
                      {"label": "", "description": "公開"}):
            with self.subTest(scope=scope):
                post = extract_facebook_post(page({**base, "privacy_scope": scope}), URL, require_public=True)
                self.assertEqual(post.video_urls, [MP4])
        for scope in ({"description": "Friends"}, {"description": "Custom"}, {"description": "not recognized"},
                      {"label": "Friends", "description": "Public"}, {"description": "Public is a word in this text"}):
            with self.subTest(scope=scope):
                self.assertFalse(extract_facebook_post(page({**base, "privacy_scope": scope}), URL, require_public=True).has_content)

    def test_nested_playback_does_not_bypass_parent_video_restrictions(self):
        for flags in ({"is_live_streaming": True}, {"has_audio": False}, {"is_drm": True}):
            with self.subTest(flags=flags):
                node = {"id": "1602403008023321", **flags, "creation_story": {
                    "short_form_video_context": {"playback_video": {
                        "id": "1602403008023321", "browser_native_hd_url": MP4, "image": {"uri": IMAGE},
                    }}}}
                post = extract_facebook_post(page(node), URL)
                self.assertEqual(post.video_urls, [])
                self.assertEqual(post.image_urls, [IMAGE])

    def test_post_sections_bind_permalink_privacy_and_attached_photo(self):
        url = "https://www.facebook.com/demo/posts/pfbidTEST"
        node = {"__typename": "Story", "id": "opaque-story-id", "comet_sections": {
            "content": {"story": {"url": url, "message": {"text": "target photo"},
                "attachments": [{"styles": {"attachment": {"media": {
                    "__typename": "Photo", "image": {"uri": IMAGE}
                }}}}]}},
            "context_layout": {"story": {"comet_sections": {"metadata": [
                {"story": {"privacy_scope": {"label": "Public"}}}
            ]}}},
        }}
        post = extract_facebook_post(page({"data": {"node": node}}), url, require_public=True)
        self.assertEqual(post.description, "target photo")
        self.assertEqual(post.image_urls, [IMAGE])
        self.assertTrue(post.public)

    def test_unknown_json_and_parse_budgets_fail_without_body_data(self):
        body = '<script type="application/json">not-json</script>'
        self.assertFalse(extract_facebook_post(body, URL).has_content)
        with patch("discord_bot.features.social_preview.facebook_fetch.MAX_FACEBOOK_PARSE_NODES", 2):
            post = extract_facebook_post(page({"irrelevant": [{}, {}, {}], "video": {"id": "1602403008023321", "browser_native_hd_url": MP4}}), URL)
        self.assertEqual(post.reason, "parse_budget_exhausted")
        with patch("discord_bot.features.social_preview.facebook_fetch.MAX_FACEBOOK_HTML_BYTES", 20):
            self.assertEqual(extract_facebook_post(FIXTURE, URL).reason, "response_too_large")

    def test_generic_player_certificate_does_not_hide_normal_mp4(self):
        body = FIXTURE.replace('"drm_info": null', '"drm_info": ' + json.dumps(json.dumps({
            "video_license_uri_map": {}, "graph_api_video_license_uri": None, "widevine_cert": "generic-player-certificate"
        })))
        post = extract_facebook_post(body, URL, require_public=True)
        self.assertEqual(len(post.video_urls), 1)
        self.assertTrue(post.public)

    def test_child_legacy_formats_do_not_bypass_parent_video_restrictions(self):
        for update in ({"is_live_streaming": True}, {"has_audio": False}, {"is_video_only": True},
                       {"drm_info": {"graph_api_video_license_uri": "https://example.com/license"}}):
            with self.subTest(update=update):
                video = {"id": "1602403008023321", "videoDeliveryLegacyFields": {
                    "id": "1602403008023321", "browser_native_hd_url": "https://video.xx.fbcdn.net/video.mp4"
                }, **update}
                self.assertEqual(extract_facebook_post(page(video), URL).video_urls, [])

    def test_later_payload_supplies_video_after_caption_only_payload(self):
        early = {"id": "1602403008023321", "creation_story": {
            "privacy_scope": {"label": "Public"}, "message": {"text": "earlier caption"}
        }}
        post = extract_facebook_post(page(early) + FIXTURE, URL)
        self.assertEqual(len(post.video_urls), 1)
        self.assertIn("target-hd.mp4", post.video_urls[0])


if __name__ == "__main__":
    unittest.main()
