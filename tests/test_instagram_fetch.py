import unittest

from discord_bot.features.social_preview.instagram_fetch import (
    InstagramPost,
    InstagramMedia,
    _fallback_media_post,
    _is_probable_profile_image,
    _native_embed_proxy_url,
    _post_from_api_payload,
    fetch_instagram_post,
    instagram_media_id_from_shortcode,
    normalize_instagram_url,
    parse_instagram_html,
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

    def test_fetch_instagram_post_defaults_to_native_embed_without_network(self):
        post = fetch_instagram_post("https://www.instagram.com/reel/ABC123/")

        self.assertEqual(post.preview_level, "native_embed")
        self.assertEqual(post.native_embed_url, "https://www.kkinstagram.com/reels/ABC123/")
        self.assertEqual(post.media, [])

    def test_shortcode_can_be_converted_to_media_id(self):
        self.assertEqual(instagram_media_id_from_shortcode("ABC"), "66")

    def test_fallback_media_post_uses_instagram_media_endpoint(self):
        post = _fallback_media_post("https://www.instagram.com/reel/ABC123/")

        self.assertEqual(post.preview_level, "media_fallback")
        self.assertEqual(post.native_embed_url, "https://www.kkinstagram.com/reels/ABC123/")
        self.assertEqual(post.media[0].url, "https://www.instagram.com/reel/ABC123/media/?size=l")
        self.assertEqual(post.media[1].url, "https://www.instagram.com/p/ABC123/media/?size=l")

    def test_parse_open_graph_extracts_caption_author_and_media(self):
        html = """
        <html><head>
          <meta property="og:title" content="Demo User (@demo.user) on Instagram">
          <meta property="og:description" content='12 likes, 1 comments - demo.user on Instagram: "hello world"'>
          <meta property="og:image" content="https://cdn.example.com/post.jpg">
          <meta property="og:video" content="https://cdn.example.com/video.mp4">
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/p/ABC123/",
        )

        self.assertEqual(post.shortcode, "ABC123")
        self.assertEqual(post.author_username, "demo.user")
        self.assertEqual(post.text, "hello world")
        self.assertEqual([media.type for media in post.media], ["image", "video"])
        self.assertEqual(post.media[0].url, "https://cdn.example.com/post.jpg")

    def test_parse_open_graph_extracts_unquoted_instagram_caption(self):
        html = """
        <html><head>
          <meta property="og:title" content="Demo User (@demo.user) on Instagram">
          <meta property="og:description" content="12 likes, 1 comments - demo.user on Instagram: hello without quotes">
          <meta property="twitter:image" content="https://cdn.example.com/thumb.jpg">
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/reel/ABC123/",
        )

        self.assertEqual(post.text, "hello without quotes")
        self.assertEqual(post.media[0].url, "https://cdn.example.com/thumb.jpg")

    def test_parse_json_ld_extracts_social_post(self):
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "SocialMediaPosting",
            "author": {"@type": "Person", "name": "Demo User", "alternateName": "@demo"},
            "articleBody": "json caption",
            "image": {"url": "https://cdn.example.com/json.jpg"},
            "video": {"contentUrl": "https://cdn.example.com/json.mp4"}
          }
          </script>
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/reel/XYZ789/",
        )

        self.assertEqual(post.shortcode, "XYZ789")
        self.assertEqual(post.author_name, "Demo User")
        self.assertEqual(post.author_username, "demo")
        self.assertEqual(post.text, "json caption")
        self.assertEqual([media.url for media in post.media], [
            "https://cdn.example.com/json.jpg",
            "https://cdn.example.com/json.mp4",
        ])

    def test_parse_json_ld_video_object_uses_thumbnail_as_image(self):
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@type": "VideoObject",
            "name": "video caption",
            "thumbnailUrl": "https://cdn.example.com/video-thumb.jpg",
            "contentUrl": "https://cdn.example.com/video.mp4"
          }
          </script>
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/reel/ABC123/",
        )

        self.assertEqual(post.text, "video caption")
        self.assertEqual([media.type for media in post.media], ["image", "video"])
        self.assertEqual(post.media[0].url, "https://cdn.example.com/video-thumb.jpg")

    def test_parse_embedded_graphql_extracts_sidecar_resources(self):
        html = """
        <html><head>
          <script type="application/json">
          {
            "shortcode_media": {
              "owner": {"username": "demo", "full_name": "Demo User"},
              "edge_media_to_caption": {"edges": [{"node": {"text": "embedded caption"}}]},
              "edge_sidecar_to_children": {
                "edges": [
                  {
                    "node": {
                      "display_resources": [
                        {"src": "https://cdn.example.com/small.jpg", "config_width": 320, "config_height": 320},
                        {"src": "https://cdn.example.com/large.jpg", "config_width": 1080, "config_height": 1080}
                      ]
                    }
                  },
                  {
                    "node": {
                      "video_url": "https://cdn.example.com/sidecar.mp4",
                      "display_url": "https://cdn.example.com/thumb.jpg"
                    }
                  }
                ]
              }
            }
          }
          </script>
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/p/ABC123/",
        )

        self.assertEqual(post.author_username, "demo")
        self.assertEqual(post.author_name, "Demo User")
        self.assertEqual(post.text, "embedded caption")
        self.assertEqual([media.url for media in post.media], [
            "https://cdn.example.com/large.jpg",
            "https://cdn.example.com/thumb.jpg",
            "https://cdn.example.com/sidecar.mp4",
        ])

    def test_parse_shortcode_api_extracts_caption_author_carousel_and_video(self):
        fallback = InstagramPost(url="https://www.instagram.com/reel/ABC123", shortcode="ABC123", kind="reel")
        payload = {
            "items": [
                {
                    "caption": {"text": "api caption"},
                    "user": {"username": "demo", "full_name": "Demo User"},
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn.example.com/small.jpg", "width": 320, "height": 320},
                            {"url": "https://cdn.example.com/large.jpg", "width": 1080, "height": 1080},
                        ]
                    },
                    "video_versions": [
                        {"url": "https://cdn.example.com/reel.mp4", "width": 720, "height": 1280}
                    ],
                    "carousel_media": [
                        {
                            "image_versions2": {
                                "candidates": [
                                    {"url": "https://cdn.example.com/carousel.jpg", "width": 800, "height": 800}
                                ]
                            }
                        }
                    ],
                }
            ]
        }

        post = _post_from_api_payload(payload, fallback=fallback)

        self.assertEqual(post.author_username, "demo")
        self.assertEqual(post.author_name, "Demo User")
        self.assertEqual(post.text, "api caption")
        self.assertEqual([media.url for media in post.media], [
            "https://cdn.example.com/large.jpg",
            "https://cdn.example.com/reel.mp4",
            "https://cdn.example.com/carousel.jpg",
        ])

    def test_parse_shortcode_api_uses_thumbnail_when_video_has_no_image_versions(self):
        fallback = InstagramPost(url="https://www.instagram.com/reel/ABC123", shortcode="ABC123", kind="reel")
        payload = {
            "items": [
                {
                    "caption": "caption string",
                    "user": {"username": "demo"},
                    "thumbnail_url": "https://cdn.example.com/thumb.jpg",
                    "video_versions": [
                        {"url": "https://cdn.example.com/reel.mp4", "width": 720, "height": 1280}
                    ],
                }
            ]
        }

        post = _post_from_api_payload(payload, fallback=fallback)

        self.assertEqual(post.text, "caption string")
        self.assertEqual([media.type for media in post.media], ["image", "video"])
        self.assertEqual(post.media[0].url, "https://cdn.example.com/thumb.jpg")

    def test_profile_images_are_filtered(self):
        html = """
        <html><head>
          <meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.2885-19/avatar.jpg">
          <meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.29350-15/post.jpg">
        </head></html>
        """

        post = parse_instagram_html(
            html,
            final_url="https://www.instagram.com/p/ABC123/",
        )

        self.assertEqual([media.url for media in post.media], [
            "https://scontent.cdninstagram.com/v/t51.29350-15/post.jpg",
        ])

    def test_is_probable_profile_image_detects_profile_marker(self):
        media = InstagramMedia("image", "https://cdn.example.com/avatar.jpg?profile_pic=1")

        self.assertTrue(_is_probable_profile_image(media))


if __name__ == "__main__":
    unittest.main()
