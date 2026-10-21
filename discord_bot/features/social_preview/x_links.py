"""Offline X/Twitter status identity. Never fetch URLs or touch Discord messages."""
from __future__ import annotations

import re
from urllib.parse import urlparse

X_HOSTS = frozenset({"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"})
STATUS_PATH = re.compile(
    r"/(?:([A-Za-z0-9_]+)/status|i/web/status)/([1-9][0-9]{0,19})"
    r"(?:/(?:photo|video)/[1-9][0-9]*)?/?"
)
URL_TOKEN = re.compile(r"(?<![\w])https?://[^\s<>()|\[\]\"']+", re.IGNORECASE)


def x_post_identity(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or (parsed.hostname or "").lower() not in X_HOSTS):
            return None
        match = STATUS_PATH.fullmatch(parsed.path)
        if match is None:
            return None
        username, status_id = match.groups()
        path = f"/{username}/status/{status_id}" if username and username != "i" else f"/i/web/status/{status_id}"
        return status_id, f"https://x.com{path}"
    except (TypeError, ValueError):
        return None


def extract_first_x_url(content: str) -> str | None:
    """Match the existing one-post-per-platform/source-message history contract."""
    for token in URL_TOKEN.finditer(content or ""):
        identity = x_post_identity(token.group().rstrip(".,!?;:，。！？；：、"))
        if identity is not None:
            return identity[1]
    return None
