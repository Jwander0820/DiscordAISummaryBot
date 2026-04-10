from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TZ_8 = timezone(timedelta(hours=8))


def now_tz8_iso() -> str:
    """Return the current timestamp in the project's default UTC+8 format."""
    return datetime.now(TZ_8).isoformat()


def build_summary_record(
    *,
    channel_id: Optional[str],
    user_id: Optional[str],
    command: str,
    question: str = "",
    prompt: str = "",
    summary: Optional[str] = None,
    call_time: Optional[str] = None,
) -> dict[str, Any]:
    """Build a normalized record payload for the `summaries` table and notifications."""
    return {
        "channel_id": channel_id,
        "user_id": user_id,
        "command": command,
        "question": question,
        "prompt": prompt,
        "summary": summary,
        "call_time": call_time or now_tz8_iso(),
    }
