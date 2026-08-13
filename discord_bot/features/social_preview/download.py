from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Literal, Optional, Set
from urllib.parse import urljoin, urlparse

import aiohttp


logger = logging.getLogger("discord_digest_bot")

MAX_IMAGE_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_VIDEO_DOWNLOAD_BYTES = 20 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_MEDIA_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
TRUSTED_MEDIA_HOST_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    "fbsbx.com",
)
TRUSTED_MEDIA_EXACT_HOSTS = {
    "lookaside.instagram.com",
}
INSTAGRAM_MEDIA_ENDPOINT_HOSTS = {
    "instagram.com",
    "www.instagram.com",
}


def _normalized_host(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        return None

    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return host


def is_trusted_media_url(url: str) -> bool:
    """Allow only HTTPS URLs hosted by Meta media infrastructure."""
    host = _normalized_host(url)
    if host is None:
        return False

    if host in TRUSTED_MEDIA_EXACT_HOSTS:
        return True
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in TRUSTED_MEDIA_HOST_SUFFIXES):
        return True
    if host in INSTAGRAM_MEDIA_ENDPOINT_HOSTS:
        path_parts = [part for part in urlparse(url).path.split("/") if part]
        return (
            len(path_parts) == 3
            and path_parts[0].lower() in {"p", "reel", "reels", "tv"}
            and path_parts[2].lower() == "media"
        )
    return False


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


async def _resolve_public_addresses(host: str) -> Set[str]:
    """Resolve a host and reject it if any returned address is non-public."""
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(
        host,
        443,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    addresses = {result[4][0] for result in results}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("media host resolved to a non-public address")
    return addresses


def _response_peer_address(response: aiohttp.ClientResponse) -> Optional[str]:
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        protocol = getattr(response, "_protocol", None)
        transport = getattr(protocol, "transport", None)
    if transport is None:
        return None
    peer = transport.get_extra_info("peername")
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return None


async def _validate_media_target(url: str) -> str:
    host = _normalized_host(url)
    if host is None or not is_trusted_media_url(url):
        raise ValueError("media URL is not an approved HTTPS Meta media URL")
    await _resolve_public_addresses(host)
    return host


def _matches_media_type(content_type: str, media_type: Literal["image", "video"]) -> bool:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return normalized.startswith(f"{media_type}/")


async def download_bytes_limited(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int,
    timeout_sec: int = 20,
    media_type: Literal["image", "video"] = "image",
    max_redirects: int = MAX_MEDIA_REDIRECTS,
) -> Optional[bytes]:
    """Safely download trusted Meta media without buffering beyond ``max_bytes``."""
    current_url = url
    try:
        if max_bytes < 1 or max_redirects < 0:
            raise ValueError("download limits must be positive")

        for redirect_count in range(max_redirects + 1):
            host = await _validate_media_target(current_url)
            async with session.get(
                current_url,
                timeout=timeout_sec,
                allow_redirects=False,
            ) as response:
                peer_address = _response_peer_address(response)
                if peer_address is not None and not _is_public_address(peer_address):
                    raise ValueError("media connection used a non-public peer address")

                if response.status in REDIRECT_STATUSES:
                    if redirect_count >= max_redirects:
                        logger.info("Social preview media skipped: redirect limit exceeded (%s)", host)
                        return None
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    current_url = urljoin(current_url, location)
                    continue

                if response.status != 200:
                    return None

                content_type = response.headers.get("Content-Type", "")
                if not _matches_media_type(content_type, media_type):
                    logger.info(
                        "Social preview media skipped: unexpected Content-Type %r (%s)",
                        content_type,
                        host,
                    )
                    return None

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            logger.info("Social preview media skipped: Content-Length exceeds limit (%s)", host)
                            return None
                    except ValueError:
                        logger.debug("Invalid Content-Length for social preview media: %s", content_length)

                payload = bytearray()
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                    if len(payload) + len(chunk) > max_bytes:
                        logger.info("Social preview media skipped: streamed body exceeds limit (%s)", host)
                        return None
                    payload.extend(chunk)
                return bytes(payload)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        host = _normalized_host(current_url) or "invalid-host"
        logger.debug("Social preview media download rejected or failed: %s", host, exc_info=True)
        return None
