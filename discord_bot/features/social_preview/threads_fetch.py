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
from urllib.parse import parse_qs, urlparse, urlunparse, urlencode

# =============== 配置 ===============
REQUEST_TIMEOUT = 15
RETRY = 3
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

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_LANG = "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7"

THREADS_RE = re.compile(
    r"https?://(www\.)?(threads\.net|threads\.com)/@[^/]+/post/[A-Za-z0-9_\-]+/?",
    re.IGNORECASE,
)


# =============== 資料結構 ===============
@dataclass
class ThreadsMedia:
    type: str  # "image" | "video"
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    alt: Optional[str] = None
    mime: Optional[str] = None


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


# =============== 小工具 ===============
def _strip_tracking_query(url: str) -> str:
    """移除 Threads 分享連結常見的 tracking query 與尾端標點。"""
    # 連結包在 ||spoiler|| 時，regex 可能把結尾 || 一起吃進來
    parsed = urlparse(url.rstrip(").,>|"))
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))


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
    if any(m.url == item.url for m in lst):
        return False
    lst.append(item)
    return True


def _safe_int(value: Any) -> Optional[int]:
    """寬鬆地把值轉成 int；失敗回 `None`。"""
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
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
    path = urlparse(url or "").path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".mp4"):
        return "video/mp4"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".mov"):
        return "video/quicktime"
    return None


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
    """從已渲染 DOM 找圖片/影片，補足 JSON-LD 缺漏。"""
    media_link_re = re.compile(r"/@[^/]+/post/[^/]+/media(?:[/?#]|$)")
    found = False

    for link in soup.find_all("a", href=True):
        href = link.get("href") or ""
        if not media_link_re.search(href):
            continue

        for img in link.find_all("img"):
            media_url = _best_image_url_from_tag(img)
            if not media_url:
                continue

            item = ThreadsMedia(
                "image",
                media_url,
                _safe_int(img.get("width")),
                _safe_int(img.get("height")),
                img.get("alt"),
            )
            if _append_media_unique(post.media, item):
                found = True

        for video in link.find_all("video"):
            video_url = video.get("src")
            if isinstance(video_url, str) and video_url.strip():
                mime = _video_mime_from_url(video_url.strip())
                if _append_media_unique(post.media, ThreadsMedia("video", video_url.strip(), mime=mime)):
                    found = True
            for source in video.find_all("source"):
                video_url = source.get("src")
                if isinstance(video_url, str) and video_url.strip():
                    mime = source.get("type") or _video_mime_from_url(video_url.strip())
                    if _append_media_unique(post.media, ThreadsMedia("video", video_url.strip(), mime=mime)):
                        found = True

    # Threads/Instagram 的影片有時不會出現在 <video> 標籤，而是藏在下載按鈕的 src。
    for node in soup.find_all(src=True):
        media_url = (node.get("src") or "").strip()
        mime = _video_mime_from_url(media_url)
        if not mime:
            continue
        if _append_media_unique(post.media, ThreadsMedia("video", media_url, mime=mime)):
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
    """用 aria/class/style 啟發式判斷節點是否帶 spoiler 效果。"""
    spoiler_tokens = [
        "spoiler",
        "劇透",
        "防雷",
        "tap to reveal",
        "show spoiler",
        "reveal",
    ]
    style_tokens = ["blur", "mask", "filter", "hidden"]

    for attr in ("aria-label", "title", "data-testid", "data-visualcompletion", "class"):
        value = _attr_text(_node_get(node, attr))
        if value and any(token in value for token in spoiler_tokens):
            return True

    style = _attr_text(_node_get(node, "style"))
    if style and any(token in style for token in style_tokens):
        return True

    return False


