# threads_fetch.py
from __future__ import annotations

import json
import os
import re
import sys
import time
import html
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse, urlencode

from .text import TextSpan, coalesce_text_spans, trim_text_spans

# =============== 配置 ===============
REQUEST_TIMEOUT = 15
RETRY = 2
MAX_PAGE_ATTEMPTS = 6
MAX_EMBED_HTML_BYTES = 2 * 1024 * 1024
RETRY_BACKOFF = 1.8

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
UA_INSTAGRAM = (
    # 常見的 IG/Threads App UA 片段（用來繞過某些變體）
    "Instagram 305.0.0.34.110 Android (31/12; 420dpi; 1080x2138; Xiaomi; M2102K1G; star; qcom; en_US)"
)
UA_DISCORD = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_LANG = "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7"

THREADS_RE = re.compile(
    r"https?://(www\.)?(threads\.net|threads\.com)/@[^/]+/post/[A-Za-z0-9_\-]+/?",
    re.IGNORECASE,
)
THREADS_SHARE_RE = re.compile(
    r"https?://(www\.)?(threads\.net|threads\.com)/share/[A-Za-z0-9_\-]+/?",
    re.IGNORECASE,
)
THREADS_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
THREADS_SHARE_MAX_REDIRECTS = 5


# =============== 資料結構 ===============
@dataclass
class ThreadsMedia:
    type: str  # "image" | "video"
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    alt: Optional[str] = None
    mime: Optional[str] = None
    spoiler: bool = False


@dataclass
class ThreadsPost:
    url: str
    author_username: Optional[str] = None
    author_name: Optional[str] = None
    text: Optional[str] = None
    created_at: Optional[str] = None
    media: List[ThreadsMedia] = field(default_factory=list)
    oembed_html: Optional[str] = None
    preview_error: Optional[str] = None
    debug: Dict[str, Any] = field(default_factory=dict)
    text_spans: Optional[List[TextSpan]] = None


# =============== 小工具 ===============
def _strip_tracking_query(url: str) -> str:
    """移除 Threads 分享連結常見的 tracking query 與尾端標點。"""
    # 連結包在 ||spoiler|| 時，regex 可能把結尾 || 一起吃進來
    parsed = urlparse(url.rstrip(").,>|"))
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))


def _resolve_threads_share_url(url: str) -> str:
    """Resolve a Threads ``/share/<token>`` URL to its canonical post URL."""
    import requests

    share_url = _strip_tracking_query(url)
    if not THREADS_SHARE_RE.fullmatch(share_url):
        raise ValueError(f"不是 Threads 分享 URL：{url}")

    current_url = share_url
    last_url = share_url

    for _ in range(THREADS_SHARE_MAX_REDIRECTS + 1):
        response = requests.get(
            current_url,
            # Do not automatically follow the canonical post URL. Threads can
            # redirect that second request to ``/?error=invalid_post`` even
            # though the share response already exposed the correct post URL.
            headers={"Accept": DEFAULT_ACCEPT, "Accept-Language": DEFAULT_LANG},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        try:
            response_url = getattr(response, "url", None) or current_url
            resolved_url = _strip_tracking_query(response_url)
            if THREADS_RE.fullmatch(resolved_url):
                return resolved_url

            status_code = getattr(response, "status_code", None)
            headers = getattr(response, "headers", {}) or {}
            location = headers.get("Location") or headers.get("location")
            if status_code not in THREADS_REDIRECT_STATUSES or not location:
                last_url = resolved_url
                break

            next_url = urljoin(response_url, location)
            parsed_next = urlparse(next_url)
            next_host = parsed_next.netloc.lower().split(":", 1)[0]
            if parsed_next.scheme not in {"http", "https"} or next_host not in {
                "threads.net",
                "www.threads.net",
                "threads.com",
                "www.threads.com",
            }:
                raise ValueError("Threads 分享 URL 導向非 Threads 網域")

            resolved_url = _strip_tracking_query(next_url)
            if THREADS_RE.fullmatch(resolved_url):
                return resolved_url
            if not THREADS_SHARE_RE.fullmatch(resolved_url):
                last_url = resolved_url
                break
            current_url = resolved_url
            last_url = resolved_url
        finally:
            response.close()

    raise ValueError(f"Threads 分享 URL 未導向標準 Threads 貼文：{last_url}")


def build_candidate_urls(url: str) -> list[str]:
    """建立多種 host/query 版本，增加 requests 抓取成功率。"""
    p = urlparse(_strip_tracking_query(url))
    if p.scheme not in ("http", "https"):
        p = p._replace(scheme="https")

    path = p.path.rstrip("/")
    hosts = ["threads.net", "threads.com"]
    queries = ["", "hl=en"]  # 先不帶，再帶 hl=en

    candidates = []
    for h in hosts:
        for q in queries:
            candidates.append(urlunparse((p.scheme, h, path, "", q, "")))
    # 去重保序
    seen, out = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u);
            out.append(u)
    return out


def _normalize(url: str) -> str:
    """把輸入 URL 正規化成 parser 內部使用的標準 Threads URL。"""
    clean_url = _strip_tracking_query(url)
    if not THREADS_RE.match(clean_url):
        raise ValueError(f"不是 Threads 貼文 URL：{url}")
    p = urlparse(clean_url)
    scheme = p.scheme or "https"
    path = p.path.rstrip("/")
    # 先強制 threads.net，加 hl=en 提升 meta 完整度
    return urlunparse((scheme, "threads.net", path, "", "hl=en", ""))


