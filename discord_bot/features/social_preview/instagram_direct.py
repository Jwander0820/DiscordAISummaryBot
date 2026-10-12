"""Bounded Instagram reads with target-bound media and optional isolated login."""
from __future__ import annotations

import json
import logging
import os
import re
from time import perf_counter
from urllib.parse import urlparse

from .cookie_store import prepare_cookie, response_cookies, save_response_cookies
from .download import is_trusted_media_url
from .instagram_video_fallback import resolve_instagram_video_fallback
from .instagram_logging import elapsed_ms, log_event, new_trace_id
from .instagram_fetch import (
    DEFAULT_HEADERS, REQUEST_TIMEOUT, InstagramMedia, InstagramPost,
    _api_headers, _caption_from_embedded_node, _embedded_image_urls,
    _embedded_video_urls, _fill_from_open_graph, _is_probable_profile_image,
    _looks_like_login_wall, _make_soup, _first_meta,
    instagram_kind, instagram_media_id_from_shortcode, instagram_shortcode, normalize_instagram_url,
)

logger = logging.getLogger("discord_digest_bot")
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PARSE_NODES = 100000
MAX_ERROR_BYTES = 64 * 1024
PUBLIC_HEADERS = {**DEFAULT_HEADERS, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"}
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def _post(url, error=None):
    return InstagramPost(url=url, shortcode=instagram_shortcode(url), kind=instagram_kind(url), preview_error=error)


def _url_code(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {"www.instagram.com", "instagram.com"}:
        return None
    match = re.fullmatch(r"/(?:[^/]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)/?", parsed.path)
    return match.group(1) if match else None


def _walk(payload):
    stack = [(payload, 0)]
    remaining = MAX_PARSE_NODES
    while stack and remaining:
        value, depth = stack.pop()
        remaining -= 1
        if depth > 60:
            continue
        if isinstance(value, dict):
            yield value
            stack.extend((child, depth + 1) for key, child in reversed(list(value.items()))
                         if key not in {"comments", "edge_media_to_comment", "recommendations", "related_posts"})
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in reversed(value[:remaining]))


def _from_node(url, node, *, authenticated=False):
    owner = node.get("user") or node.get("owner") or {}
    private = owner.get("is_private") if isinstance(owner, dict) else None
    if private is True or (authenticated and private is not False):
        return _post(url, "private_or_unverified")
    post = _post(url)
    if isinstance(owner, dict):
        post.author_username = owner.get("username") if isinstance(owner.get("username"), str) else None
        post.author_name = owner.get("full_name") if isinstance(owner.get("full_name"), str) else None
    post.text = _caption_from_embedded_node(node)
    if post.text and _looks_like_login_wall(post.text):
        post.text = None
    nodes = [(node, 0)]
    remaining = 1000
    seen = set()
    while nodes and remaining:
        item, depth = nodes.pop()
        remaining -= 1
        if not isinstance(item, dict) or depth > 40:
            continue
        child_owner = item.get("user") or item.get("owner") or {}
        if isinstance(child_owner, dict) and child_owner.get("is_private") is True:
            continue
        children = item.get("carousel_media")
        sidecar = item.get("edge_sidecar_to_children")
        if not children and isinstance(sidecar, dict):
            children = [edge.get("node") for edge in sidecar.get("edges", []) if isinstance(edge, dict)]
        if isinstance(children, list) and children:
            nodes.extend((child, depth + 1) for child in reversed(children[:remaining]))
            continue
        videos = [u for u in _embedded_video_urls(item) if is_trusted_media_url(u) and urlparse(u).path.lower().endswith(".mp4")]
        images = [u for u in _embedded_image_urls(item) if is_trusted_media_url(u)
                  and not _is_probable_profile_image(InstagramMedia("image", u))]
        # A video node's thumbnail is not a separate carousel photo.
        kind, candidates = ("video", videos) if videos else ("image", images)
        if candidates and len(candidates[0]) <= 8192 and candidates[0] not in seen:
            seen.add(candidates[0])
            post.media.append(InstagramMedia(kind, candidates[0]))
    post.preview_level = "structured"
    if not (post.text or post.media):
        post.preview_error = "no_content"
    return post


def _target_node(payload, shortcode):
    for node in _walk(payload):
        if (node.get("code") or node.get("shortcode")) == shortcode and any(
            key in node for key in ("image_versions2", "video_versions", "display_url", "display_resources",
                                   "video_url", "carousel_media", "edge_sidecar_to_children")
        ):
            return node
    return None


def parse_instagram_page(url, html):
    post = _post(url, "no_content")
    soup = _make_soup(html)
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or script.get_text()
        if not raw or len(raw) > MAX_HTML_BYTES or post.shortcode not in raw:
            continue
        try:
            node = _target_node(json.loads(raw), post.shortcode)
        except (ValueError, RecursionError):
            continue
        if node is not None:
            return _from_node(url, node)
    # Only the page's matching permalink may authorize its public OG fallback.
    canonical = _first_meta(soup, "og:url")
    if _url_code(canonical) != post.shortcode:
        return post
    _fill_from_open_graph(post, soup)
    if not post.author_username:
        match = re.fullmatch(r"/([^/]+)/(?:p|reels?|tv)/[^/]+/?", urlparse(canonical).path)
        if match:
            post.author_username = match.group(1)
    post.media = [m for m in post.media if is_trusted_media_url(m.url) and len(m.url) <= 8192
                  and not _is_probable_profile_image(m)
                  and (m.type == "image" or urlparse(m.url).path.lower().endswith(".mp4"))]
    if post.text and _looks_like_login_wall(post.text):
        post.text = None
    if any(media.type == "video" for media in post.media):
        # OG images on a video page are cover art, not ordered carousel slots.
        post.media = [media for media in post.media if media.type == "video"][:1]
    post.preview_error = None if post.text or post.media else "no_content"
    return post


def _api_error(payload):
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str):
        return "request_rejected"
    return {
        "checkpoint_required": "checkpoint_required", "challenge_required": "checkpoint_required",
        "consent_required": "checkpoint_required", "login_required": "login_required",
        "feedback_required": "rate_limited", "please wait a few minutes before you try again.": "rate_limited",
        "useragent mismatch": "request_rejected",
    }.get(message.strip().lower(), "request_rejected")


