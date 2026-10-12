"""One isolated Facebook Cookie request; no credential-bearing diagnostics."""
from __future__ import annotations

import logging
import re

import aiohttp
from bs4 import BeautifulSoup

from .cookie_store import prepare_cookie, response_cookies, save_response_cookies
from .facebook_fetch import (
    MAX_FACEBOOK_HTML_BYTES, FacebookPostData, canonical_facebook_url,
    extract_facebook_post, facebook_post_identity,
)

logger = logging.getLogger("discord_digest_bot")
MAX_FACEBOOK_COOKIE_BYTES = 16 * 1024
COOKIE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}


async def _rejection_hint(response) -> tuple[str, int]:
    """Classify a bounded error body without retaining its text or headers."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(16 * 1024):
        body.extend(chunk[:max(0, 64 * 1024 - len(body))])
        if len(body) >= 64 * 1024:
            break
    text = body.decode("utf-8", errors="replace").lower()
    if re.search(r"(?:header|cookie)[\s\S]{0,80}too (?:large|long)", text):
        hint = "headers_too_large"
    elif "checkpoint" in text or "security check" in text:
        hint = "access_challenge"
    elif 'id="login_form"' in text or 'type="password"' in text:
        hint = "login_required"
    elif "bad request" in text:
        hint = "bad_request"
    else:
        hint = "unclassified"
    return hint, len(body)


def _valid_cookie(cookie: str) -> bool:
    if not cookie.strip() or len(cookie) > MAX_FACEBOOK_COOKIE_BYTES or any(ord(c) < 32 or ord(c) >= 127 for c in cookie):
        return False
    for pair in cookie.strip().split(";"):
        name, separator, _value = pair.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            return False
    return True


async def try_facebook_cookie(url: str, cookie: str, *, persist_cookies: bool = True) -> FacebookPostData:
    """Use a known target once, without redirects, cookie jars, or HTML storage."""
    target_url = canonical_facebook_url(url)
    result = FacebookPostData(url=target_url or url, reason="cookie_fetch_failed", debug={"cookie_attempts": 0})
    status = None
    try:
        if not target_url or not facebook_post_identity(target_url):
            result.reason = "cookie_target_unresolved"
            return result
        if not _valid_cookie(cookie):
            result.reason = "cookie_config_invalid"
            return result
        snapshot = prepare_cookie("facebook", target_url, cookie, persist=persist_cookies)
        if not snapshot.header:
            result.reason = "cookie_login_required"
            return result
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
            result.debug["cookie_attempts"] = 1
            async with session.get(target_url, headers={**COOKIE_HEADERS, "Cookie": snapshot.header},
                                   timeout=aiohttp.ClientTimeout(total=25), allow_redirects=False) as response:
                status = response.status
                if status == 400:
                    result.reason = "cookie_request_rejected"
                    hint, size = await _rejection_hint(response)
                    result.debug.update(cookie_error_hint=hint, response_bytes=size)
                    return result
                if status in (401, 403):
                    result.reason = "cookie_login_required"
                    return result
                if status == 429:
                    result.reason = "cookie_rate_limited"
                    return result
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    result.reason = "cookie_login_required" if re.search(r"/(?:login|checkpoint|challenge)(?:[/?]|$)", location, re.I) else "cookie_redirect"
                    return result
                if status != 200:
                    return result
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    result.debug["response_bytes"] = len(body) + len(chunk)
                    if len(body) + len(chunk) > MAX_FACEBOOK_HTML_BYTES:
                        result.reason = "response_too_large"
                        return result
                    body.extend(chunk)
                result.debug["response_bytes"] = len(body)
                text = body.decode("utf-8", errors="replace")
                soup = BeautifulSoup(text, "html.parser")
                if soup.select_one('input[type="password"], input[name="approvals_code"], form#login_form'):
                    result.reason = "cookie_login_required"
                    return result
                parsed = extract_facebook_post(text, target_url, require_public=True)
                parsed.debug.update(result.debug)
                result = parsed
                if result.has_content:
                    save_response_cookies(snapshot, response_cookies(response.headers))
                return result
    except Exception:
        # aiohttp/parser exceptions can include request headers or response data.
        return result
    finally:
        if status is not None:
            result.debug["cookie_status"] = status
        if not result.has_content:
            logger.warning("Facebook cookie fallback stopped: %s; status=%s hint=%s bytes=%s json=%s matched=%s public=%s",
                           result.reason, status, result.debug.get("cookie_error_hint", "none"), result.debug.get("response_bytes", 0),
                           result.debug.get("json_count", 0), result.debug.get("matched_nodes", 0),
                           result.debug.get("public_nodes", 0))