def _variants(url: str) -> List[str]:
    """回傳不同 Threads host 與 `hl=en` 組合的變體網址。"""
    p = urlparse(_strip_tracking_query(url))
    path = p.path.rstrip("/")
    out = []
    for host in ("threads.net", "threads.com"):
        for q in ("hl=en", ""):
            out.append(urlunparse((p.scheme or "https", host, path, "", q, "")))
    # 去重保序
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u);
            uniq.append(u)
    return uniq


def _threads_error_from_url(url: Optional[str]) -> Optional[str]:
    """Return a known Threads error code from a redirected final URL."""
    if not url:
        return None

    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}:
        return None

    for key, values in parse_qs(parsed.query).items():
        if key.lower() != "error":
            continue
        for value in values:
            if value.lower() == "invalid_post":
                return "invalid_post"
    return None


def _is_probable_profile_image(item: ThreadsMedia) -> bool:
    """根據 URL/alt 判斷媒體是否比較像頭像而非貼文圖片。"""
    if item.type != "image":
        return False

    url = (item.url or "").lower()
    if any(token in url for token in ["profile_pic", "profilepic", "profile_media", "avatar"]):
        return True
    if re.search(r"/t(?:\d+|51)\.(?:2885|82787)-19/", url):
        return True

    alt = (item.alt or "").lower()
    if alt and any(token in alt for token in ["profile picture", "頭像", "大頭照"]):
        return True

    return False


def _append_media_unique(lst: List[ThreadsMedia], item: ThreadsMedia):
    """避免重複或頭像型圖片混入媒體列表。"""
    if not item.url:
        return False
    if _is_probable_profile_image(item):
        return False
    for existing in lst:
        if existing.url == item.url:
            existing.spoiler = existing.spoiler or item.spoiler
            return False
    lst.append(item)
    return True


def _safe_int(value: Any) -> Optional[int]:
    """寬鬆地把值轉成 int；失敗回 `None`。"""
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _dimensions_from_url(url: str) -> tuple[Optional[int], Optional[int]]:
    """嘗試從 CDN 路徑中的 `1080x1350` 片段推回圖片尺寸。"""
    path = urlparse(url).path.lower()
    match = re.search(r"(?:^|/)(?:[sp])?(?P<w>\d{2,4})x(?P<h>\d{2,4})(?:/|$)", path)
    if not match:
        return None, None
    return _safe_int(match.group("w")), _safe_int(match.group("h"))


def _best_src_from_srcset(srcset: str) -> Optional[str]:
    """從 srcset 中選出最大寬度的圖片來源。"""
    best_url = None
    best_width = -1
    for raw in (srcset or "").split(","):
        part = raw.strip()
        if not part:
            continue
        pieces = part.split()
        url = pieces[0].strip()
        width = 0
        if len(pieces) > 1 and pieces[1].endswith("w"):
            width = _safe_int(pieces[1][:-1]) or 0
        if best_url is None or width > best_width:
            best_url = url
            best_width = width
    return best_url


def _best_image_url_from_tag(tag: Any) -> Optional[str]:
    """從 img 標籤常見屬性裡找出最適合的圖片網址。"""
    for attr in ("srcset", "data-srcset"):
        best = _best_src_from_srcset(tag.get(attr) or "")
        if best:
            return best

    for attr in ("src", "data-src", "data-lazy-src", "data-image-src", "data-full-res-src"):
        value = tag.get(attr)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _video_mime_from_url(url: str) -> Optional[str]:
    """用副檔名粗略推斷影片 MIME type。"""
    path = unquote(urlparse(url or "").path).lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith((".mp4", ".m4v")):
        return "video/mp4"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".mov"):
        return "video/quicktime"
    return None


def _direct_video(raw_url: Any, width=None, height=None) -> Optional[ThreadsMedia]:
    """Accept a complete, trusted media URL; preserve its signed query verbatim."""
    from .download import is_trusted_media_url

    if not isinstance(raw_url, str) or len(raw_url) > 8192:
        return None
    url = html.unescape(re.sub(r"\\u0026", "&", raw_url.strip(), flags=re.IGNORECASE).replace(r"\/", "/"))
    if not is_trusted_media_url(url):
        return None
    mime = _video_mime_from_url(url)
    if mime not in {"video/mp4", "video/webm", "video/quicktime"}:
        return None
    return ThreadsMedia("video", url, _safe_int(width), _safe_int(height), mime=mime)


def first_direct_video(media: List[ThreadsMedia]) -> Optional[ThreadsMedia]:
    """Keep post order; HLS manifests and local blob URLs are not inline videos."""
    for item in media:
        if item.type == "video":
            video = _direct_video(item.url, item.width, item.height)
            if video:
                return video
    return None


