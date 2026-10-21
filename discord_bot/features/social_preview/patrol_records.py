"""Event-time identity snapshots; no network lookups or retrospective name guesses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from uuid import uuid4


def _text(value):
    return None if value is None else str(value)


@dataclass(frozen=True)
class PatrolDelivery:
    method: str
    message_id: str | None


def delivered(method: str, message) -> PatrolDelivery:
    value = getattr(message, "id", None)
    valid = isinstance(value, (int, str)) and str(value).isascii() and str(value).isdecimal()
    return PatrolDelivery(method, str(value) if valid else None)


def build_patrol_event(message, event, match) -> dict:
    user = message.author
    created = getattr(message, "created_at", None)
    created_at = created.timestamp() if isinstance(created, datetime) and created.tzinfo is not None else None
    previous = match.previous
    return {
        "event_id": str(uuid4()), "guild_id": event.guild_id,
        "guild_name": _text(getattr(message.guild, "name", None)),
        "channel_id": event.channel_id, "channel_name": _text(getattr(message.channel, "name", None)),
        "user_id": event.user_id, "username": _text(getattr(user, "name", None)),
        "global_name": _text(getattr(user, "global_name", None)),
        "display_name": _text(getattr(user, "display_name", None)),
        "platform": event.platform, "post_key": event.post_key, "canonical_url": event.canonical_url,
        "source_message_id": event.source_message_id, "source_message_created_at": created_at,
        "current_preview_message_id": event.preview_message_id,
        "first_source_message_id": previous.source_message_id, "first_user_id": previous.user_id,
        "reference_message_id": previous.preview_message_id, "share_count": match.share_count,
        "reference_channel_id": previous.channel_id,
        "occurred_at": time.time(), "delivery_status": "pending", "delivery_method": None,
        "patrol_message_id": None, "completed_at": None, "error_type": None,
        "taunt_text": None, "taunt_source": None,
    }
