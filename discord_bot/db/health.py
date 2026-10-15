"""Small, bounded DB health event buffer; contains no Discord or credential data."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class DatabaseEvent:
    repository: str
    backend: str
    operation: str
    recovered: bool
    unavailable: bool
    error_type: str
    sqlstate: str | None
    occurred_at: str


class DatabaseHealth:
    """Record transitions before Discord connects, coalescing repeated failures."""

    def __init__(self, *, reminder_seconds: float = 300.0, max_events: int = 16) -> None:
        self.events: asyncio.Queue[DatabaseEvent] = asyncio.Queue(maxsize=max_events)
        self.reminder_seconds = reminder_seconds
        self._failures: dict[str, float] = {}

    def failed(self, repository: str, backend: str, operation: str, *,
               error: Exception | None = None, reason: str = "DatabaseError",
               unavailable: bool = True) -> None:
        now = time.monotonic()
        last_notice = self._failures.get(repository)
        if last_notice is not None and now - last_notice < self.reminder_seconds:
            return
        self._failures[repository] = now
        error_type = type(error).__name__ if error is not None else reason
        error_type = re.sub(r"[^A-Za-z0-9_]", "", error_type)[:64] or "DatabaseError"
        sqlstate = getattr(error, "pgcode", None)
        if not isinstance(sqlstate, str) or not re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            sqlstate = None
        self._enqueue(DatabaseEvent(repository, backend, operation, False, unavailable,
                                    error_type, sqlstate, datetime.now(timezone.utc).isoformat()))

    def succeeded(self, repository: str, backend: str, operation: str) -> None:
        if self._failures.pop(repository, None) is None:
            return
        self._enqueue(DatabaseEvent(repository, backend, operation, True, False, "", None,
                                    datetime.now(timezone.utc).isoformat()))

    def _enqueue(self, event: DatabaseEvent) -> None:
        # Only the bot's main thread produces events. Never retain SQL/records/exceptions.
        if self.events.full():
            self.events.get_nowait()
            self.events.task_done()
        self.events.put_nowait(event)


database_health = DatabaseHealth()
