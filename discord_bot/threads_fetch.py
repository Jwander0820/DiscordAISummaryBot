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
from urllib.parse import urlparse, urlunparse, urlencode
from playwright.sync_api import sync_playwright

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
    debug: Dict[str, Any] = field(default_factory=dict)


# =============== 小工具 ===============
# --- 新增：從環境變數取得瀏覽器 args（預設必要時才加） ---
def _browser_args_from_env() -> list[str]:
    # 以逗號分隔，讓你完全自訂；未設定就回空
    raw = os.getenv("THREADS_BROWSER_ARGS", "")
    args = [a.strip() for a in raw.split(",") if a.strip()]
    # 若設 THREADS_USE_NO_SANDBOX=1，注入常見兩個旗標
    if os.getenv("THREADS_USE_NO_SANDBOX", "0") == "1":
        # 去重：避免重覆加入
        if "--no-sandbox" not in args:
            args.append("--no-sandbox")
        if "--disable-dev-shm-usage" not in args:
            args.append("--disable-dev-shm-usage")

    print(args)
    return args


def build_candidate_urls(url: str) -> list[str]:
    p = urlparse(url)
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
    if not THREADS_RE.match(url):
        raise ValueError(f"不是 Threads 貼文 URL：{url}")
    p = urlparse(url)
    scheme = p.scheme or "https"
    path = p.path.rstrip("/")
    # 先強制 threads.net，加 hl=en 提升 meta 完整度
    return urlunparse((scheme, "threads.net", path, "", "hl=en", ""))


def _variants(url: str) -> List[str]:
    p = urlparse(url)
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


def _is_probable_profile_image(item: ThreadsMedia) -> bool:
    if item.type != "image":
        return False

    url = (item.url or "").lower()
    if any(token in url for token in ["profile_pic", "profile_media", "avatar"]):
        return True
    if re.search(r"/t51\.2885-19/", url):
        return True

    alt = (item.alt or "").lower()
    if alt and any(token in alt for token in ["profile picture", "頭像", "大頭照"]):
        return True

    return False


def _append_media_unique(lst: List[ThreadsMedia], item: ThreadsMedia):
    if not item.url:
        return
    if _is_probable_profile_image(item):
        return
    if any(m.url == item.url for m in lst):
        return
    lst.append(item)


def _first(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _save_debug_html(name: str, text: str):
    try:
        os.makedirs("./_threads_debug", exist_ok=True)
        path = f"./_threads_debug/{name}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None


# =============== 解析 ===============
def _parse_html(url: str, html_text: str) -> ThreadsPost:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    post = ThreadsPost(url=url)

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
                        _append_media_unique(post.media, ThreadsMedia("image", im))
                    elif isinstance(im, dict):
                        u = im.get("url") or im.get("contentUrl") or im.get("thumbnailUrl")
                        if u:
                            _append_media_unique(post.media, ThreadsMedia("image", u, im.get("width"), im.get("height"),
                                                                          im.get("caption") or im.get("name")))
            # video
            vids = node.get("video")
            if isinstance(vids, dict):
                vids = [vids]
            if isinstance(vids, list):
                for v in vids:
                    if isinstance(v, dict):
                        u = v.get("contentUrl") or v.get("embedUrl") or v.get("url")
                        if u:
                            _append_media_unique(post.media, ThreadsMedia("video", u, v.get("width"), v.get("height")))

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

    for key in ["og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"]:
        for u in metas([key]):
            _append_media_unique(post.media, ThreadsMedia("image", u))
    for key in ["og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"]:
        for u in metas([key]):
            mime = "application/vnd.apple.mpegurl" if u.endswith(".m3u8") else (
                "video/mp4" if u.endswith(".mp4") else None)
            _append_media_unique(post.media, ThreadsMedia("video", u, mime=mime))

    if post.text:
        post.text = html.unescape(post.text).strip()

    return post


# =============== 抓取（requests） ===============
def _try_requests(url: str) -> ThreadsPost:
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
    for vurl in variants:
        for hdr in headers_list:
            err = None
            for i in range(RETRY):
                try:
                    resp = requests.get(vurl, headers=hdr, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                    last_status = resp.status_code
                    last_url = resp.url
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
    p.debug["requests_last_status"] = last_status
    p.debug["requests_last_url"] = last_url
    if last_html:
        path = _save_debug_html("requests_last.html", last_html)
        p.debug["requests_last_html_path"] = path
    return p


# =============== oEmbed 後援（輕量） ===============
def _try_oembed_fill(post: ThreadsPost):
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
            if thumb:
                _append_media_unique(post.media, ThreadsMedia("image", thumb))
    except Exception:
        pass


# =============== Playwright 後援（重型） ===============
def _try_playwright(url: str) -> ThreadsPost:
    """
    需要：
      pip install playwright
      playwright install
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        # 未安裝就直接回空
        p = ThreadsPost(url=_normalize(url))
        p.debug["playwright"] = f"not available: {e}"
        return p

    browser_args = _browser_args_from_env()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=browser_args  # <= 關鍵
        )
        context = browser.new_context(
            user_agent=UA_MOBILE,  # 你的 UA
            locale="en-US",
        )
        page = context.new_page()
        target = _normalize(url)
        try:
            page.goto(target, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html_dump = page.content()
            final_url = page.url
        except Exception as e:
            context.close(); browser.close()
            p = ThreadsPost(url=_normalize(url))
            p.debug["playwright"] = f"goto failed: {e}"
            return p

        context.close()
        browser.close()

    return _parse_html(final_url, html_dump)


# =============== 外部主函數 ===============
def fetch_threads_post(url: str) -> ThreadsPost:
    # 1) requests（多 UA、多網域變體）
    post = _try_requests(url)

    # 2) 不足 → oEmbed 補資料（作者名、縮圖、嵌入 HTML）
    if (not post.text or not post.media):
        _try_oembed_fill(post)

    # 3) 還是不夠 → Playwright 渲染（需要你安裝）
    if (not post.text and not post.media):
        rendered = _try_playwright(url)
        # 合併（以渲染結果為主）
        if rendered.text:
            post.text = rendered.text
        if rendered.media:
            for m in rendered.media:
                _append_media_unique(post.media, m)
        post.debug.update(rendered.debug or {})

    return post


# =============== CLI ===============
def main():
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
