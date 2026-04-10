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
    """移除本次要處理的 URL，保留使用者原本的評論文字。

    這層會特別避開把 Threads/Facebook tracking query 殘留成評論的一部分。
    """
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
            suffix = raw_url[len(target_url):]
            if _should_preserve_url_suffix(suffix):
                segments.append(suffix)
        cursor = end
        removed_any = True

    if not removed_any:
        return _normalize_commentary(content)

    segments.append(content[cursor:])
    return _normalize_commentary("".join(segments))


def _should_preserve_url_suffix(suffix: str) -> bool:
    # 只保留純標點尾碼，像 `,`、`!`；`?xmt=...` 這類 query/fragment 一律丟掉。
    if not suffix:
        return False
    if any(char.isalnum() for char in suffix):
        return False
    if any(char in "?&=#/%" for char in suffix):
        return False
    return True


def _normalize_commentary(text: str) -> str:
    text = EMPTY_SPOILER_RE.sub("", text)
    cleaned_lines = []

    for raw_line in text.splitlines():
        line = INLINE_SPACE_RE.sub(" ", raw_line).strip()
        line = SPACE_BEFORE_PUNCT_RE.sub(r"\1", line)
        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)
