"""Explicit local maintenance: one Facebook Cookie GET, safe counters only."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_cookie() -> str:
    cookie = os.environ.get("FACEBOOK_COOKIE", "")
    if not cookie:
        for path in (PROJECT_ROOT / "discord_bot" / ".env", PROJECT_ROOT / ".env"):
            if path.is_file():
                from dotenv import dotenv_values
                cookie = dotenv_values(path, interpolate=False).get("FACEBOOK_COOKIE") or ""
                break
    return cookie


def _safe_result(post) -> dict:
    allowed = ("cookie_attempts", "cookie_status", "cookie_error_hint", "response_bytes", "json_count", "matched_nodes", "public_nodes")
    return {"result": "success" if post.has_content else post.reason,
            "has_text": bool(post.description), "image_count": len(post.image_urls), "video_count": len(post.video_urls),
            "public": post.public, **{key: post.debug[key] for key in allowed if key in post.debug}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Resolved Facebook post or video URL; not a share shortcut")
    parser.add_argument("--check-config", action="store_true", help="Validate only; do not make any network request")
    args = parser.parse_args()
    from discord_bot.features.social_preview.facebook_cookie import try_facebook_cookie
    from discord_bot.features.social_preview.facebook_fetch import canonical_facebook_url, facebook_post_identity

    url = canonical_facebook_url(args.url)
    if not url or not facebook_post_identity(url):
        print(json.dumps({"result": "invalid_post_url"}))
        return 2
    cookie = _load_cookie()
    if not cookie.strip():
        print(json.dumps({"result": "cookie_not_configured"}))
        return 2
    if args.check_config:
        from discord_bot.features.social_preview.facebook_cookie import _valid_cookie
        pairs = [pair.strip().partition("=") for pair in cookie.strip().split(";")]
        names = [pair[0].lower() for pair in pairs]
        values = {name: value for name, separator, value in pairs if separator}
        valid = _valid_cookie(cookie)
        print(json.dumps({"result": "cookie_format_valid" if valid else "cookie_config_invalid",
                          "cookie_bytes": len(cookie.encode("utf-8")), "cookie_pairs": len(pairs),
                          "duplicate_names": len(names) - len(set(names)),
                          "has_c_user": bool(values.get("c_user")), "has_xs": bool(values.get("xs")),
                          "export_attributes": len(set(names) & {"domain", "path", "expires", "max-age", "samesite"}),
                          "requests": 0}))
        return 0 if valid else 2
    post = asyncio.run(try_facebook_cookie(url, cookie, persist_cookies=False))
    print(json.dumps(_safe_result(post), ensure_ascii=False))
    return 0 if post.has_content else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"result": "diagnostic_failed"}))
        raise SystemExit(2) from None
