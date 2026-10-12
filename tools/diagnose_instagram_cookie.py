"""One explicit Instagram Cookie GET; emit only fixed status and media counts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_cookie():
    cookie = os.environ.get("INSTAGRAM_COOKIE", "")
    if not cookie:
        for path in (PROJECT_ROOT / "discord_bot" / ".env", PROJECT_ROOT / ".env"):
            if path.is_file():
                from dotenv import dotenv_values
                cookie = dotenv_values(path, interpolate=False).get("INSTAGRAM_COOKIE") or ""
                break
    return cookie.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--check-config", action="store_true", help="Validate format without network requests")
    args = parser.parse_args()
    from discord_bot.features.social_preview.instagram_direct import _request_post, _valid_cookie
    from discord_bot.features.social_preview.instagram_fetch import normalize_instagram_url

    try:
        url = normalize_instagram_url(args.url)
    except ValueError:
        print(json.dumps({"result": "invalid_post_url"}))
        return 2
    cookie = _load_cookie()
    if not cookie:
        print(json.dumps({"result": "cookie_not_configured"}))
        return 2
    valid = _valid_cookie(cookie)
    if args.check_config or not valid:
        pairs = [pair.strip().partition("=") for pair in cookie.split(";")]
        values = {name: value for name, separator, value in pairs if separator}
        print(json.dumps({"result": "cookie_format_valid" if valid else "cookie_config_invalid",
                          "has_sessionid": bool(values.get("sessionid")), "requests": 0}))
        return 0 if valid else 2
    post = _request_post(url, cookie, persist_cookies=False)
    print(json.dumps({"result": post.preview_error or "success", "has_text": bool(post.text),
                      "image_count": sum(m.type == "image" for m in post.media),
                      "video_count": sum(m.type == "video" for m in post.media)}))
    return 1 if post.preview_error else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"result": "diagnostic_failed"}))
        raise SystemExit(2) from None