def _post_code(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    try:
        clean = _strip_tracking_query(url)
    except ValueError:
        return None
    return urlparse(clean).path.rsplit("/", 1)[-1] if THREADS_RE.fullmatch(clean) else None


def _structured_page_error(url: str, soup: Any) -> Optional[str]:
    """Identify the active post's restriction route without matching UI wording."""
    target = _post_code(url)
    if not target:
        return None
    remaining = 20000
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or ""
        if len(raw) > MAX_EMBED_HTML_BYTES or "initialRouteInfo" not in raw:
            continue
        try:
            stack = [(json.loads(raw), 0)]
        except (ValueError, RecursionError):
            continue
        while stack and remaining > 0:
            node, depth = stack.pop()
            remaining -= 1
            if depth > 40:
                continue
            if isinstance(node, dict):
                info = node.get("initialRouteInfo")
                route = info.get("route") if isinstance(info, dict) else None
                if isinstance(route, dict) and isinstance(route.get("url"), str):
                    route_url = urljoin(url, route["url"])
                    root = route.get("rootView")
                    entry = root.get("entryPoint") if isinstance(root, dict) else None
                    if (_post_code(route_url) == target and isinstance(entry, dict)
                            and entry.get("__dr") == "BarcelonaGeoBlockedErrorRoot.entrypoint"):
                        return "restricted_content"
                stack.extend((child, depth + 1) for child in node.values() if isinstance(child, (dict, list)))
            elif isinstance(node, list):
                stack.extend((child, depth + 1) for child in node if isinstance(child, (dict, list)))
    return None


def _best_structured_image(node: dict) -> Optional[ThreadsMedia]:
    """Pick one rendition of an already post-bound image, never a profile image."""
    from .download import is_trusted_media_url

    versions = node.get("image_versions2") or node.get("image_versions") or {}
    candidates = versions.get("candidates", []) if isinstance(versions, dict) else versions
    images = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        raw_url = candidate.get("url")
        if not isinstance(raw_url, str) or len(raw_url) > 8192:
            continue
        url = html.unescape(re.sub(r"\\u0026", "&", raw_url.strip(), flags=re.IGNORECASE).replace(r"\/", "/"))
        if not is_trusted_media_url(url):
            continue
        image = ThreadsMedia("image", url, _safe_int(candidate.get("width")), _safe_int(candidate.get("height")))
        if not _is_probable_fallback_avatar(image):
            images.append(image)
    return max(images, key=lambda item: (item.width or 0) * (item.height or 0)) if images else None


def _structured_media_spoiler(node: dict) -> bool:
    info = node.get("text_post_app_info")
    return (node.get("is_spoiler_media") is True or
            isinstance(info, dict) and info.get("is_spoiler_media") is True)


def _structured_spoiler_spans(node: dict) -> Optional[List[TextSpan]]:
    info = node.get("text_post_app_info")
    fragments = info.get("text_fragments") if isinstance(info, dict) else None
    fragments = fragments.get("fragments") if isinstance(fragments, dict) else None
    if not isinstance(fragments, list):
        return None
    def protected(fragment):
        style = fragment.get("styling_info") if isinstance(fragment, dict) else None
        return isinstance(style, dict) and style.get("is_spoiler") is True
    if not any(protected(fragment) for fragment in fragments):
        return None
    caption = node.get("caption")
    caption = caption.get("text") if isinstance(caption, dict) else caption
    if all(isinstance(fragment, dict) and isinstance(fragment.get("plaintext"), str) for fragment in fragments):
        spans = trim_text_spans([TextSpan(fragment["plaintext"], protected(fragment)) for fragment in fragments])
        if not isinstance(caption, str) or "".join(span.text for span in spans) == caption.strip():
            return spans
    # An incomplete set of known spoiler fragments must not expose the caption.
    return [TextSpan(caption.strip(), True)] if isinstance(caption, str) and caption.strip() else None


def _extract_structured_media(post: ThreadsPost, soup: Any) -> List[ThreadsMedia]:
    """Read target-bound media and native text spoilers, including linked IG media."""
    target = _post_code(post.url)
    media: List[ThreadsMedia] = []
    if not target:
        return media
    remaining_nodes = 20000
    media_keys = {"video_versions", "playable_url", "video_url", "carousel_media", "image_versions2", "image_versions", "text_post_app_info"}

    def visit(node, owner=None, depth=0, attached_media=False, in_post_info=False, image_attachment=False, spoiler=False):
        nonlocal remaining_nodes
        if remaining_nodes <= 0 or depth > 40:
            return
        remaining_nodes -= 1
        if isinstance(node, list):
            for child in node:
                visit(child, owner, depth + 1, attached_media, in_post_info, image_attachment, spoiler)
            return
        if not isinstance(node, dict):
            return
        identity = next((_post_code(node.get(key)) for key in ("permalink", "url", "share_url", "post_url")
                         if _post_code(node.get(key))), None)
        if not identity and (media_keys.intersection(node) or re.search("post|thread|media", str(node.get("__typename", "")), re.I)):
            identity = next((node[key] for key in ("code", "shortcode", "media_code", "post_code", "thread_code", "permalink_code")
                             if isinstance(node.get(key), str) and re.fullmatch(r"[A-Za-z0-9_-]+", node[key])), None)
        owner = owner if attached_media and owner else identity or owner
        user = node.get("user") or {}
        if owner == target and isinstance(user, dict) and user.get("is_private") is True:
            return
        if identity == target and post.text_spans is None:
            spans = _structured_spoiler_spans(node)
            if spans:
                post.text_spans = spans
                post.text = "".join(span.text for span in spans)
        spoiler = owner == target and (spoiler or _structured_media_spoiler(node))
        children = node.get("carousel_media")
        has_carousel = isinstance(children, list) and bool(children)
        if owner == target and not has_carousel:
            candidates = []
            versions = node.get("video_versions")
            for rendition in versions if isinstance(versions, list) else []:
                if isinstance(rendition, str):
                    rendition = {"url": rendition}
                if not isinstance(rendition, dict):
                    continue
                video = _direct_video(rendition.get("url") or rendition.get("src") or rendition.get("playable_url"),
                                      rendition.get("width"), rendition.get("height"))
                if video:
                    candidates.append(((video.mime == "video/mp4", (video.width or 0) * (video.height or 0),
                                        _safe_int(rendition.get("bandwidth") or rendition.get("bitrate")) or 0), video))
            video = max(candidates, key=lambda item: item[0])[1] if candidates else next(
                (v for key in ("playable_url", "video_url") if (v := _direct_video(node.get(key)))), None
            )
            if video:
                video.spoiler = spoiler
                _append_media_unique(media, video)
            # Images must be on the post itself or an explicit carousel/linked
            # attachment node, not an arbitrary descendant that inherited owner.
            elif identity == target or image_attachment:
                image = _best_structured_image(node)
                if image:
                    image.spoiler = spoiler
                    _append_media_unique(media, image)
        for key, child in node.items():
            if key in {"video_versions", "playable_url", "video_url", "image_versions", "image_versions2", "author", "owner", "profile", "user"}:
                continue
            boundary = bool(re.search(r"parent_post|quoted|reply_to|repost|recommend|related|direct_replies", key, re.I))
            # A linked IG post has its own shortcode, but this exact attachment
            # edge binds it to the target Threads post. Quotes/replies still reset ownership.
            linked_inline = in_post_info and key == "linked_inline_media" and owner == target
            visit(child, None if boundary else owner, depth + 1,
                  not boundary and (attached_media or key == "carousel_media" or linked_inline),
                  key == "text_post_app_info" and owner == target and not boundary,
                  not boundary and (key == "carousel_media" or linked_inline), spoiler if not boundary else False)

    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or ""
        if len(raw) > MAX_EMBED_HTML_BYTES or not any(key in raw for key in ("video_versions", "playable_url", "video_url", "image_versions", "text_fragments")):
            continue
        try:
            visit(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    return media


def _is_probable_fallback_avatar(item: ThreadsMedia) -> bool:
    """判斷圖片是否屬於縮略頭像/預設 fallback，而非正文媒體。"""
    if _is_probable_profile_image(item):
        return True

    width = _safe_int(item.width)
    height = _safe_int(item.height)
    if width is None or height is None:
        width, height = _dimensions_from_url(item.url or "")

    if width is None or height is None:
        return False

    return width == height and width <= 240


def _has_only_fallback_media(items: List[ThreadsMedia]) -> bool:
    """檢查媒體列表是否只剩頭像型 fallback 圖片。"""
    if not items:
        return False

    saw_image = False
    for item in items:
        if item.type == "video":
            return False
        if item.type == "image":
            saw_image = True
            if not _is_probable_fallback_avatar(item):
                return False

    return saw_image


def _first(*vals):
    """回傳第一個非空白字串。"""
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _save_debug_html(name: str, text: str):
    """把最後抓到的 HTML 存到本地，方便手動追 parser 問題。"""
    try:
        os.makedirs("./_threads_debug", exist_ok=True)
        path = f"./_threads_debug/{name}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None


# =============== 解析 ===============
def _extract_dom_media(post: ThreadsPost, soup: Any) -> bool:
    """Read post-bound DOM in order; a video's sources are alternate renditions."""
    media_link_re = re.compile(r"/@[^/]+/post/[^/]+/media(?:[/?#]|$)")
    target = _post_code(post.url)
    scopes = [link for link in soup.find_all("a", href=True)
              if media_link_re.search(link.get("href") or "")
              and _post_code(urljoin(post.url, link.get("href")).split("/media", 1)[0]) == target]
    # Official embed markup places the permalink outside its media container.
    for root in soup.select(".Embed"):
        if any(_post_code(link.get("href")) == target for link in root.select(".LinkContainer a[href]")):
            scopes.extend(root.select(".SoloMediaContainer"))
    found = False
    seen_videos = set()

    def native_spoiler(tag, scope):
        # The exact official wrapper/overlay pair applies only to this media.
        current = tag
        while current is not None:
            if ("BlurredMediaContainer" in (current.get("class") or [])
                    and current.select_one(".SpoilerOverlay") is not None):
                return True
            if current is scope:
                break
            current = current.parent
        return False

    def append_video(tag, scope=None):
        nonlocal found
        if id(tag) in seen_videos:
            return
        seen_videos.add(id(tag))
        candidates = [tag.get("src")] + [source.get("src") for source in tag.find_all("source")]
        video = next((item for raw in candidates if (item := _direct_video(raw))), None)
        if video and scope is not None:
            video.spoiler = native_spoiler(tag, scope)
        if video and _append_media_unique(post.media, video):
            found = True

    for scope in scopes:
        for node in scope.find_all(["img", "video"]):
            if node.name == "video":
                append_video(node, scope)
            elif node.find_parent("video") is None:
                media_url = _best_image_url_from_tag(node)
                if media_url and _append_media_unique(post.media, ThreadsMedia(
                    "image", media_url, _safe_int(node.get("width")), _safe_int(node.get("height")), node.get("alt"),
                    spoiler=native_spoiler(node, scope),
                )):
                    found = True

    # Keep the existing src-based fallback for official download controls.
    for node in soup.find_all(src=True):
        embed = node.find_parent(class_="Embed")
        if embed is not None and not any(_post_code(link.get("href")) == target for link in embed.select(".LinkContainer a[href]")):
            continue
        owner_link = node.find_parent("a", href=media_link_re)
        if owner_link is not None and _post_code(urljoin(post.url, owner_link.get("href")).split("/media", 1)[0]) != target:
            continue
        video_tag = node if node.name == "video" else node.find_parent("video")
        if video_tag is not None:
            append_video(video_tag)
            continue
        media_url = (node.get("src") or "").strip()
        video = _direct_video(media_url)
        if video and _append_media_unique(post.media, video):
            found = True
    return found


def _looks_like_ui_text(text: str) -> bool:
    """過濾按鈕、footer、統計數字等非正文文字。"""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return True

    lowered = normalized.lower()
    if lowered in {
        "download",
        "instagram logo",
        "translate",
        "threads terms",
        "privacy policy",
        "cookies policy",
    }:
        return True

    if normalized in {"讚", "收回讚", "回覆", "轉發", "分享"}:
        return True

    if re.fullmatch(r"[©(]?\s*\d{4}\s*[)]?", normalized):
        return True

    if re.fullmatch(r"[\d\s/.,:]+", normalized):
        return True

    return False


def _node_name(node: Any) -> Optional[str]:
    """抽象化 DOM node.name 讀取，讓 fake DOM 也能共用。"""
    return getattr(node, "name", None)


def _node_get(node: Any, key: str, default: Any = None) -> Any:
    """安全讀取節點屬性。"""
    getter = getattr(node, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def _node_parents(node: Any) -> List[Any]:
    """安全取得父節點串列。"""
    parents = getattr(node, "parents", None)
    if parents is None:
        return []
    return list(parents)


def _node_children(node: Any) -> List[Any]:
    """安全取得子節點串列。"""
    children = getattr(node, "children", None)
    if children is None:
        return []
    return list(children)


def _node_text_for_filter(node: Any) -> str:
    """讀出節點純文字，供 UI 文案過濾用。"""
    get_text = getattr(node, "get_text", None)
    if callable(get_text):
        return get_text(" ", strip=True)
    return ""


def _attr_text(value: Any) -> str:
    """把屬性值轉成方便比對的 lowercase 字串。"""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(v).lower() for v in value)
    return str(value).lower() if value is not None else ""


def _strip_spoiler_markup(text: str) -> str:
    """移除 Discord spoiler 標記，方便做純文字比對。"""
    return (text or "").replace("||", "")


def _has_interactive_ancestor(node: Any) -> bool:
    """判斷節點是否位於按鈕/連結等互動元素內。"""
    interactive_roles = {"button", "link"}
    interactive_labels = {"video player", "instagram logo"}

    for parent in _node_parents(node):
        if _is_interactive_node(parent, interactive_roles, interactive_labels):
            return True

    return False


def _is_interactive_node(
    node: Any,
    interactive_roles: Optional[set[str]] = None,
    interactive_labels: Optional[set[str]] = None,
) -> bool:
    """判斷節點本身是否像按鈕、連結或影片播放器控制元件。"""
    if interactive_roles is None:
        interactive_roles = {"button", "link"}
    if interactive_labels is None:
        interactive_labels = {"video player", "instagram logo"}

    name = _node_name(node)
    if name in {"a", "button"}:
        return True

    role = _attr_text(_node_get(node, "role"))
    if role in interactive_roles:
        return True

    aria_label = _attr_text(_node_get(node, "aria-label"))
    if aria_label in interactive_labels:
        return True

    return False


def _has_dir_auto_ancestor(node: Any) -> bool:
    """避免巢狀 `dir=auto` 節點造成正文重複擷取。"""
    for parent in _node_parents(node):
        if _attr_text(_node_get(parent, "dir")) == "auto":
            return True
    return False


def _is_probable_spoiler_node(node: Any) -> bool:
    """Read explicit spoiler markers, not ordinary clipping/visibility styles."""
    if _attr_text(_node_get(node, "data-text-fragment")) == "spoiler":
        return True
    spoiler_tokens = [
        "spoiler",
        "劇透",
        "防雷",
        "tap to reveal",
        "show spoiler",
    ]

    for attr in ("aria-label", "title", "data-testid", "data-visualcompletion", "class"):
        value = _attr_text(_node_get(node, attr))
        if value and any(token in value for token in spoiler_tokens):
            return True

    return False


def _render_text_spans(node: Any, *, spoiler_active: bool = False) -> List[TextSpan]:
    """Keep native ranges as data; the Discord view owns Markdown escaping."""
    if isinstance(node, str):
        text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape(node).replace("\xa0", " "))
        return [TextSpan(text, spoiler_active)]

    name = _node_name(node)
    if name == "br":
        return [TextSpan("\n", spoiler_active)]
    if _is_interactive_node(node):
        # A native spoiler's reveal button wraps the actual text fragment.
        children = [child for child in _node_children(node)
                    if _attr_text(_node_get(child, "data-text-fragment")) == "spoiler"]
        if _attr_text(_node_get(node, "data-text-fragment")) != "spoiler":
            return coalesce_text_spans([span for child in children for span in _render_text_spans(child)])

    node_is_spoiler = _is_probable_spoiler_node(node)
    next_spoiler_state = spoiler_active or node_is_spoiler

    parts: List[TextSpan] = []
    for child in _node_children(node):
        parts.extend(_render_text_spans(child, spoiler_active=next_spoiler_state))
    return coalesce_text_spans(parts)


def _render_text_subtree(node: Any, *, spoiler_active: bool = False) -> str:
    """Compatibility renderer for callers needing Markdown rather than spans."""
    return "".join(f"||{span.text}||" if span.spoiler else span.text
                   for span in _render_text_spans(node, spoiler_active=spoiler_active))


def _extract_dom_text(post: ThreadsPost, soup: Any) -> bool:
    """從 DOM 的 `span[dir='auto']` 區塊抽出貼文正文。"""
    author_tokens = {
        token.lower()
        for token in [post.author_username, post.author_name]
        if isinstance(token, str) and token.strip()
    }
    text_blocks: List[str] = []
    text_spans: List[TextSpan] = []
    seen_plain_blocks = set()

    for node in soup.select("span[dir='auto']"):
        if _has_dir_auto_ancestor(node):
            continue
        if _has_interactive_ancestor(node):
            continue

        filter_text = re.sub(r"\s+", " ", _node_text_for_filter(node)).strip()
        plain_filter_text = _strip_spoiler_markup(filter_text)
        if _looks_like_ui_text(plain_filter_text):
            continue
        if plain_filter_text.lower().lstrip("@") in author_tokens:
            continue

        spans = trim_text_spans(_render_text_spans(node))
        rendered = "".join(span.text for span in spans)
        plain_rendered = re.sub(r"\s+", " ", _strip_spoiler_markup(rendered)).strip()
        if not plain_rendered or _looks_like_ui_text(plain_rendered):
            continue
        if plain_rendered.lower().lstrip("@") in author_tokens:
            continue
        if plain_rendered in seen_plain_blocks:
            continue

        seen_plain_blocks.add(plain_rendered)
        if text_blocks:
            text_spans.append(TextSpan("\n"))
        text_spans.extend(spans)
        text_blocks.append(rendered)

    if text_blocks:
        post.text = "\n".join(text_blocks)
        if any(span.spoiler for span in text_spans):
            post.text_spans = coalesce_text_spans(text_spans)
        return True

    return False


def _parse_html(url: str, html_text: str) -> ThreadsPost:
    """整合 JSON-LD、meta 與 DOM heuristic，產生完整 `ThreadsPost`。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    post = ThreadsPost(url=url)
    page_error = _structured_page_error(url, soup)
    if page_error:
        post.preview_error = page_error
        return post
    explicit_media_found = False

    # Post-bound structured data precedes page-wide DOM/meta fallbacks.
    structured_media = _extract_structured_media(post, soup)
    post.media.extend(structured_media)
    explicit_media_found = bool(structured_media)
    if first_direct_video(structured_media):
        post.debug["video_source"] = "structured_json"

    # 作者 @ 從 URL 推回
    try:
        segs = urlparse(url).path.strip("/").split("/")
        if segs and segs[0].startswith("@"):
            post.author_username = segs[0][1:]
    except Exception:
        pass

    # JSON-LD
    jsonlds = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            import json
            data = json.loads(tag.string or "{}")
            if isinstance(data, dict):
                jsonlds.append(data)
            elif isinstance(data, list):
                jsonlds.extend([d for d in data if isinstance(d, dict)])
        except Exception:
            pass

    for node in jsonlds:
        jtype = node.get("@type") or node.get("type")
        jt = ",".join(jtype) if isinstance(jtype, list) else (jtype or "")
        if any(k in jt.lower() for k in ["socialmediaposting", "creativework", "article", "blogposting"]):
            post.text = _first(post.text, node.get("articleBody"), node.get("text"), node.get("description"))
            post.created_at = _first(post.created_at, node.get("datePublished"), node.get("dateCreated"))
            author = node.get("author") or node.get("creator")
            if isinstance(author, dict):
                post.author_name = _first(post.author_name, author.get("name"))
                same_as = author.get("sameAs") or author.get("url")
                if isinstance(same_as, str) and "/@" in same_as:
                    uname = same_as.rsplit("/@", 1)[-1].split("/", 1)[0]
                    post.author_username = _first(post.author_username, uname)

            # image
            imgs = node.get("image")
            if isinstance(imgs, dict):
                imgs = [imgs]
            if isinstance(imgs, list):
                for im in imgs:
                    if isinstance(im, str):
                        if _append_media_unique(post.media, ThreadsMedia("image", im)):
                            explicit_media_found = True
                    elif isinstance(im, dict):
                        u = im.get("url") or im.get("contentUrl") or im.get("thumbnailUrl")
                        if u:
                            if _append_media_unique(
                                post.media,
                                ThreadsMedia(
                                    "image",
                                    u,
                                    im.get("width"),
                                    im.get("height"),
                                    im.get("caption") or im.get("name"),
                                ),
                            ):
                                explicit_media_found = True
            # video
            vids = node.get("video")
            if isinstance(vids, dict):
                vids = [vids]
            if isinstance(vids, list):
                for v in vids:
                    if isinstance(v, dict):
                        u = v.get("contentUrl") or v.get("embedUrl") or v.get("url")
                        if u:
                            if _append_media_unique(post.media, ThreadsMedia("video", u, v.get("width"), v.get("height"))):
                                explicit_media_found = True

    dom_post = ThreadsPost(url=url)
    if _extract_dom_media(dom_post, soup):
        for item in dom_post.media:
            _append_media_unique(post.media, item)
        explicit_media_found = True

    # An embed's permalink binds its caption; native ranges beat flat metadata.
    for root in soup.select(".Embed"):
        if not any(_post_code(link.get("href")) == _post_code(url) for link in root.select(".LinkContainer a[href]")):
            continue
        caption = root.select_one(".BodyTextContainer")
        if caption is not None and post.text_spans is None:
            spans = trim_text_spans(_render_text_spans(caption))
            protected = any(span.spoiler for span in spans)
            if spans and (protected or not post.text):
                post.text = "".join(span.text for span in spans)
                post.text_spans = spans if protected else None
        break
    if post.text_spans is None:
        dom_text = ThreadsPost(url=url, author_username=post.author_username, author_name=post.author_name)
        if _extract_dom_text(dom_text, soup):
            same_text = " ".join((post.text or "").split()) == " ".join((dom_text.text or "").split())
            if not post.text or (dom_text.text_spans and same_text):
                post.text, post.text_spans = dom_text.text, dom_text.text_spans

    # __NEXT_DATA__ 或其他 script（某些版本會塞不同 id）
    next_node = soup.find("script", id="__NEXT_DATA__")
    if next_node and next_node.string and not post.text:
        raw = next_node.string
        # 啟發式抓 caption/text
        m = re.search(r'"(caption|text|body|content)"\s*:\s*"(.+?)"', raw)
        if m:
            post.text = html.unescape(m.group(2))

    # og / twitter meta（多值）
    def metas(keys):
        vals = []
        for k in keys:
            for tag in soup.find_all("meta", attrs={"property": k}):
                c = tag.get("content")
                if c: vals.append(c)
            for tag in soup.find_all("meta", attrs={"name"
                                                    "": k}):
                c = tag.get("content")
                if c: vals.append(c)
        # 去重保序
        out, seen = [], set()
        for v in vals:
            v2 = v.strip()
            if v2 and v2 not in seen:
                seen.add(v2);
                out.append(v2)
        return out

    if not post.text:
        post.text = _first(*metas(["og:description", "description", "twitter:description"]))
    if not post.author_name:
        post.author_name = _first(*metas(["og:site_name", "twitter:creator"]))

    meta_image_width = _safe_int(_first(*metas(["og:image:width", "twitter:image:width"])))
    meta_image_height = _safe_int(_first(*metas(["og:image:height", "twitter:image:height"])))
    skipped_meta_images = 0
    for key in ["og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"]:
        for u in metas([key]):
            candidate = ThreadsMedia("image", u, meta_image_width, meta_image_height)
            if _is_probable_profile_image(candidate):
                skipped_meta_images += 1
                continue
            # 純文字貼文有時只會帶作者頭像等 fallback 縮圖，這種情況直接略過
            if post.text and not explicit_media_found and _is_probable_fallback_avatar(candidate):
                skipped_meta_images += 1
                continue
            _append_media_unique(post.media, candidate)
    if skipped_meta_images:
        post.debug["skipped_meta_image_count"] = skipped_meta_images
    for key in ["og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"]:
        for u in metas([key]):
            mime = _video_mime_from_url(u)
            _append_media_unique(post.media, ThreadsMedia("video", u, mime=mime))

    if post.text and post.text_spans is None:
        post.text = html.unescape(post.text).strip()

    if structured_media:
        # Post-bound carousel order is authoritative. Page-wide DOM/OG may
        # repeat its first image or contain a video's poster and renditions.
        post.media = structured_media
        post.debug["media_source"] = "structured_json"
    elif dom_post.media:
        post.media = dom_post.media
    elif video := first_direct_video(post.media):
        # Unordered OG/JSON-LD images beside a video are normally cover art.
        post.media = [video]
    return post


# =============== 抓取（requests） ===============
def _try_requests(url: str) -> ThreadsPost:
    """用 requests + 多 UA/host 重試抓 Threads 頁面。"""
    import requests

    headers_list = [
        {"User-Agent": UA_DESKTOP, "Accept": DEFAULT_ACCEPT, "Accept-Language": DEFAULT_LANG},
        {"User-Agent": UA_MOBILE, "Accept": DEFAULT_ACCEPT, "Accept-Language": DEFAULT_LANG},
        {"User-Agent": UA_INSTAGRAM, "Accept": DEFAULT_ACCEPT, "Accept-Language": DEFAULT_LANG},
    ]

    variants = build_candidate_urls(url)

    last_html = None
    last_status = None
    last_url = None
    last_threads_error = None
    # Cover every URL and all UAs first, then retry transient primary failures.
    # Permanent failures can use alternate UAs; every path shares one budget.
    primary_pairs = [(vurl, index % len(headers_list)) for index, vurl in enumerate(variants)]
    alternate_pairs = [
        (vurl, header_index)
        for header_index in range(len(headers_list))
        for vurl in variants
        if (vurl, header_index) not in primary_pairs
    ]
    plan = [
        (vurl, header_index, retry_index)
        for pairs in (primary_pairs, alternate_pairs)
        for retry_index in range(RETRY)
        for vurl, header_index in pairs
    ]
    completed_pairs = set()
    attempts = 0
    for index, (vurl, header_index, retry_index) in enumerate(plan):
        if attempts >= MAX_PAGE_ATTEMPTS:
            break
        pair = (vurl, header_index)
        if pair in completed_pairs:
            continue
        attempts += 1
        retryable = False
        try:
            resp = requests.get(vurl, headers=headers_list[header_index], timeout=REQUEST_TIMEOUT, allow_redirects=True)
            last_status = resp.status_code
            last_url = resp.url
            redirect_error = _threads_error_from_url(resp.url)
            if redirect_error:
                last_threads_error = redirect_error
                completed_pairs.add(pair)
            elif resp.status_code == 200 and "<html" in resp.text.lower():
                post = _parse_html(_strip_tracking_query(resp.url), resp.text)
                if post.preview_error:
                    # A verified access restriction is not post text, and UA
                    # retries should not keep presenting that error page as content.
                    post.debug["requests_attempts"] = attempts
                    return post
                if post.text or post.media:
                    return post
                last_html = resp.text
                completed_pairs.add(pair)
            elif resp.status_code in (429, 500, 502, 503, 504):
                retryable = True
            else:
                last_html = resp.text
                completed_pairs.add(pair)
        except Exception:
            retryable = True
        if retryable and attempts < MAX_PAGE_ATTEMPTS and index + 1 < len(plan):
            time.sleep(RETRY_BACKOFF * (retry_index + 1))
    # 全部失敗，回傳空 post + debug
    p = ThreadsPost(url=_normalize(url))
    p.preview_error = last_threads_error
    p.debug["requests_last_status"] = last_status
    p.debug["requests_last_url"] = last_url
    p.debug["requests_attempts"] = attempts
    if last_html and not p.preview_error:
        path = _save_debug_html("requests_last.html", last_html)
        p.debug["requests_last_html_path"] = path
    return p


def _try_embed_fill(post: ThreadsPost):
    """One anonymous request to the official embed page; never fetch video bytes."""
    import requests
    from bs4 import BeautifulSoup

    code = _post_code(post.url)
    if not code:
        return
    embed_url = "https://www.threads.com" + urlparse(_strip_tracking_query(post.url)).path + "/embed"
    response = None
    post.debug["embed_attempts"] = 1
    try:
        response = requests.get(embed_url, headers={"User-Agent": UA_DISCORD, "Accept": DEFAULT_ACCEPT},
                                timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True)
        post.debug["embed_status"] = response.status_code
        if response.status_code != 200:
            return
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_EMBED_HTML_BYTES:
                post.debug["embed_error"] = "html_too_large"
                return
        soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")
        # Official embed DOM provides a post permalink outside the media itself.
        root = next((container for container in soup.select(".Embed")
                     if any(_post_code(a.get("href")) == code for a in container.select(".LinkContainer a[href]"))), None)
        if root is None:
            post.debug["embed_error"] = "post_not_found"
            return
        embedded = _parse_html(post.url, str(root))
        video = first_direct_video(embedded.media)
        if video:
            post.media = embedded.media
            post.debug["video_source"] = "official_embed"
        else:
            for item in post.media:
                for candidate in embedded.media:
                    if item.type == candidate.type and (item.url == candidate.url or len(post.media) == len(embedded.media) == 1):
                        item.spoiler = item.spoiler or candidate.spoiler
        if embedded.text_spans or not post.text:
            post.text, post.text_spans = embedded.text, embedded.text_spans
        if not post.author_username:
            post.author_username = embedded.author_username
        if not post.author_name:
            author = root.select_one(".AuthorIdentity .HeaderLink")
            if author:
                post.author_name = author.get_text(strip=True) or None
    except Exception as exc:
        # Only diagnostic categories; never log signed CDN URLs or page contents.
        post.debug["embed_error"] = type(exc).__name__
    finally:
        if response is not None:
            response.close()


# =============== oEmbed 後援（輕量） ===============
def _try_oembed_fill(post: ThreadsPost, *, allow_thumbnail: bool = True):
    """當正文或媒體不足時，用 oEmbed 補作者名稱與縮圖。"""
    import requests

    try:
        qs = urlencode({"url": post.url, "omitscript": "1"})
        oembed_url = f"https://www.threads.net/oembed?{qs}"
        resp = requests.get(oembed_url, headers={"User-Agent": UA_DESKTOP, "Accept": "application/json",
                                                 "Accept-Language": DEFAULT_LANG}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            o = resp.json()
            post.oembed_html = o.get("html")
            post.author_name = _first(post.author_name, o.get("author_name"))
            thumb = o.get("thumbnail_url")
            if allow_thumbnail and thumb:
                _append_media_unique(post.media, ThreadsMedia("image", thumb))
    except Exception:
        pass

# =============== 外部主函數 ===============
def fetch_threads_post(url: str) -> ThreadsPost:
    """Threads parser 對外主入口。"""
    clean_url = _strip_tracking_query(url)
    if THREADS_SHARE_RE.fullmatch(clean_url):
        clean_url = _resolve_threads_share_url(clean_url)

    # 1) requests（多 UA、多網域變體）
    post = _try_requests(clean_url)

    if (not post.preview_error and post.debug.get("media_source") != "structured_json"
            and not first_direct_video(post.media)):
        _try_embed_fill(post)

    # 2) 不足 → oEmbed 補資料（作者名、縮圖、嵌入 HTML）
    if not post.preview_error and (not post.text or not post.media):
        _try_oembed_fill(post, allow_thumbnail=not post.text and not post.media)

    # 3) 完全關閉 Playwright，僅保留 requests / oEmbed 結果。
    # 若最後只剩可疑 fallback/avatar 圖，直接清掉，避免純文字貼文誤帶頭貼。
    if _has_only_fallback_media(post.media):
        post.media = []

    # Optional account state is restricted to one separate, post-bound request.
    # Never spend credentials to improve an already usable anonymous preview.
    if not post.text and not post.media:
        cookie = os.environ.get("THREADS_COOKIE", "")
        if cookie.strip():
            from .threads_cookie import try_cookie_fallback
            authenticated = try_cookie_fallback(clean_url, cookie)
            authenticated.debug = {**post.debug, **authenticated.debug}
            post = authenticated

    return post


# =============== CLI ===============
def main():
    """本地 CLI 除錯入口，輸出解析結果 JSON。"""
    if len(sys.argv) < 2:
        print("用法：python threads_fetch.py <Threads貼文URL>")
        sys.exit(1)
    url = sys.argv[1]
    post = fetch_threads_post(url)
    out = {
        "url": post.url,
        "author_username": post.author_username,
        "author_name": post.author_name,
        "text": post.text,
        "created_at": post.created_at,
        "media": [m.__dict__ for m in post.media],
        "oembed_html": post.oembed_html,
        "debug": post.debug,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
