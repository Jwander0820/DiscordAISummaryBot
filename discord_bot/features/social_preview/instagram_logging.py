"""Small, allowlisted diagnostic records; never serialize response objects or URLs."""
from __future__ import annotations

import json
import logging
import re
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger("discord_digest_bot")
EVENTS = {"fetch_stage", "fallback", "fetch_complete", "preview_prepared", "preview_delivery"}
FIELDS = {
    "stage", "source", "result", "reason", "http_status", "elapsed_ms", "image_count", "video_count",
    "has_text", "kind", "cookie_result", "kk_result", "outcome", "selected_count", "rendered_count",
    "dropped_count", "spoiler", "page_count", "error_type", "error_code", "phase",
}
SAFE_CODE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def new_trace_id():
    return uuid4().hex[:16]


def elapsed_ms(started):
    return max(0, round((perf_counter() - started) * 1000))


def _safe(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    return value if isinstance(value, str) and SAFE_CODE.fullmatch(value) else "redacted"


def _level(record):
    event, result = record["event"], record.get("result")
    if event == "preview_delivery":
        return logging.ERROR if result == "failed" else logging.DEBUG
    if event == "preview_prepared":
        dropped = record.get("dropped_count", 0)
        if result not in {None, "success"} or (isinstance(dropped, int) and dropped > 0):
            return logging.WARNING
    if event == "fetch_stage" and result not in {None, "success"}:
        status = record.get("http_status")
        if (record.get("stage") != "anonymous" or (isinstance(status, int) and status >= 400)
                or record.get("reason") in {"timeout", "request_failed"}):
            return logging.WARNING
    if event == "fetch_complete":
        if (result not in {None, "success"}
                or record.get("cookie_result") not in {None, "success", "not_attempted", "not_configured"}
                or record.get("kk_result") not in {None, "not_attempted"}
                or (record.get("kind") in {"reel", "tv"} and record.get("outcome") in {"images_only", "text_only"})):
            return logging.WARNING
    return logging.DEBUG


def log_event(event, *, trace_id, shortcode, **fields):
    if event not in EVENTS:
        return
    record = {"event": event, "trace_id": _safe(trace_id), "shortcode": _safe(shortcode)}
    record.update((key, _safe(value)) for key, value in fields.items() if key in FIELDS)
    logger.log(_level(record), "instagram_diagnostic %s", json.dumps(record, sort_keys=True, separators=(",", ":")))
