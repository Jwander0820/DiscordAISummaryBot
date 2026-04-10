from __future__ import annotations

from datetime import datetime
from typing import Optional

import discord


async def collect_non_bot_messages(
    channel: discord.TextChannel,
    *,
    limit: int,
    after: Optional[datetime] = None,
    fetch_multiplier: float = 1.0,
) -> list[discord.Message]:
    """Fetch recent messages while skipping bot authors.

    `fetch_multiplier` lets callers over-fetch when they expect many bot/system messages
    so the final non-bot list can still reach the requested limit.
    """
    messages: list[discord.Message] = []
    history_limit = max(limit, int(limit * fetch_multiplier))
    async for message in channel.history(limit=history_limit, after=after, oldest_first=False):
        if message.author.bot:
            continue
        messages.append(message)
        if len(messages) >= limit:
            break
    return messages


def format_message_history(
    messages: list[discord.Message],
    *,
    include_author_id: bool = False,
    include_display_name: bool = True,
    time_format: str = "%H:%M",
) -> str:
    """Render Discord messages into the prompt format used by summary and Q&A flows."""
    lines = []
    for message in reversed(messages):
        timestamp = message.created_at.astimezone(TZ_8).strftime(time_format)
        author = message.author.display_name if include_display_name else message.author.name
        if include_author_id:
            lines.append(f"[{timestamp}] [id:{message.author.name}] {author}: {message.content}")
        else:
            lines.append(f"[{timestamp}] {author}: {message.content}")
    return "\n".join(lines)


def truncate_for_discord(text: str, *, limit: int = 1900, suffix: str = "...（已截斷）") -> str:
    """Trim long text while keeping room for a readable suffix."""
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


TZ_8 = datetime.now().astimezone().tzinfo
try:
    from datetime import timedelta, timezone

    TZ_8 = timezone(timedelta(hours=8))
except Exception:  # pragma: no cover - defensive fallback
    pass
