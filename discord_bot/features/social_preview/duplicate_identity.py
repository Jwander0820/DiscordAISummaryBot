"""Identify confirmed post URLs without fetching URLs or interpreting share tokens."""
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .facebook_fetch import facebook_post_identity
from .x_links import x_post_identity


def post_identity(platform: str, url: str):
    if platform == "x":
        return x_post_identity(url)
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        if platform == "threads" and host in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}:
            match = re.fullmatch(r"/@[^/]+/post/([A-Za-z0-9_-]+)", path)
            if match:
                return match[1], f"https://www.threads.com{path}"
        if platform == "instagram" and host in {"instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am"}:
            match = re.fullmatch(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)", path)
            if match:
                return match[1], f"https://www.instagram.com/p/{match[1]}/"
        if platform == "facebook":
            if re.match(r"/(?:share|login|checkpoint|recover|dialog)(?:[/.]|$)", path) or path == "/l.php":
                return None
            identity = facebook_post_identity(url)
            if identity:
                kind, key = identity
                query = parse_qs(parsed.query, max_num_fields=64)
                retained = {name: values[0] for name, values in query.items()
                            if name in {"v", "story_fbid", "fbid", "id", "set"}}
                canonical = urlunparse(("https", "www.facebook.com", parsed.path, "", urlencode(retained), ""))
                return f"{kind}:{key}", canonical
    except (TypeError, ValueError):
        pass
    return None
