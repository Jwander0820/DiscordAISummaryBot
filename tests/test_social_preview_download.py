import unittest
from unittest.mock import AsyncMock, patch

from discord_bot.features.social_preview.download import (
    download_bytes_limited,
    is_trusted_media_url,
)


TRUSTED_IMAGE_URL = "https://scontent.cdninstagram.com/v/test/post.jpg"


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _chunk_size):
        for chunk in self._chunks:
            yield chunk


class FakeTransport:
    def __init__(self, peer_address="1.1.1.1"):
        self.peer_address = peer_address

    def get_extra_info(self, name):
        if name == "peername":
            return (self.peer_address, 443)
        return None


class FakeResponse:
    def __init__(self, *, chunks=(), status=200, headers=None, peer_address="1.1.1.1"):
        self.status = status
        self.headers = {"Content-Type": "image/jpeg", **(headers or {})}
        self.content = FakeContent(chunks)
        self.connection = type("Connection", (), {"transport": FakeTransport(peer_address)})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class SocialPreviewDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def _download(self, session, url=TRUSTED_IMAGE_URL, **kwargs):
        with patch(
            "discord_bot.features.social_preview.download._resolve_public_addresses",
            new=AsyncMock(return_value={"1.1.1.1"}),
        ):
            return await download_bytes_limited(session, url, max_bytes=6, **kwargs)

    def test_media_url_allowlist_accepts_meta_media_hosts_and_instagram_media_endpoint(self):
        self.assertTrue(is_trusted_media_url(TRUSTED_IMAGE_URL))
        self.assertTrue(is_trusted_media_url("https://video.xx.fbcdn.net/video.mp4"))
        self.assertTrue(is_trusted_media_url("https://www.instagram.com/reel/ABC/media/?size=l"))

    def test_media_url_allowlist_rejects_http_credentials_and_untrusted_hosts(self):
        self.assertFalse(is_trusted_media_url("http://scontent.cdninstagram.com/post.jpg"))
        self.assertFalse(is_trusted_media_url("https://user@scontent.cdninstagram.com/post.jpg"))
        self.assertFalse(is_trusted_media_url("https://cdn.example.com/post.jpg"))
        self.assertFalse(is_trusted_media_url("https://www.instagram.com/accounts/login/"))

    async def test_download_returns_payload_within_limit(self):
        session = FakeSession(FakeResponse(chunks=[b"abc", b"def"]))

        payload = await self._download(session)

        self.assertEqual(payload, b"abcdef")
        self.assertFalse(session.requests[0][1]["allow_redirects"])

    async def test_download_rejects_untrusted_host_before_request(self):
        session = FakeSession(FakeResponse(chunks=[b"unused"]))

        payload = await self._download(session, url="https://example.com/post.jpg")

        self.assertIsNone(payload)
        self.assertEqual(session.requests, [])

    async def test_download_rejects_private_dns_resolution_before_request(self):
        session = FakeSession(FakeResponse(chunks=[b"unused"]))
        with patch(
            "discord_bot.features.social_preview.download._resolve_public_addresses",
            new=AsyncMock(side_effect=ValueError("private address")),
        ):
            payload = await download_bytes_limited(session, TRUSTED_IMAGE_URL, max_bytes=6)

        self.assertIsNone(payload)
        self.assertEqual(session.requests, [])

    async def test_download_revalidates_every_redirect(self):
        final_url = "https://scontent.xx.fbcdn.net/v/final.jpg"
        session = FakeSession(
            FakeResponse(status=302, headers={"Location": final_url}),
            FakeResponse(chunks=[b"image"]),
        )

        payload = await self._download(session)

        self.assertEqual(payload, b"image")
        self.assertEqual([request[0] for request in session.requests], [TRUSTED_IMAGE_URL, final_url])
        self.assertTrue(all(not request[1]["allow_redirects"] for request in session.requests))

    async def test_download_rejects_redirect_to_untrusted_host(self):
        session = FakeSession(
            FakeResponse(status=302, headers={"Location": "https://example.com/login"}),
            FakeResponse(chunks=[b"unused"]),
        )

        payload = await self._download(session)

        self.assertIsNone(payload)
        self.assertEqual(len(session.requests), 1)

    async def test_download_rejects_non_public_connected_peer(self):
        session = FakeSession(FakeResponse(chunks=[b"unused"], peer_address="127.0.0.1"))

        payload = await self._download(session)

        self.assertIsNone(payload)

    async def test_download_rejects_html_and_wrong_media_mime(self):
        html_session = FakeSession(
            FakeResponse(chunks=[b"<html>"], headers={"Content-Type": "text/html"})
        )
        video_session = FakeSession(
            FakeResponse(chunks=[b"image"], headers={"Content-Type": "image/jpeg"})
        )

        html_payload = await self._download(html_session)
        video_payload = await self._download(video_session, media_type="video")

        self.assertIsNone(html_payload)
        self.assertIsNone(video_payload)

    async def test_download_rejects_large_content_length_before_streaming(self):
        session = FakeSession(
            FakeResponse(chunks=[b"unused"], headers={"Content-Length": "7"})
        )

        payload = await self._download(session)

        self.assertIsNone(payload)

    async def test_download_stops_when_stream_exceeds_limit(self):
        session = FakeSession(FakeResponse(chunks=[b"abc", b"defg"]))

        payload = await self._download(session)

        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
