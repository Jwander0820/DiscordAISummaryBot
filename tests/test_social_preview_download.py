import unittest

from discord_bot.features.social_preview.download import download_bytes_limited


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _chunk_size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, *, chunks=(), status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, _url, *, timeout):
        self.timeout = timeout
        return self.response


class SocialPreviewDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_returns_payload_within_limit(self):
        session = FakeSession(FakeResponse(chunks=[b"abc", b"def"]))

        payload = await download_bytes_limited(session, "https://cdn.example/test", max_bytes=6)

        self.assertEqual(payload, b"abcdef")

    async def test_download_rejects_large_content_length_before_streaming(self):
        session = FakeSession(FakeResponse(chunks=[b"unused"], headers={"Content-Length": "7"}))

        payload = await download_bytes_limited(session, "https://cdn.example/test", max_bytes=6)

        self.assertIsNone(payload)

    async def test_download_stops_when_stream_exceeds_limit(self):
        session = FakeSession(FakeResponse(chunks=[b"abc", b"defg"]))

        payload = await download_bytes_limited(session, "https://cdn.example/test", max_bytes=6)

        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
