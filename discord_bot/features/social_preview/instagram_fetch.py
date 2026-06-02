from __future__ import annotations

import json
import logging
import re
import urllib.request
from html import unescape
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, urlunparse

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised when optional dependency is unavailable
    BeautifulSoup = None

logger = logging.getLogger("discord_digest_bot")

REQUEST_TIMEOUT = 20
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
    native_embed_url: Optional[str] = None
    media: list[InstagramMedia] = field(default_factory=list)


def normalize_instagram_url(url: str) -> str:
    clean = (url or "").strip().rstrip(").,>|")
    parsed = urlparse(clean)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")

    host = parsed.netloc.lower()
    if host == "instagram.com":
        host = "www.instagram.com"

    path = parsed.path.rstrip("/")
    normalized = urlunparse((parsed.scheme or "https", host, path, "", "", ""))
    if not INSTAGRAM_POST_RE.match(normalized):
        raise ValueError(f"Unsupported Instagram URL: {url}")
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


def fetch_instagram_post(url: str) -> InstagramPost:
    normalized_url = normalize_instagram_url(url)
    return _native_embed_post(normalized_url)


def _fetch_instagram_post_with_metadata(url: str) -> InstagramPost:
    """Best-effort metadata path kept for future use; default preview uses native embeds."""
    normalized_url = normalize_instagram_url(url)
    api_post = _try_shortcode_api(normalized_url)
    if _has_preview_signal(api_post):
        return api_post

    media_id_post = _try_media_id_api(normalized_url)
    if _has_preview_signal(media_id_post):
        return media_id_post

    request = urllib.request.Request(normalized_url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            html = response.read().decode("utf-8", errors="ignore")
            final_url = getattr(response, "url", normalized_url) or normalized_url
    except Exception as exc:
        logger.info("Instagram page HTML unavailable for %s: %s", normalized_url, exc)
        return _fallback_media_post(normalized_url)

    html_post = parse_instagram_html(html, final_url=final_url, source_url=normalized_url)
    if not html_post.media and (html_post.text or html_post.author_username):
        for fallback_url in _media_endpoint_urls(html_post):
            _add_media(html_post.media, InstagramMedia("image", fallback_url))
    if not _has_preview_signal(html_post):
        return _fallback_media_post(normalized_url)
    return html_post


def _native_embed_post(url: str) -> InstagramPost:
    normalized_url = normalize_instagram_url(url)
    return InstagramPost(
        url=normalized_url,
        shortcode=instagram_shortcode(normalized_url),
        kind=instagram_kind(normalized_url),
        preview_level="native_embed",
        native_embed_url=_native_embed_proxy_url(normalized_url),
    )


def parse_instagram_html(html: str, *, final_url: str, source_url: Optional[str] = None) -> InstagramPost:
    source = normalize_instagram_url(source_url or final_url)
    try:
        post_url = normalize_instagram_url(final_url)
    except ValueError:
        post_url = source
    post = InstagramPost(url=post_url, shortcode=instagram_shortcode(source), kind=instagram_kind(source))
    soup = _make_soup(html or "")

    _fill_from_open_graph(post, soup)
    _fill_from_json_ld(post, soup)
    _fill_from_embedded_json(post, soup)

    if not post.author_username:
        post.author_username = _username_from_og_title(_first_meta(soup, "og:title"))

    post.media = _dedupe_media([media for media in post.media if not _is_probable_profile_image(media)])
    return post


def _try_shortcode_api(url: str) -> InstagramPost:
    normalized_url = normalize_instagram_url(url)
    post = InstagramPost(
        url=normalized_url,
        shortcode=instagram_shortcode(normalized_url),
        kind=instagram_kind(normalized_url),
    )
    api_url = f"https://www.instagram.com/api/v1/media/shortcode/{post.shortcode}/info/"
    headers = _api_headers(normalized_url)
    request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.info("Instagram shortcode API unavailable for %s: %s", normalized_url, exc)
        return post

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return post
    return _post_from_api_payload(payload, fallback=post)


def _try_media_id_api(url: str) -> InstagramPost:
    normalized_url = normalize_instagram_url(url)
    post = InstagramPost(
        url=normalized_url,
        shortcode=instagram_shortcode(normalized_url),
        kind=instagram_kind(normalized_url),
    )
    try:
        media_id = instagram_media_id_from_shortcode(post.shortcode)
    except ValueError:
        return post

    api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
    request = urllib.request.Request(api_url, headers=_api_headers(normalized_url))
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            final_url = getattr(response, "url", api_url) or api_url
            if "/accounts/login" in urlparse(final_url).path:
                return post
            raw = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.info("Instagram media API unavailable for %s: %s", normalized_url, exc)
        return post

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return post
    return _post_from_api_payload(payload, fallback=post)


def _api_headers(referer: str) -> dict[str, str]:
    return {
        **DEFAULT_HEADERS,
        "Accept": "*/*",
        "X-IG-App-ID": IG_APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "Referer": referer,
    }


def _post_from_api_payload(payload: dict[str, Any], *, fallback: InstagramPost) -> InstagramPost:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return fallback
    item = items[0]
    if not isinstance(item, dict):
        return fallback

    post = InstagramPost(url=fallback.url, shortcode=fallback.shortcode, kind=fallback.kind)
    _fill_from_api_item(post, item)
    post.media = _dedupe_media([media for media in post.media if not _is_probable_profile_image(media)])
    return post


def _fill_from_api_item(post: InstagramPost, item: dict[str, Any]) -> None:
    user = item.get("user")
    if isinstance(user, dict):
        post.author_username = _first_string(user.get("username"))
        post.author_name = _first_string(user.get("full_name"), user.get("username"))

    caption = item.get("caption")
    if isinstance(caption, dict):
        post.text = _first_string(caption.get("text"))
    elif isinstance(caption, str) and caption.strip():
        post.text = caption.strip()
    if not post.text:
        post.text = _first_string(item.get("accessibility_caption"), item.get("title"))

    _add_api_image_media(post.media, item)
    _add_api_video_media(post.media, item)

    carousel_media = item.get("carousel_media")
    if isinstance(carousel_media, list):
        for child in carousel_media:
            if isinstance(child, dict):
                _add_api_image_media(post.media, child)
                _add_api_video_media(post.media, child)


def _add_api_image_media(media: list[InstagramMedia], item: dict[str, Any]) -> None:
    candidates = item.get("image_versions2", {}).get("candidates") if isinstance(item.get("image_versions2"), dict) else None
    if isinstance(candidates, list):
        best = _best_api_candidate(candidates)
        if best:
            _add_media(
                media,
                InstagramMedia(
                    "image",
                    best["url"],
                    width=_safe_int(best.get("width")),
                    height=_safe_int(best.get("height")),
                ),
            )
            return

    for key in ("thumbnail_url", "thumbnail_src", "display_url"):
        value = item.get(key)
        if isinstance(value, str) and _is_http_url(value):
            _add_media(media, InstagramMedia("image", value))
            return


def _add_api_video_media(media: list[InstagramMedia], item: dict[str, Any]) -> None:
    videos = item.get("video_versions")
    if not isinstance(videos, list):
        return
    best = _best_api_candidate(videos)
    if best:
        _add_media(
            media,
            InstagramMedia(
                "video",
                best["url"],
                width=_safe_int(best.get("width")),
                height=_safe_int(best.get("height")),
                mime=_video_mime_from_url(best["url"]),
            ),
        )


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


def _media_endpoint_urls(post: InstagramPost) -> list[str]:
    kinds = [post.kind or "p", "p", "reel", "tv"]
    urls = []
    for kind in kinds:
        if kind:
            urls.append(f"https://www.instagram.com/{kind}/{post.shortcode}/media/?size=l")
    return _dedupe_strings(urls)


def _fallback_media_post(url: str) -> InstagramPost:
    normalized_url = normalize_instagram_url(url)
    post = InstagramPost(
        url=normalized_url,
        shortcode=instagram_shortcode(normalized_url),
        kind=instagram_kind(normalized_url),
        preview_level="media_fallback",
        native_embed_url=_native_embed_proxy_url(normalized_url),
    )
    for fallback_url in _media_endpoint_urls(post):
        _add_media(post.media, InstagramMedia("image", fallback_url))
    logger.info("Instagram using media fallback for %s with %d candidate(s)", normalized_url, len(post.media))
    return post


def _native_embed_proxy_url(url: str) -> str:
    normalized_url = normalize_instagram_url(url)
    shortcode = instagram_shortcode(normalized_url)
    kind = instagram_kind(normalized_url)
    if kind == "reel":
        return f"https://www.kkinstagram.com/reels/{shortcode}/"
    return f"https://www.kkinstagram.com/{kind}/{shortcode}/"


def _has_preview_signal(post: InstagramPost) -> bool:
    return bool(post.text or post.author_username or post.media)


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


def _fill_from_json_ld(post: InstagramPost, soup: Any) -> None:
    for item in _iter_json_ld_items(soup):
        item_type = _json_type(item)
        if item_type not in {"SocialMediaPosting", "ImageObject", "VideoObject", "Posting"}:
            continue

        author = item.get("author")
        author_name, author_username = _author_from_json(author)
        if author_name and not post.author_name:
            post.author_name = author_name
        if author_username and not post.author_username:
            post.author_username = author_username

        text = _first_string(item.get("articleBody"), item.get("caption"), item.get("description"), item.get("name"))
        if text and not _looks_like_login_wall(text) and not post.text:
            post.text = text

        for image_url in _image_urls_from_json_value(item.get("image")):
            _add_media(post.media, InstagramMedia("image", image_url))
        for image_url in _image_urls_from_json_value(item.get("thumbnailUrl") or item.get("thumbnail")):
            _add_media(post.media, InstagramMedia("image", image_url))

        for image_url in _video_thumbnail_urls_from_json_value(item.get("video")):
            _add_media(post.media, InstagramMedia("image", image_url))
        for video_url in _video_urls_from_json_value(item.get("video")):
            _add_media(post.media, InstagramMedia("video", video_url, mime=_video_mime_from_url(video_url)))

        if item_type == "VideoObject":
            for image_url in _image_urls_from_json_value(
                item.get("thumbnailUrl") or item.get("thumbnail") or item.get("image")
            ):
                _add_media(post.media, InstagramMedia("image", image_url))
            for video_url in _urls_from_json_value(
                item.get("contentUrl") or item.get("embedUrl") or item.get("url")
            ):
                _add_media(post.media, InstagramMedia("video", video_url, mime=_video_mime_from_url(video_url)))
        elif item_type == "ImageObject":
            for image_url in _image_urls_from_json_value(item.get("contentUrl") or item.get("url")):
                _add_media(post.media, InstagramMedia("image", image_url))


def _fill_from_embedded_json(post: InstagramPost, soup: Any) -> None:
    for payload in _iter_script_json_payloads(soup):
        for item in _walk_json_dicts(payload):
            candidates = [item]
            for key in ("shortcode_media", "xdt_shortcode_media", "media"):
                nested = item.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)

            for candidate in candidates:
                if _looks_like_instagram_media_node(candidate):
                    _fill_from_embedded_media_node(post, candidate)


