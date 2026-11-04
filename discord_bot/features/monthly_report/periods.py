"""Taipei calendar months as half-open UTC epoch ranges.

Taiwan has used a fixed UTC+08:00 offset without daylight saving since 1979, so a
fixed offset is exact for every period this bot can report and needs no tzdata.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
_MONTH = re.compile(r"(\d{4})-(\d{2})")
WEEKDAYS = "一二三四五六日"


def parse_month(value: str) -> tuple[int, int]:
    match = _MONTH.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("月份格式需為 YYYY-MM。")
    year, month = int(match[1]), int(match[2])
    if not 2000 <= year <= 9998 or not 1 <= month <= 12:
        raise ValueError("月份超出可結算範圍。")
    return year, month


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def shift_month(report_month: str, delta: int) -> str:
    year, month = parse_month(report_month)
    index = year * 12 + (month - 1) + delta
    return month_key(index // 12, index % 12 + 1)


def month_start(report_month: str) -> float:
    year, month = parse_month(report_month)
    return datetime(year, month, 1, tzinfo=TAIPEI).timestamp()


def month_bounds(report_month: str) -> tuple[float, float]:
    """Return [Taipei 1st 00:00, next month 1st 00:00) as UTC epoch seconds."""
    return month_start(report_month), month_start(shift_month(report_month, 1))


def taipei_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, TAIPEI)


def taipei_date(epoch: float) -> date:
    return taipei_datetime(epoch).date()


def month_of(epoch: float) -> str:
    moment = taipei_datetime(epoch)
    return month_key(moment.year, moment.month)


def due_month(now: float) -> str:
    """The most recent month whose Taipei boundary has already passed."""
    return shift_month(month_of(now), -1)


def next_boundary(now: float) -> float:
    return month_start(shift_month(month_of(now), 1))
