from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _user_snapshot(user: Any, prefix: str) -> dict[str, Any]:
    """Capture Discord identity fields without assuming the member is human."""
    return {
        f"{prefix}_user_id": str(user.id),
        f"{prefix}_username": _optional_text(getattr(user, "name", None)),
        f"{prefix}_global_name": _optional_text(getattr(user, "global_name", None)),
        f"{prefix}_display_name": _optional_text(getattr(user, "display_name", None)),
        f"{prefix}_is_bot": bool(getattr(user, "bot", False)),
    }


def build_deepfaker_event(
    *,
    guild: Any,
    channel: Any,
    actor: Any,
    target: Any,
    outcome_success: bool,
    failure_probability: float,
    random_roll: float,
    requested_content: str,
    webhook_content: str,
    failure_notice: Optional[str],
    failure_exposed_content: Optional[str],
    delivery_status: str,
    occurred_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build one analytics-ready DeepFaker invocation event."""
    record = {
        "guild_id": str(guild.id),
        "guild_name": _optional_text(getattr(guild, "name", None)),
        "channel_id": str(channel.id),
        "channel_name": _optional_text(getattr(channel, "name", None)),
    }
    record.update(_user_snapshot(actor, "actor"))
    record.update(_user_snapshot(target, "target"))
    record.update(
        {
            "outcome_success": bool(outcome_success),
            "failure_probability": float(failure_probability),
            "random_roll": float(random_roll),
            "requested_content": requested_content,
            "webhook_content": webhook_content,
            "failure_notice": failure_notice,
            "failure_exposed_content": failure_exposed_content,
            "delivery_status": delivery_status,
            "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        }
    )
    return record