def _iter_script_json_payloads(soup: Any) -> Iterable[Any]:
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue

        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                yield json.loads(stripped)
                continue
            except json.JSONDecodeError:
                pass

        for marker in (
            '"shortcode_media"',
            '"xdt_shortcode_media"',
            '"display_resources"',
            '"edge_sidecar_to_children"',
            '"video_versions"',
            '"image_versions2"',
        ):
            for match in re.finditer(re.escape(marker), raw):
                fragment = _extract_json_object_around(raw, match.start())
                if not fragment:
                    continue
                try:
                    yield json.loads(fragment)
                except json.JSONDecodeError:
                    continue


def _extract_json_object_around(text: str, index: int) -> Optional[str]:
    start = text.rfind("{", 0, index)
    while start != -1:
        end = _find_json_object_end(text, start)
        if end is not None and end > index:
            return text[start : end + 1]
        start = text.rfind("{", 0, start)
    return None


def _find_json_object_end(text: str, start: int) -> Optional[int]:
    depth = 0
    in_string = False
    escape = False
    quote = ""
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _walk_json_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_dicts(item)


def _looks_like_instagram_media_node(item: dict[str, Any]) -> bool:
    return any(
        key in item
        for key in (
            "display_url",
            "display_resources",
            "video_url",
            "video_versions",
            "image_versions2",
            "edge_sidecar_to_children",
            "carousel_media",
        )
    )


