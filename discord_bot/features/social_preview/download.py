from __future__ import annotations

import logging
from typing import Optional

import aiohttp


logger = logging.getLogger("discord_digest_bot")

MAX_IMAGE_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_VIDEO_DOWNLOAD_BYTES = 20 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024


async def download_bytes_limited(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int,
    timeout_sec: int = 20,
) -> Optional[bytes]:
    """Download a response without ever buffering more than ``max_bytes``."""
    try:
        async with session.get(url, timeout=timeout_sec) as response:
            if response.status != 200:
                return None

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        logger.info("Social preview media skipped: Content-Length exceeds limit (%s)", url)
                        return None
                except ValueError:
                    logger.debug("Invalid Content-Length for social preview media: %s", content_length)

            payload = bytearray()
            async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                if len(payload) + len(chunk) > max_bytes:
                    logger.info("Social preview media skipped: streamed body exceeds limit (%s)", url)
                    return None
                payload.extend(chunk)
            return bytes(payload)
    except Exception:
        logger.debug("Social preview media download failed: %s", url, exc_info=True)
        return None
