from __future__ import annotations

import re
from typing import Callable, Pattern

EMPTY_SPOILER_RE = re.compile(r"\|\|\s*\|\|")
INLINE_SPACE_RE = re.compile(r"[ \t]+")
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?，。！？；：])")


def extract_message_commentary(
    content: str,
    *,
    target_url: str,
    url_pattern: Pattern[str],
    sanitize_url: Callable[[str], str],
) -> str:
    """Remove the handled URL from a message while keeping the user's commentary."""
    if not content or not target_url:
        return ""

    segments = []
    cursor = 0
    removed_any = False

    for match in url_pattern.finditer(content):
        raw_url = match.group(0)
        if sanitize_url(raw_url) != target_url:
            continue

        start, end = match.span()
        segments.append(content[cursor:start])
        if raw_url.startswith(target_url):
            segments.append(raw_url[len(target_url):])
        cursor = end
        removed_any = True

    if not removed_any:
        return _normalize_commentary(content)

    segments.append(content[cursor:])
    return _normalize_commentary("".join(segments))


def _normalize_commentary(text: str) -> str:
    text = EMPTY_SPOILER_RE.sub("", text)
    cleaned_lines = []

    for raw_line in text.splitlines():
        line = INLINE_SPACE_RE.sub(" ", raw_line).strip()
        line = SPACE_BEFORE_PUNCT_RE.sub(r"\1", line)
        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)