def _fill_from_embedded_media_node(post: InstagramPost, item: dict[str, Any]) -> None:
    owner = item.get("owner") or item.get("user")
    if isinstance(owner, dict):
        if not post.author_username:
            post.author_username = _first_string(owner.get("username"))
        if not post.author_name:
            post.author_name = _first_string(owner.get("full_name"), owner.get("username"))

    caption = _caption_from_embedded_node(item)
    if caption and not _looks_like_login_wall(caption) and not post.text:
        post.text = caption

    for image_url in _embedded_image_urls(item):
        _add_media(post.media, InstagramMedia("image", image_url))

    for video_url in _embedded_video_urls(item):
        _add_media(post.media, InstagramMedia("video", video_url, mime=_video_mime_from_url(video_url)))

    sidecar = item.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict):
        edges = sidecar.get("edges")
        if isinstance(edges, list):
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    _fill_from_embedded_media_node(post, node)

    carousel_media = item.get("carousel_media")
    if isinstance(carousel_media, list):
        for child in carousel_media:
            if isinstance(child, dict):
                _fill_from_api_item(post, child)
                _fill_from_embedded_media_node(post, child)


def _caption_from_embedded_node(item: dict[str, Any]) -> Optional[str]:
    for key in ("caption", "caption_text", "accessibility_caption", "title", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for path in (
        ("edge_media_to_caption", "edges", 0, "node", "text"),
        ("caption", "text"),
        ("accessibility_caption",),
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


def _iter_json_ld_items(soup: Any) -> Iterable[dict[str, Any]]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _walk_json_ld(payload)


def _walk_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _walk_json_ld(graph)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_ld(item)


def _json_type(item: dict[str, Any]) -> str:
    raw_type = item.get("@type") or item.get("type") or ""
    if isinstance(raw_type, list):
        raw_type = raw_type[0] if raw_type else ""
    return str(raw_type)


def _author_from_json(author: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(author, str):
        return author, _strip_username(author)
    if not isinstance(author, dict):
        return None, None

    name = _first_string(author.get("name"), author.get("alternateName"))
    username = _strip_username(_first_string(author.get("alternateName"), author.get("identifier"), name))
    return name, username


def _urls_from_json_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if _is_http_url(value) else []
    if isinstance(value, dict):
        urls: list[str] = []
        for key in ("url", "contentUrl", "embedUrl", "thumbnailUrl"):
            urls.extend(_urls_from_json_value(value.get(key)))
        return _dedupe_strings(urls)
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_urls_from_json_value(item))
        return _dedupe_strings(urls)
    return []


def _image_urls_from_json_value(value: Any) -> list[str]:
    return _urls_from_json_value(value)


def _video_urls_from_json_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if _is_http_url(value) else []
    if isinstance(value, dict):
        urls: list[str] = []
        for key in ("contentUrl", "embedUrl", "url"):
            urls.extend(_urls_from_json_value(value.get(key)))
        return _dedupe_strings(urls)
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_video_urls_from_json_value(item))
        return _dedupe_strings(urls)
    return []


def _video_thumbnail_urls_from_json_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        urls: list[str] = []
        for key in ("thumbnailUrl", "thumbnail", "image"):
            urls.extend(_urls_from_json_value(value.get(key)))
        return _dedupe_strings(urls)
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(_video_thumbnail_urls_from_json_value(item))
        return _dedupe_strings(urls)
    return []


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


def _strip_username(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"@?([A-Za-z0-9._]+)", value)
    return match.group(1) if match else None


def _first_string(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


def _dedupe_media(media: list[InstagramMedia]) -> list[InstagramMedia]:
    seen: set[str] = set()
    output: list[InstagramMedia] = []
    for item in media:
        if item.url in seen:
            continue
        seen.add(item.url)
        output.append(item)
    return output


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
