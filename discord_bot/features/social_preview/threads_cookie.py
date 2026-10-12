"""One opt-in authenticated request, with post-bound parsing and safe diagnostics."""
from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import urlparse

from .cookie_store import prepare_cookie, response_cookies, save_response_cookies
from .download import is_trusted_media_url
from .threads_fetch import (
    DEFAULT_ACCEPT, DEFAULT_LANG, MAX_EMBED_HTML_BYTES, REQUEST_TIMEOUT,
    THREADS_RE, THREADS_REDIRECT_STATUSES, UA_MOBILE, ThreadsPost,
    _append_media_unique, _best_structured_image, _direct_video, _has_only_fallback_media, _parse_html,
    _post_code, _strip_tracking_query, _structured_page_error,
    _structured_media_spoiler, _structured_spoiler_spans,
)

logger = logging.getLogger("discord_digest_bot")
MAX_COOKIE_BYTES = 16 * 1024
MAX_COOKIE_PARSE_NODES = 100000


def _valid_cookie(cookie: str) -> bool:
    if len(cookie) > MAX_COOKIE_BYTES or any(ord(c) < 32 or ord(c) >= 127 for c in cookie):
        return False
    for pair in cookie.strip().split(";"):
        name, separator, _value = pair.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            return False
    return True


def _node_code(node: dict) -> str | None:
    for key in ("url", "permalink", "post_url", "share_url", "@id", "mainEntityOfPage"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        code = _post_code(value)
        if code:
            return code
    for key in ("code", "shortcode", "media_code", "post_code", "thread_code", "permalink_code"):
        if isinstance(node.get(key), str):
            return node[key]
    return None


def _read_post_node(url: str, node: dict, *, json_ld: bool) -> ThreadsPost:
    """Read direct fields and carousel children; never harvest quoted/reply posts."""
    user = node.get("user") or node.get("author") or {}
    if isinstance(user, dict) and user.get("is_private") is True:
        return ThreadsPost(url=url, preview_error="cookie_no_content", debug={"cookie_reason": "private_post"})
    if json_ld:
        # Feed only the matched node to the existing JSON-LD parser, without any
        # page DOM/meta or other scripts from the authenticated response.
        raw = json.dumps(node).replace("<", "\\u003c")
        post = _parse_html(url, '<script type="application/ld+json">' + raw + '</script>')
        post.media = [m for m in post.media if is_trusted_media_url(m.url)]
        return post

    post = ThreadsPost(url=url, author_username=urlparse(url).path.split("/")[1].lstrip("@"))
    if isinstance(user, dict):
        name = user.get("full_name") or user.get("name")
        post.author_name = name if isinstance(name, str) else None
    caption = node.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    if isinstance(caption, str):
        post.text = html.unescape(caption).strip() or None
    post.text_spans = _structured_spoiler_spans(node)
    if post.text_spans:
        post.text = "".join(span.text for span in post.text_spans)
    media_nodes = []

    def add_media(media, depth=0, spoiler=False):
        # Only this explicit attachment edge is traversed. Quotes, replies and
        # recommendations inside a media object are not part of the carousel.
        if not isinstance(media, dict) or depth > 40 or len(media_nodes) >= 1000:
            return
        owner = media.get("user") or {}
        if isinstance(owner, dict) and owner.get("is_private") is True:
            return
        spoiler = spoiler or _structured_media_spoiler(media)
        children = media.get("carousel_media")
        if isinstance(children, list) and children:
            for child in children:
                add_media(child, depth + 1, spoiler)
        else:
            media_nodes.append((media, spoiler))

    add_media(node)
    info = node.get("text_post_app_info")
    linked = info.get("linked_inline_media") if isinstance(info, dict) else None
    if isinstance(linked, dict):
        add_media(linked, spoiler=_structured_media_spoiler(node))
    for media, spoiler in media_nodes:
        if not isinstance(media, dict):
            continue
        versions = media.get("video_versions")
        videos = []
        for version in versions if isinstance(versions, list) else []:
            if isinstance(version, dict):
                video = _direct_video(version.get("url"), version.get("width"), version.get("height"))
                if video:
                    video.spoiler = spoiler
                    videos.append(video)
        if videos:
            _append_media_unique(post.media, max(videos, key=lambda v: (
                v.mime == "video/mp4", (v.width or 0) * (v.height or 0))))
            continue
        video = _direct_video(media.get("playable_url") or media.get("video_url"))
        if video:
            video.spoiler = spoiler
            _append_media_unique(post.media, video)
            continue
        image = _best_structured_image(media)
        if image:
            image.spoiler = spoiler
            _append_media_unique(post.media, image)
    return post


def _parse_cookie_html(url: str, body: str) -> ThreadsPost:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    failed = ThreadsPost(url=url, preview_error="cookie_no_content")
    metrics = {"cookie_json_count": 0, "cookie_parsed_json": 0, "cookie_matched_nodes": 0,
               "cookie_viewer_hint": "unknown"}
    failed.debug = metrics
    if _structured_page_error(url, soup):
        failed.preview_error = "cookie_restricted_content"
        return failed
    # Match rendered forms, not words embedded in the site's JavaScript bundles.
    if soup.select_one('input[type="password"], input[name="approvals_code"]') or any(
        re.search(r"/(?:login|accounts/login|challenge|checkpoint)(?:/|\?|$)", form.get("action", ""), re.I)
        for form in soup.find_all("form")
    ):
        failed.preview_error = "cookie_login_required"
        return failed
    target = _post_code(url)
    remaining = MAX_COOKIE_PARSE_NODES

    def matched_nodes(node, depth=0):
        nonlocal remaining
        # Bootstrap scalars cannot contain a post. The response is already
        # capped at 2 MiB; reserve this additional budget for containers.
        if not isinstance(node, (dict, list)):
            return
        if remaining <= 0 or depth > 40:
            return
        remaining -= 1
        if isinstance(node, dict):
            # Only an active-route actor is a viewer hint. Never retain its ID.
            if isinstance(node.get("rootView"), dict) and isinstance(node.get("url"), str):
                actor = node.get("actorID")
                if isinstance(actor, (str, int)) and str(actor).isdigit():
                    metrics["cookie_viewer_hint"] = "present" if int(actor) else "absent"
            if _node_code(node) == target:
                metrics["cookie_matched_nodes"] += 1
                yield node
                user = node.get("user") or node.get("author") or {}
                if isinstance(user, dict) and user.get("is_private") is True:
                    return
                # Routes and other wrappers can have the same URL as the post.
                # Continue looking for a separately bound child post.
            for value in node.values():
                yield from matched_nodes(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                yield from matched_nodes(value, depth + 1)

    scripts = soup.find_all("script", attrs={"type": ["application/json", "application/ld+json"]})
    # Try target-bearing payloads before unrelated site configuration. Keep all
    # other scripts as candidates because a shortcode can be JSON-escaped.
    scripts.sort(key=lambda script: target not in (script.string or ""))
    for script in scripts:
        metrics["cookie_json_count"] += 1
        try:
            data = json.loads(script.string or "")
            metrics["cookie_parsed_json"] += 1
            for node in matched_nodes(data):
                post = _read_post_node(url, node, json_ld=script.get("type") == "application/ld+json")
                if post.debug.get("cookie_reason") == "private_post":
                    metrics["cookie_reason"] = "private_post"
                if _has_only_fallback_media(post.media):
                    post.media = []
                if not post.preview_error and (post.text or post.media):
                    post.debug.update(metrics)
                    post.debug["cookie_reason"] = "target_found"
                    return post
        except (ValueError, TypeError, RecursionError):
            continue
    metrics.setdefault("cookie_reason", "parse_budget_exhausted" if remaining <= 0 else
                       "json_missing" if not metrics["cookie_json_count"] else
                       "json_invalid" if not metrics["cookie_parsed_json"] else
                       "target_not_found" if not metrics["cookie_matched_nodes"] else "target_has_no_content")
    return failed


def try_cookie_fallback(url: str, cookie: str, *, persist_cookies: bool = True) -> ThreadsPost:
    """Never propagate credential-bearing request exceptions, bodies or redirects."""
    import requests

    result = ThreadsPost(url=url, preview_error="cookie_fetch_failed", debug={"cookie_attempts": 0})
    response = None
    status = None
    try:
        clean = _strip_tracking_query(url)
        if not THREADS_RE.fullmatch(clean):
            return result
        target_url = "https://www.threads.com" + urlparse(clean).path
        result.url = target_url
        if not _valid_cookie(cookie):
            result.preview_error = "cookie_config_invalid"
            return result
        snapshot = prepare_cookie("threads", target_url, cookie, persist=persist_cookies)
        if not snapshot.header:
            result.preview_error = "cookie_login_required"
            return result
        with requests.Session() as session:
            # Isolate this request from ambient .netrc credentials, proxies and
            # shared cookie jars; TLS verification remains enabled by default.
            session.trust_env = False
            result.debug["cookie_attempts"] = 1
            response = session.get(
                # The mobile hl=en variant supplies server-rendered post data;
                # the desktop response can be only an empty JavaScript shell.
                target_url + "?hl=en", headers={"User-Agent": UA_MOBILE, "Accept": DEFAULT_ACCEPT,
                                     "Accept-Language": DEFAULT_LANG, "Cookie": snapshot.header},
                timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
            )
            status = response.status_code
            if status in (401, 403):
                result.preview_error = "cookie_login_required"
                return result
            if status == 429:
                result.preview_error = "cookie_rate_limited"
                return result
            if status in THREADS_REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                if re.search(r"/(?:login|accounts/login|challenge|checkpoint)(?:/|\?|$)", location, re.I):
                    result.preview_error = "cookie_login_required"
                return result
            if status != 200:
                return result
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                if len(data) + len(chunk) > MAX_EMBED_HTML_BYTES:
                    result.debug["cookie_reason"] = "response_too_large"
                    return result
                data.extend(chunk)
            parsed = _parse_cookie_html(target_url, data.decode("utf-8", errors="replace"))
            parsed.debug["cookie_attempts"] = 1
            parsed.debug["cookie_response_bytes"] = len(data)
            result = parsed
            if not result.preview_error and (result.text or result.media):
                raw_headers = getattr(getattr(response, "raw", None), "headers", None)
                save_response_cookies(snapshot, response_cookies(raw_headers if raw_headers is not None else response.headers))
            return result
    except Exception:
        # Even a timeout/parse exception may embed request headers or login HTML.
        return result
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if status is not None:
            result.debug["cookie_status"] = status
        result.debug["cookie_result"] = result.preview_error or "success"
        if result.preview_error:
            logger.warning("Threads cookie fallback stopped: %s; status=%s reason=%s bytes=%s json=%s matched=%s viewer=%s",
                           result.preview_error, status, result.debug.get("cookie_reason", result.preview_error),
                           result.debug.get("cookie_response_bytes", 0), result.debug.get("cookie_json_count", 0),
                           result.debug.get("cookie_matched_nodes", 0), result.debug.get("cookie_viewer_hint", "unknown"))