def parse_instagram_api(url, body):
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("status") == "fail":
            return _post(url, _api_error(payload))
        node = _target_node(payload, instagram_shortcode(url))
        return _from_node(url, node, authenticated=True) if node is not None else _post(url, "no_content")
    except (ValueError, RecursionError, TypeError):
        return _post(url, "no_content")


def _valid_cookie(value):
    if len(value) > 16384 or any(ord(c) < 32 or ord(c) >= 127 for c in value):
        return False
    return all(separator and COOKIE_NAME.fullmatch(name) for name, separator, _ in
               (pair.strip().partition("=") for pair in value.split(";")))


def _request_post(url, cookie="", *, persist_cookies=True, trace_id=None):
    import requests

    result = _post(url, "fetch_failed")
    response = None
    status = None
    started = perf_counter()
    trace_id = trace_id or new_trace_id()
    reason = None

    def fail(error):
        result.preview_error = error
        return result

    try:
        target = url + "/"
        headers = dict(PUBLIC_HEADERS)
        snapshot = None
        if cookie:
            if not _valid_cookie(cookie):
                return fail("cookie_config_invalid")
            media_id = instagram_media_id_from_shortcode(instagram_shortcode(url))
            target = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
            snapshot = prepare_cookie("instagram", target, cookie, persist=persist_cookies)
            if not snapshot.header:
                return fail("login_required")
            headers = {**_api_headers(url), "Cookie": snapshot.header}
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(target, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True)
            status = response.status_code
            if status == 429:
                return fail("rate_limited")
            if status in (401, 403, 301, 302, 303, 307, 308):
                return fail("login_required")
            if status == 404:
                return fail("not_found")
            if status == 400 and snapshot:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    if len(body) + len(chunk) > MAX_ERROR_BYTES:
                        return fail("request_rejected")
                    body.extend(chunk)
                try:
                    return fail(_api_error(json.loads(body)))
                except (ValueError, TypeError, RecursionError):
                    return fail("request_rejected")
            if status != 200:
                return result
            body = bytearray()
            for chunk in response.iter_content(65536):
                if len(body) + len(chunk) > MAX_HTML_BYTES:
                    return result
                body.extend(chunk)
            text = body.decode("utf-8", errors="replace")
            result = parse_instagram_api(url, text) if snapshot else parse_instagram_page(url, text)
            if snapshot and not result.preview_error and (result.text or result.media):
                raw = getattr(getattr(response, "raw", None), "headers", None)
                save_response_cookies(snapshot, response_cookies(raw if raw is not None else response.headers))
            return result
    except requests.Timeout:
        reason = "timeout"
        return result
    except Exception:
        # Never include exception strings: they may contain Cookie headers or a login page.
        reason = "request_failed"
        return result
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        log_event("fetch_stage", trace_id=trace_id, shortcode=result.shortcode,
                  stage="cookie" if cookie else "anonymous", http_status=status,
                  result=result.preview_error or "success", reason=reason or result.preview_error or "content_found",
                  elapsed_ms=elapsed_ms(started), has_text=bool(result.text),
                  image_count=sum(m.type == "image" for m in result.media),
                  video_count=sum(m.type == "video" for m in result.media))


