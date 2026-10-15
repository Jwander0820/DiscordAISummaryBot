"""Instagram models, URL utilities and helpers shared by the active fetchers."""
from __future__ import annotations

import re
from html import unescape
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised when optional dependency is unavailable
    BeautifulSoup = None

REQUEST_TIMEOUT = 30
IG_APP_ID = "936619743392459"
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

INSTAGRAM_POST_RE = re.compile(
    r"^https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(p|reels?|tv)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class InstagramMedia:
    type: str
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    alt: Optional[str] = None
    mime: Optional[str] = None


@dataclass
class InstagramPost:
    url: str
    shortcode: str
    kind: Optional[str] = None
    author_username: Optional[str] = None
    author_name: Optional[str] = None
    text: Optional[str] = None
    preview_level: str = "metadata"
    media: list[InstagramMedia] = field(default_factory=list)
    preview_error: Optional[str] = None
    trace_id: Optional[str] = None
    fetch_source: str = "unknown"


def normalize_instagram_url(url: str) -> str:
    clean = (url or "").strip().rstrip(").,>|")
    parsed = urlparse(clean)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")

    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am"}:
        raise ValueError("Unsupported Instagram host")
    path = parsed.path.rstrip("/")
    normalized = urlunparse(("https", "www.instagram.com", path, "", "", ""))
    if not re.fullmatch(r"/(?:p|reels?|tv)/[A-Za-z0-9_-]+", path, re.IGNORECASE):
        raise ValueError("Unsupported Instagram post path")
    return normalized


def instagram_shortcode(url: str) -> str:
    match = INSTAGRAM_POST_RE.match(normalize_instagram_url(url))
    if not match:
        raise ValueError(f"Unsupported Instagram URL: {url}")
    return match.group(2)


def instagram_kind(url: str) -> str:
    match = INSTAGRAM_POST_RE.match(normalize_instagram_url(url))
    if not match:
        raise ValueError(f"Unsupported Instagram URL: {url}")
    kind = match.group(1).lower()
    return "reel" if kind == "reels" else kind


def instagram_media_id_from_shortcode(shortcode: str) -> str:
    media_id = 0
    for char in shortcode:
        if char not in SHORTCODE_ALPHABET:
            raise ValueError(f"Unsupported Instagram shortcode character: {char}")
        media_id = media_id * 64 + SHORTCODE_ALPHABET.index(char)
    return str(media_id)


def fetch_instagram_post(url: str, *, trace_id: Optional[str] = None) -> InstagramPost:
    from .instagram_direct import fetch_instagram_direct

    return fetch_instagram_direct(normalize_instagram_url(url), trace_id=trace_id)


def _api_headers(referer: str) -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Accept": "*/*",
        "X-IG-App-ID": IG_APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "Referer": referer,
    }


def _best_api_candidate(candidates: list[Any]) -> Optional[dict[str, Any]]:
    dict_candidates = [
        item
        for item in candidates
        if isinstance(item, dict) and _is_http_url(str(item.get("url") or item.get("src") or ""))
    ]
    if not dict_candidates:
        return None
    return max(dict_candidates, key=_candidate_area)


def _candidate_area(item: dict[str, Any]) -> int:
    width = _safe_int(item.get("width")) or _safe_int(item.get("config_width")) or 0
    height = _safe_int(item.get("height")) or _safe_int(item.get("config_height")) or 0
    return width * height


def _native_embed_proxy_url(url: str) -> str:
    normalized_url = normalize_instagram_url(url)
    shortcode = instagram_shortcode(normalized_url)
    kind = instagram_kind(normalized_url)
    if kind == "reel":
        return f"https://www.kkinstagram.com/reels/{shortcode}/"
    return f"https://www.kkinstagram.com/{kind}/{shortcode}/"


def _make_soup(html: str) -> Any:
    if BeautifulSoup is not None:
        return BeautifulSoup(html, "html.parser")
    return _RegexSoup(html)


class _RegexTag:
    def __init__(self, attrs: dict[str, str], text: str = "") -> None:
        self.attrs = attrs
        self.string = text
        self._text = text

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(key, default)

    def get_text(self) -> str:
        return self._text


class _RegexSoup:
    def __init__(self, html: str) -> None:
        self.html = html

    def find_all(self, name: str, attrs: Optional[dict[str, str]] = None) -> list[_RegexTag]:
        attrs = attrs or {}
        if name == "meta":
            return self._find_meta(attrs)
        if name == "script":
            return self._find_script(attrs)
        return []

    def _find_meta(self, attrs: dict[str, str]) -> list[_RegexTag]:
        tags: list[_RegexTag] = []
        for raw_tag in re.findall(r"<meta\b[^>]*>", self.html, flags=re.IGNORECASE):
            parsed_attrs = _parse_attrs(raw_tag)
            if all(parsed_attrs.get(key) == value for key, value in attrs.items()):
                tags.append(_RegexTag(parsed_attrs))
        return tags

    def _find_script(self, attrs: dict[str, str]) -> list[_RegexTag]:
        pattern = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<text>.*?)</script>", re.IGNORECASE | re.DOTALL)
        tags: list[_RegexTag] = []
        for match in pattern.finditer(self.html):
            parsed_attrs = _parse_attrs(match.group("attrs"))
            if all(parsed_attrs.get(key) == value for key, value in attrs.items()):
                tags.append(_RegexTag(parsed_attrs, match.group("text")))
        return tags


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    pattern = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
    for key, _quote, value in pattern.findall(raw):
        attrs[key.lower()] = value
    return attrs


def _fill_from_open_graph(post: InstagramPost, soup: Any) -> None:
    title = _first_meta(soup, "og:title")
    description = _first_meta(soup, "og:description") or _first_meta(soup, "description")

    username = _username_from_og_title(title) or _username_from_description(description)
    if username and not post.author_username:
        post.author_username = username
    if username and not post.author_name:
        post.author_name = f"@{username}"

    text = _caption_from_description(description)
    if text and not post.text:
        post.text = text
    elif description and not post.text and not _looks_like_login_wall(description):
        post.text = description

    image_meta_keys = (
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src",
        "thumbnail",
        "thumbnailUrl",
        "og:video:image",
    )
    for image_url in _unique_meta_contents(soup, image_meta_keys):
        _add_media(post.media, InstagramMedia("image", image_url))

    for key in ("og:video", "og:video:url", "og:video:secure_url"):
        for video_url in _meta_contents(soup, key):
            _add_media(post.media, InstagramMedia("video", video_url, mime=_video_mime_from_url(video_url)))


def _caption_from_embedded_node(item: dict[str, Any]) -> Optional[str]:
    # A real caption may be nested while accessibility_caption is a string.
    # Image alt text is not the author's post body, even when no caption exists.
    for path in (
        ("caption", "text"),
        ("caption",),
        ("edge_media_to_caption", "edges", 0, "node", "text"),
        ("caption_text",),
        ("text",),
        ("title",),
    ):
        value = _get_path(item, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _embedded_image_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    display_resources = item.get("display_resources")
    if isinstance(display_resources, list):
        resources = [entry for entry in display_resources if isinstance(entry, dict)]
        if resources:
            best = _best_api_candidate(resources)
            if best:
                urls.append(best["url"] if "url" in best else best.get("src", ""))

    display_url = item.get("display_url")
    if isinstance(display_url, str):
        urls.append(display_url)

    for key in ("thumbnail_url", "thumbnail_src", "thumbnailUrl"):
        value = item.get(key)
        if isinstance(value, str):
            urls.append(value)

    thumbnail_resources = item.get("thumbnail_resources")
    if isinstance(thumbnail_resources, list):
        best = _best_api_candidate(thumbnail_resources)
        if best:
            urls.append(best["url"] if "url" in best else best.get("src", ""))

    image_versions2 = item.get("image_versions2")
    if isinstance(image_versions2, dict):
        best = _best_api_candidate(image_versions2.get("candidates") or [])
        if best:
            urls.append(best["url"])

    return [url for url in _dedupe_strings(urls) if _is_http_url(url)]


def _embedded_video_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    video_url = item.get("video_url")
    if isinstance(video_url, str):
        urls.append(video_url)

    video_versions = item.get("video_versions")
    if isinstance(video_versions, list):
        best = _best_api_candidate(video_versions)
        if best:
            urls.append(best["url"])

    return [url for url in _dedupe_strings(urls) if _is_http_url(url)]


def _get_path(value: Any, path: tuple[Any, ...]) -> Any:
    current = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or len(current) <= part:
                return None
            current = current[part]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
    return current


def _meta_contents(soup: Any, key: str) -> list[str]:
    values: list[str] = []
    for attrs in ({"property": key}, {"name": key}):
        for tag in soup.find_all("meta", attrs=attrs):
            value = tag.get("content")
            if value and value.strip():
                values.append(value.strip())
    return _dedupe_strings(values)


def _unique_meta_contents(soup: Any, keys: Iterable[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        values.extend(_meta_contents(soup, key))
    return _dedupe_strings(values)


def _first_meta(soup: Any, key: str) -> Optional[str]:
    values = _meta_contents(soup, key)
    return values[0] if values else None


def _caption_from_description(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    text = unescape(description).strip()
    if _looks_like_login_wall(text):
        return None

    quoted = re.search(r'["“](.+?)["”]\s*$', text, re.DOTALL)
    if quoted:
        return quoted.group(1).strip()

    after_instagram = re.search(r"\bon\s+Instagram\s*:\s*(.+)$", text, re.IGNORECASE | re.DOTALL)
    if after_instagram:
        return _strip_wrapping_quotes(after_instagram.group(1).strip())

    after_colon = re.search(r":\s*(.+)$", text, re.DOTALL)
    if after_colon:
        candidate = _strip_wrapping_quotes(after_colon.group(1).strip())
        if candidate and not _looks_like_counter_prefix(candidate):
            return candidate

    return text


def _strip_wrapping_quotes(text: str) -> str:
    return text.strip().strip('"“”').strip()


def _looks_like_counter_prefix(text: str) -> bool:
    lowered = text.lower()
    return bool(re.match(r"^[\d,.]+\s+(likes?|comments?|followers?)\b", lowered))


def _username_from_og_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    match = re.search(r"\(@([A-Za-z0-9._]+)\)", title)
    if match:
        return match.group(1)
    match = re.search(r"@([A-Za-z0-9._]+)", title)
    return match.group(1) if match else None


def _username_from_description(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    match = re.search(r"-\s*([A-Za-z0-9._]+)\s+on\s+Instagram", description)
    return match.group(1) if match else None


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _add_media(media: list[InstagramMedia], item: InstagramMedia) -> None:
    if not item.url or not _is_http_url(item.url):
        return
    if any(existing.url == item.url for existing in media):
        return
    media.append(item)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _is_probable_profile_image(media: InstagramMedia) -> bool:
    url = media.url.lower()
    return any(
        marker in url
        for marker in (
            "profile_pic",
            "/t51.2885-19/",
            "/t51.82787-19/",
            "s150x150",
            "favicon",
        )
    )


def _video_mime_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.lower()
    if path.endswith(".mp4"):
        return "video/mp4"
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    return None


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_login_wall(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "log in to instagram",
            "sign up for instagram",
            "create an account or log in",
            "登入 instagram",
        )
    )
