"""Patrol audit operations with independently owned connection state."""
from __future__ import annotations

from .schema import SOCIAL_PATROL_EVENT_COLUMNS
from .social_duplicate_repository import SocialDuplicateRepository


class SocialPatrolRepository:
    def __init__(self, storage=None):
        self.storage = storage if storage is not None else SocialDuplicateRepository()

    def close(self):
        self.storage.close()

    @property
    def backend(self):
        return self.storage.backend

    def insert_event(self, record: dict) -> bool:
        if set(record) != set(SOCIAL_PATROL_EVENT_COLUMNS):
            raise ValueError("Patrol event fields do not match schema")
        columns = ", ".join(SOCIAL_PATROL_EVENT_COLUMNS)
        placeholders = ", ".join("?" for _ in SOCIAL_PATROL_EVENT_COLUMNS)
        return self.storage.execute(
            f"INSERT INTO social_patrol_events ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT (guild_id, channel_id, platform, source_message_id) DO NOTHING",
            tuple(record[key] for key in SOCIAL_PATROL_EVENT_COLUMNS), write=True) == 1

    def finish_event(self, event_id: str, *, status: str, method=None, message_id=None,
                     completed_at: float, error_type=None, taunt_text=None, taunt_source=None) -> bool:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("Invalid patrol outcome")
        if method not in {None, "webhook", "bot_reply", "bot_notice"}:
            raise ValueError("Invalid patrol delivery method")
        if taunt_source not in {None, "none", "fixed", "gemini", "fallback"}:
            raise ValueError("Invalid patrol taunt source")
        return self.storage.execute(
            "UPDATE social_patrol_events SET delivery_status = ?, delivery_method = ?, "
            "patrol_message_id = ?, completed_at = ?, error_type = ?, taunt_text = ?, taunt_source = ? "
            "WHERE event_id = ? AND delivery_status = 'pending'",
            (status, method, message_id, completed_at, error_type, taunt_text, taunt_source, event_id), write=True) == 1
