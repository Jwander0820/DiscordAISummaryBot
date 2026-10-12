"""Explicit local maintenance: one cookie request, only safe diagnostic output.

Reads the configured THREADS_COOKIE. Never prints credentials, post text, media
URLs, account IDs or response HTML, and never saves response data or cookies.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="One canonical Threads post URL to diagnose")
    args = parser.parse_args()
    from discord_bot.features.social_preview.threads_cookie import try_cookie_fallback
    from discord_bot.features.social_preview.threads_fetch import THREADS_RE, _strip_tracking_query

    url = _strip_tracking_query(args.url)
    if not THREADS_RE.fullmatch(url):
        print(json.dumps({"result": "invalid_post_url"}))
        return 2
    cookie = os.environ.get("THREADS_COOKIE", "")
    if not cookie:
        # Match the bot module's nearest-.env search; no environment mutation.
        for path in (PROJECT_ROOT / "discord_bot" / ".env", PROJECT_ROOT / ".env"):
            if path.is_file():
                from dotenv import dotenv_values
                cookie = dotenv_values(path, interpolate=False).get("THREADS_COOKIE") or ""
                break
    if not cookie.strip():
        print(json.dumps({"result": "cookie_not_configured"}))
        return 2
    post = try_cookie_fallback(url, cookie)
    allowed = ("cookie_attempts", "cookie_status", "cookie_reason", "cookie_response_bytes",
               "cookie_json_count", "cookie_parsed_json", "cookie_matched_nodes", "cookie_viewer_hint")
    print(json.dumps({"result": post.preview_error or "success", "has_text": bool(post.text),
                      "media_count": len(post.media), **{key: post.debug[key] for key in allowed if key in post.debug}},
                     ensure_ascii=False))
    return 0 if not post.preview_error else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Config/parser errors may contain sensitive values. Emit no traceback.
        print(json.dumps({"result": "diagnostic_failed"}))
        raise SystemExit(2) from None
