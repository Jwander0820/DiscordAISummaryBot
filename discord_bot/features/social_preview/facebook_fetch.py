"""Post-bound Facebook media parsing; no network or Discord side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .download import is_trusted_media_url

# Authenticated document payloads fluctuate around 4 MiB even for one photo.
# Keep a finite body cap while the separate node/depth budgets bound parsing.
MAX_FACEBOOK_HTML_BYTES = 8 * 1024 * 1024
MAX_FACEBOOK_PARSE_NODES = 100000
MAX_FACEBOOK_PARSE_DEPTH = 60
PUBLIC_LABELS = {"public", "everyone", "公開", "所有人"}
MEDIA_CHILDREN = {
    "attachments", "styles", "style_type_renderer", "attachment", "media",
    "all_subattachments", "subattachments", "nodes", "edges", "node",
    "short_form_video_context", "playback_video", "video", "creation_story", "story",
}
CONTEXT_CHILDREN = ("creation_story", "story", "comet_sections", "content", "context_layout", "metadata")


@dataclass
class FacebookPostData:
    url: str
    title: str = ""
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    public: bool = False
    reason: str = "target_not_found"
    debug: dict = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        return bool(self.description or self.image_urls or self.video_urls)


def canonical_facebook_url(raw: Any) -> str | None:
    """Cookie-capable targets always use Facebook HTTPS, never media hosts."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)
                or not (host == "facebook.com" or host.endswith(".facebook.com"))):
            return None
        return urlunparse(("https", "www.facebook.com", parsed.path, "", parsed.query, ""))
    except ValueError:
        return None


def facebook_post_identity(raw: Any) -> tuple[str, str] | None:
    url = canonical_facebook_url(raw)
    if not url:
        return None
    parsed = urlparse(url)
    match = re.search(r"/(reel|videos|posts)/(?:[^/]+/)?(\d+|pfbid[A-Za-z0-9]+)(?:/|$)", parsed.path)
    if match:
        return ("post" if match[1] == "posts" else "video", match[2])
    try:
        query = parse_qs(parsed.query, max_num_fields=64)
    except ValueError:
        return None
    for key, kind in (("v", "video"), ("story_fbid", "post"), ("fbid", "photo")):
        value = query.get(key, [""])[0]
        if re.fullmatch(r"\d+|pfbid[A-Za-z0-9]+", value):
            return kind, value
    return None


def is_facebook_video_page(url: str) -> bool:
    identity = facebook_post_identity(url)
    return bool(identity and identity[0] == "video") or bool(re.search(r"/share/[vr]/", urlparse(url).path))


def direct_facebook_mp4(raw: Any) -> str | None:
    """Preserve signed queries, reject manifests, range fragments and other hosts."""
    if not isinstance(raw, str) or len(raw) > 8192:
        return None
    url = html.unescape(re.sub(r"\\u0026", "&", raw.strip(), flags=re.I).replace(r"\/", "/"))
    if not is_trusted_media_url(url):
        return None
    parsed = urlparse(url)
    if not parsed.path.lower().endswith(".mp4"):
        return None
    try:
        keys = {key.lower() for key in parse_qs(parsed.query, max_num_fields=100)}
    except ValueError:
        return None
    if keys & {"bytestart", "byteend", "range"}:
        return None
    return url


