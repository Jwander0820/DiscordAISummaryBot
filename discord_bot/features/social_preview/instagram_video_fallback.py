"""Resolve a public Reel through the existing provider without credentials or media downloads."""
from __future__ import annotations

import html
import logging
import re
from time import perf_counter
from urllib.parse import parse_qs, unquote, urlparse

from .download import is_trusted_media_url
from .instagram_fetch import _make_soup, _native_embed_proxy_url, instagram_shortcode
from .instagram_logging import elapsed_ms, log_event, new_trace_id

logger = logging.getLogger("discord_digest_bot")
MAX_FALLBACK_HTML_BYTES = 256 * 1024
PROVIDER_HOSTS = {"www.kkinstagram.com", "kkinstagram.com"}
REDIRECTS = {301, 302, 303, 307, 308}


def _direct_mp4(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    value = html.unescape(value).strip()
    if not is_trusted_media_url(value):
        return None
    parsed = urlparse(value)
    if not unquote(parsed.path).lower().endswith(".mp4"):
        return None
    try:
        keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)}
    except ValueError:
        return None
    return None if keys & {"bytestart", "byteend", "range"} else value


def _same_post(value, code):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in PROVIDER_HOSTS | {"www.instagram.com", "instagram.com"}:
        return False
    match = re.fullmatch(r"/(?:[^/]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)/?", parsed.path)
    return bool(match and match.group(1) == code)


def resolve_instagram_video_fallback(url, *, trace_id=None):
    """One anonymous metadata GET; a CDN redirect is returned, never followed."""
    import requests

    response = None
    status, reason = None, "unavailable"
    started, trace_id = perf_counter(), trace_id or new_trace_id()
    code = instagram_shortcode(url)
    try:
        target = _native_embed_proxy_url(url)
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(target, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
                "Accept": "text/html",
            }, timeout=15, allow_redirects=False, stream=True)
            status = response.status_code
            if status in REDIRECTS:
                video = _direct_mp4(response.headers.get("Location"))
                reason = "success" if video else "redirect_rejected"
                return video
            if status != 200:
                reason = "rate_limited" if status == 429 else "not_found" if status == 404 else "http_error"
                return None
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                reason = "not_metadata"
                return None
            body = bytearray()
            for chunk in response.iter_content(65536):
                if len(body) + len(chunk) > MAX_FALLBACK_HTML_BYTES:
                    reason = "response_too_large"
                    return None
                body.extend(chunk)
            soup = _make_soup(body.decode("utf-8", errors="replace"))
            canonical = soup.find("meta", attrs={"property": "og:url"})
            if not canonical or not _same_post(canonical.get("content"), instagram_shortcode(url)):
                reason = "target_unverified"
                return None
            for key in ("og:video:secure_url", "og:video", "og:video:url", "twitter:player:stream"):
                for tag in soup.find_all("meta", attrs={"property": key}) + soup.find_all("meta", attrs={"name": key}):
                    video = _direct_mp4(tag.get("content"))
                    if video:
                        reason = "success"
                        return video
    except requests.Timeout:
        reason = "timeout"
    except Exception:
        # Exceptions can contain signed media URLs; report fixed categories only.
        reason = "request_failed"
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        log_event("fetch_stage", trace_id=trace_id, shortcode=code, stage="kkinstagram",
                  http_status=status, result=reason, elapsed_ms=elapsed_ms(started),
                  video_count=int(reason == "success"))
    return None