def fetch_instagram_direct(url, *, persist_cookies=True, trace_id=None):
    url = normalize_instagram_url(url)
    started, trace_id = perf_counter(), trace_id or new_trace_id()
    result = _post(url, "fetch_failed")
    source, cookie_result, kk_result = "none", "not_attempted", "not_attempted"
    cookie = os.environ.get("INSTAGRAM_COOKIE", "").strip()
    if not cookie:
        cookie_result = "not_configured"
    try:
        anonymous = _request_post(url, trace_id=trace_id)
        result, source = anonymous, "anonymous"
        if anonymous.preview_error in {"rate_limited", "not_found", "private_or_unverified"}:
            return result
        single_video_post = anonymous.kind in {"reel", "tv"}
        if (single_video_post and any(m.type == "video" for m in anonymous.media)) or (
                not single_video_post and anonymous.preview_level == "structured" and anonymous.media):
            return result
        if cookie:
            log_event("fallback", trace_id=trace_id, shortcode=result.shortcode, stage="cookie",
                      reason=anonymous.preview_error or ("missing_video" if single_video_post else "incomplete_metadata"))
            authenticated = _request_post(url, cookie, persist_cookies=persist_cookies, trace_id=trace_id)
            cookie_result = authenticated.preview_error or "success"
            if authenticated.preview_error in {"private_or_unverified", "not_found", "rate_limited"}:
                if authenticated.preview_error == "private_or_unverified" or anonymous.preview_error:
                    result, source = authenticated, "cookie"
                return result
            if not authenticated.preview_error:
                authenticated.text = authenticated.text or anonymous.text
                result, source = authenticated, "cookie"
            elif anonymous.preview_error:
                result, source = authenticated, "cookie"
        # Never guess missing carousel positions or bypass privacy.
        if (result.kind in {"reel", "tv"} and not anonymous.preview_error and (anonymous.text or anonymous.media)
                and not any(m.type == "video" for m in result.media)):
            log_event("fallback", trace_id=trace_id, shortcode=result.shortcode, stage="kkinstagram",
                      reason="missing_video", cookie_result=cookie_result)
            video = resolve_instagram_video_fallback(url, trace_id=trace_id)
            kk_result = "success" if video else "unavailable"
            if video:
                result.media = [InstagramMedia("video", video)]
                result.preview_error = None
                result.preview_level = "video_fallback"
                source = "kkinstagram"
        return result
    finally:
        result.trace_id, result.fetch_source = trace_id, source
        images = sum(m.type == "image" for m in result.media)
        videos = sum(m.type == "video" for m in result.media)
        outcome = "error" if result.preview_error else "video" if videos else "images_only" if images else "text_only"
        log_event("fetch_complete", trace_id=trace_id, shortcode=result.shortcode, kind=result.kind,
                  source=source, result=result.preview_error or "success", outcome=outcome,
                  cookie_result=cookie_result, kk_result=kk_result, elapsed_ms=elapsed_ms(started),
                  has_text=bool(result.text), image_count=images, video_count=videos)