def _identity_matches(node: dict, target: str) -> bool:
    for key in ("url", "permalink_url", "permalink", "post_url", "story_url", "shareable_url", "mainEntityOfPage"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        identity = facebook_post_identity(value)
        if identity and identity[1] == target:
            return True
    return any(str(node.get(key, "")) == target for key in ("id", "videoId", "video_id", "post_id", "story_fbid"))


def _public_state(node: dict) -> bool | None:
    if node.get("is_private") is True or node.get("is_public") is False:
        return False
    scope = node.get("privacy_scope")
    if isinstance(scope, dict):
        # Privacy is an explicit structured field, never inferred from body/UI
        # text. Unknown locales/scopes remain unverified for authenticated data.
        # Logged-in Relay payloads may expose only privacy_scope.description.
        # This is the visibility label, not the post's description/body.
        for key in ("type", "value", "label", "description"):
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().casefold() in PUBLIC_LABELS
    if node.get("is_public") is True:
        return True
    return None


def _contexts(node: dict, depth: int = 0):
    if depth > 8:
        return
    yield node
    for key in CONTEXT_CHILDREN:
        child = node.get(key)
        if isinstance(child, dict):
            yield from _contexts(child, depth + 1)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    yield from _contexts(item, depth + 1)


def _unsupported_video(node: dict) -> bool:
    if (node.get("is_live_streaming") is True or node.get("has_audio") is False
            or node.get("is_video_only") is True or node.get("is_drm") is True):
        return True
    drm = node.get("drm_info")
    if isinstance(drm, str):
        try:
            drm = json.loads(drm)
        except ValueError:
            return bool(drm)
    if isinstance(drm, dict):
        # Facebook includes a generic player certificate even for ordinary MP4.
        # Only video-specific license information establishes a DRM requirement.
        return bool(drm.get("video_license_uri_map") or drm.get("graph_api_video_license_uri")
                    or drm.get("license_url") or drm.get("is_drm"))
    return bool(drm)


def _video_from_node(node: dict) -> str | None:
    if _unsupported_video(node):
        return None
    legacy = node.get("videoDeliveryLegacyFields")
    formats = legacy if isinstance(legacy, dict) else node
    for key in ("browser_native_hd_url", "playable_url_quality_hd", "browser_native_sd_url", "playable_url"):
        url = direct_facebook_mp4(formats.get(key))
        if url:
            return url
    fragment = node.get("videoDeliveryResponseFragment") or {}
    result = fragment.get("videoDeliveryResponseResult") if isinstance(fragment, dict) else None
    progressive = result.get("progressive_urls") if isinstance(result, dict) else None
    candidates = []
    for item in progressive if isinstance(progressive, list) else []:
        if not isinstance(item, dict) or item.get("has_audio") is False or item.get("is_video_only") is True:
            continue
        url = direct_facebook_mp4(item.get("progressive_url"))
        metadata = item.get("metadata") or {}
        quality = metadata.get("quality", "") if isinstance(metadata, dict) else ""
        if url:
            candidates.append((str(quality).lower() == "hd", url))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _read_bound_post(url: str, node: dict, kind: str, target: str, require_public: bool) -> FacebookPostData:
    result = FacebookPostData(url=url)
    contexts = list(_contexts(node))
    states = [_public_state(item) for item in contexts]
    if False in states:
        result.reason = "not_public"
        return result
    result.public = True in states
    if require_public and not result.public:
        result.reason = "public_unverified"
        return result

    seen_nodes = set()
    remaining = MAX_FACEBOOK_PARSE_NODES
    allow_post_video = not any(_unsupported_video(context) for context in contexts)

    def media(node, depth=0, allow_video=allow_post_video):
        nonlocal remaining
        if not isinstance(node, (dict, list)) or depth > 30 or remaining <= 0 or id(node) in seen_nodes:
            return
        remaining -= 1
        seen_nodes.add(id(node))
        if isinstance(node, list):
            for child in node:
                media(child, depth + 1, allow_video)
            return
        if _public_state(node) is False:
            return
        allow_video = allow_video and not _unsupported_video(node)
        is_video = any(key in node for key in ("videoDeliveryLegacyFields", "videoDeliveryResponseFragment",
                       "browser_native_hd_url", "browser_native_sd_url", "playable_url", "playable_url_quality_hd"))
        if is_video and allow_video:
            video_id = node.get("videoId") or node.get("id")
            if kind == "video" and video_id is not None and str(video_id) != target:
                return
            video = _video_from_node(node)
            if video and not result.video_urls:
                result.video_urls.append(video)
        for key in ("image", "photo_image", "full_image", "best_effort_image", "thumbnailImage"):
            image = node.get(key)
            image_url = image.get("uri") or image.get("url") if isinstance(image, dict) else None
            if isinstance(image_url, str) and is_trusted_media_url(image_url) and image_url not in result.image_urls:
                result.image_urls.append(image_url)
        thumbnail = node.get("preferred_thumbnail")
        if isinstance(thumbnail, dict):
            media(thumbnail, depth + 1, allow_video)
        # Only explicit attachment edges inherit the target post's ownership.
        # Recommended posts, comments, actors, attached_story and quotes are absent.
        for key, child in node.items():
            if key in MEDIA_CHILDREN:
                media(child, depth + 1, allow_video)

    for context in contexts:
        if not result.title:
            title = context.get("name") or context.get("title")
            if isinstance(title, str):
                result.title = html.unescape(title)
        if not result.description:
            for key in ("message", "savable_description", "caption", "description"):
                value = context.get(key)
                if isinstance(value, dict):
                    value = value.get("text")
                if isinstance(value, str) and value.strip():
                    result.description = html.unescape(value).strip()
                    break
        media(context)
    result.image_urls = result.image_urls[:4]
    result.reason = "target_found" if result.has_content else "target_has_no_content"
    return result


def extract_facebook_post(body: str, page_url: str, *, require_public: bool = False) -> FacebookPostData:
    """Read only target-bound JSON; authenticated pages never use global OG/DOM."""
    result = FacebookPostData(url=page_url)
    if len(body.encode("utf-8", errors="replace")) > MAX_FACEBOOK_HTML_BYTES:
        result.reason = "response_too_large"
        return result
    soup = BeautifulSoup(body, "html.parser")
    identity = facebook_post_identity(page_url)
    if not identity:
        # Share pages may expose a canonical video URL without an HTTP redirect.
        tag = soup.find("meta", attrs={"property": "og:url"})
        canonical = canonical_facebook_url(tag.get("content")) if tag else None
        if canonical and (identity := facebook_post_identity(canonical)):
            result.url = canonical
    if not identity:
        result.reason = "target_unresolved"
        return result
    kind, target = identity
    metrics = {"json_count": 0, "matched_nodes": 0, "public_nodes": 0}
    result.debug = metrics
    remaining = MAX_FACEBOOK_PARSE_NODES

    def visit(node, depth=0):
        nonlocal remaining
        if not isinstance(node, (dict, list)) or depth > MAX_FACEBOOK_PARSE_DEPTH or remaining <= 0:
            return
        remaining -= 1
        if isinstance(node, dict):
            matches = _identity_matches(node, target)
            if not matches and isinstance(node.get("comet_sections"), dict):
                # A Story's permalink and privacy can be in separate content /
                # context_layout sections. Only these explicit own-story edges
                # establish the parent binding, never attachment/link text.
                matches = any(_identity_matches(context, target) for context in _contexts(node))
            if matches:
                yield node
            # A child format object must not bypass its parent's privacy,
            # live/video-only, or DRM classification.
            if _public_state(node) is False or _unsupported_video(node):
                return
            for child in node.values():
                yield from visit(child, depth + 1)
        else:
            for child in node:
                yield from visit(child, depth + 1)

    scripts = soup.find_all("script", attrs={"type": "application/json"})
    scripts.sort(key=lambda tag: target not in (tag.string or ""))
    for tag in scripts:
        try:
            payload = json.loads(tag.string or "")
            metrics["json_count"] += 1
            for node in visit(payload):
                metrics["matched_nodes"] += 1
                candidate = _read_bound_post(result.url, node, kind, target, require_public)
                if candidate.public:
                    metrics["public_nodes"] += 1
                if candidate.has_content:
                    # Facebook can emit a caption-only Relay payload before a
                    # second payload with the same post's media. Merge only
                    # independently bound, public-checked candidates.
                    result.title = result.title or candidate.title
                    result.description = result.description or candidate.description
                    result.public = result.public or candidate.public
                    for image in candidate.image_urls:
                        if image not in result.image_urls and len(result.image_urls) < 4:
                            result.image_urls.append(image)
                    result.video_urls = result.video_urls or candidate.video_urls
                    result.reason = "target_found"
                    if result.video_urls:
                        return result
                elif not result.has_content and candidate.reason in {"not_public", "public_unverified", "target_has_no_content"}:
                    result.reason = candidate.reason
        except (ValueError, TypeError, RecursionError):
            continue
    if remaining <= 0:
        result.reason = "parse_budget_exhausted"
    return result
