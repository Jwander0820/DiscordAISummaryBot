"""Offline YouTube video identity; no requests or preview processing."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
VIDEO_PATH = re.compile(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})")
URL_TOKEN = re.compile(r"(?<![\w])https?://[^\s<>()|\[\]\"']+", re.IGNORECASE)


def youtube_post_identity(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or parsed.params):
            return None
        path = parsed.path.rstrip("/")
        if host in SHORT_HOSTS:
            video_id = path.removeprefix("/")
        elif host in YOUTUBE_HOSTS:
            if path == "/watch":
                values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64).get("v", [])
                if len(values) != 1:
                    return None
                video_id = values[0]
            else:
                match = VIDEO_PATH.fullmatch(path)
                if match is None:
                    return None
                video_id = match[1]
        else:
            return None
        if VIDEO_ID.fullmatch(video_id):
            return video_id, f"https://youtu.be/{video_id}"
    except (TypeError, ValueError):
        pass
    return None


def extract_first_youtube_url(content: str) -> str | None:
    """Keep the one-video-per-platform/source-message history contract."""
    for token in URL_TOKEN.finditer(content or ""):
        identity = youtube_post_identity(token.group().rstrip(".,!?;:，。！？；：、"))
        if identity is not None:
            return identity[1]
    return None