def _render_text_subtree(node: Any, *, spoiler_active: bool = False) -> str:
    """遞迴渲染正文子樹，保留換行並盡量維持 spoiler 區塊。"""
    if isinstance(node, str):
        return html.unescape(node)

    name = _node_name(node)
    if name == "br":
        return "\n"
    if _is_interactive_node(node):
        return ""

    node_is_spoiler = _is_probable_spoiler_node(node)
    next_spoiler_state = spoiler_active or node_is_spoiler

    parts: List[str] = []
    for child in _node_children(node):
        rendered = _render_text_subtree(child, spoiler_active=next_spoiler_state)
        if rendered:
            parts.append(rendered)

    text = "".join(parts)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if node_is_spoiler and not spoiler_active:
        stripped = text.strip()
        if stripped:
            return f"||{stripped}||"
        return ""

    return text


def _extract_dom_text(post: ThreadsPost, soup: Any) -> bool:
    """從 DOM 的 `span[dir='auto']` 區塊抽出貼文正文。"""
    author_tokens = {
        token.lower()
        for token in [post.author_username, post.author_name]
        if isinstance(token, str) and token.strip()
    }
    text_blocks: List[str] = []
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

        rendered = _render_text_subtree(node).strip()
        rendered = re.sub(r" *\n *", "\n", rendered)
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)
        plain_rendered = re.sub(r"\s+", " ", _strip_spoiler_markup(rendered)).strip()
        if not plain_rendered or _looks_like_ui_text(plain_rendered):
            continue
        if plain_rendered.lower().lstrip("@") in author_tokens:
            continue
        if plain_rendered in seen_plain_blocks:
            continue

        seen_plain_blocks.add(plain_rendered)
        text_blocks.append(rendered)

    if text_blocks:
        post.text = "\n".join(text_blocks)
        return True

    return False


def _parse_html(url: str, html_text: str) -> ThreadsPost:
    """整合 JSON-LD、meta 與 DOM heuristic，產生完整 `ThreadsPost`。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    post = ThreadsPost(url=url)
    explicit_media_found = False

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

    if _extract_dom_media(post, soup):
        explicit_media_found = True

    if not post.text:
        _extract_dom_text(post, soup)

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

    if post.text:
        post.text = html.unescape(post.text).strip()

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
    for vurl in variants:
        for hdr in headers_list:
            err = None
            for i in range(RETRY):
                try:
                    resp = requests.get(vurl, headers=hdr, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                    last_status = resp.status_code
                    last_url = resp.url
                    redirect_error = _threads_error_from_url(resp.url)
                    if redirect_error:
                        last_threads_error = redirect_error
                        break
                    if resp.status_code == 200 and "<html" in resp.text.lower():
                        post = _parse_html(resp.url, resp.text)
                        # 判斷是否已拿到有效資料
                        if post.text or post.media:
                            return post
                        # 即便沒撈到，也記錄一下 HTML 供 fallback 使用
                        last_html = resp.text
                        break
                    elif resp.status_code in (429, 500, 502, 503, 504):
                        time.sleep(RETRY_BACKOFF * (i + 1))
                    else:
                        # 例如 403/404… 記錄一下
                        last_html = resp.text
                        break
                except Exception as e:
                    err = e
                    time.sleep(RETRY_BACKOFF * (i + 1))
            # 這個 UA/URL 過完重試了，換下一個
    # 全部失敗，回傳空 post + debug
    p = ThreadsPost(url=_normalize(url))
    p.preview_error = last_threads_error
    p.debug["requests_last_status"] = last_status
    p.debug["requests_last_url"] = last_url
    if last_html and not p.preview_error:
        path = _save_debug_html("requests_last.html", last_html)
        p.debug["requests_last_html_path"] = path
    return p


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
    # 1) requests（多 UA、多網域變體）
    post = _try_requests(url)

    # 2) 不足 → oEmbed 補資料（作者名、縮圖、嵌入 HTML）
    if not post.preview_error and (not post.text or not post.media):
        _try_oembed_fill(post, allow_thumbnail=not post.text and not post.media)

    # 3) 完全關閉 Playwright，僅保留 requests / oEmbed 結果。
    # 若最後只剩可疑 fallback/avatar 圖，直接清掉，避免純文字貼文誤帶頭貼。
    if _has_only_fallback_media(post.media):
        post.media = []

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
