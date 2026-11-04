"""Pure monthly aggregation over one consistent snapshot of the two event tables.

Discord, the database and rendering stay outside this module so the settlement rules
can be verified with fictional rows only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .periods import month_bounds, taipei_date

ZERO_COLLISION_MIN_SHARES = 50  # strictly greater than this number
POPULAR_POST_PLACES = 3
POPULAR_POST_LIMIT = 5


@dataclass(frozen=True, slots=True)
class ShareRow:
    channel_id: str
    platform: str
    source_message_id: str
    user_id: str
    post_key: str
    canonical_url: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class PatrolRow:
    event_id: str
    channel_id: str
    user_id: str
    username: str | None
    global_name: str | None
    display_name: str | None
    platform: str
    post_key: str
    first_source_message_id: str
    first_user_id: str
    reference_channel_id: str | None
    occurred_at: float
    delivery_status: str


@dataclass(frozen=True, slots=True)
class Ranked:
    user_id: str
    value: int


@dataclass(frozen=True, slots=True)
class FastestPatrol:
    user_id: str
    seconds: float
    platform: str


@dataclass(frozen=True, slots=True)
class PopularPost:
    rank: int
    platform: str
    post_key: str
    canonical_url: str
    shares: int
    tied: bool


@dataclass(frozen=True, slots=True)
class BusyDay:
    day: date
    patrols: int


@dataclass
class MonthlyReport:
    guild_id: str
    report_month: str
    period_start: float
    period_end: float
    share_rows: int = 0
    share_posts: int = 0
    share_users: int = 0
    share_channels: int = 0
    patrol_rows: int = 0
    patrol_sent: int = 0
    patrolled_users: int = 0
    cross_channel_sent: int = 0
    share_first: float | None = None
    share_last: float | None = None
    patrol_first: float | None = None
    patrol_last: float | None = None
    status_counts: dict = field(default_factory=dict)
    top_sharers: list[Ranked] = field(default_factory=list)
    zero_collision: list[Ranked] = field(default_factory=list)
    most_patrolled: list[Ranked] = field(default_factory=list)
    cross_channel: list[Ranked] = field(default_factory=list)
    first_movers: list[Ranked] = field(default_factory=list)
    fastest: list[FastestPatrol] = field(default_factory=list)
    popular_posts: list[PopularPost] = field(default_factory=list)
    busiest_days: list[BusyDay] = field(default_factory=list)
    snapshot_names: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.share_rows == 0 and self.patrol_rows == 0

    def member_ids(self) -> list[str]:
        ids = {item.user_id for group in (self.top_sharers, self.zero_collision, self.most_patrolled,
                                          self.cross_channel, self.first_movers, self.fastest)
               for item in group}
        return sorted(ids, key=_id_order)


def _id_order(value: str):
    return (0, int(value), "") if str(value).isdecimal() else (1, 0, str(value))


def _leaders(counter: Counter) -> list[Ranked]:
    if not counter:
        return []
    best = max(counter.values())
    if best <= 0:
        return []
    return [Ranked(user, count) for user, count in sorted(counter.items(), key=lambda item: _id_order(item[0]))
            if count == best]


def _snapshot_name(row: PatrolRow) -> str | None:
    for value in (row.display_name, row.global_name, row.username):
        if isinstance(value, str) and value.strip():
            return value
    return None


def compute_report(guild_id: str, report_month: str, shares, patrols, *,
                   period_end: float | None = None) -> MonthlyReport:
    """Aggregate rows already limited to this guild; boundaries are re-checked here."""
    guild_id = str(guild_id)
    lo, hi = month_bounds(report_month)
    if period_end is not None:
        hi = min(hi, period_end)
    report = MonthlyReport(guild_id, report_month, lo, hi)

    shares = [row for row in shares if lo <= row.occurred_at < hi]
    patrols = [row for row in patrols if lo <= row.occurred_at < hi]

    report.share_rows = len(shares)
    report.share_posts = len({(row.platform, row.post_key) for row in shares})
    report.share_users = len({row.user_id for row in shares})
    report.share_channels = len({row.channel_id for row in shares})
    if shares:
        report.share_first = min(row.occurred_at for row in shares)
        report.share_last = max(row.occurred_at for row in shares)
    sent = [row for row in patrols if row.delivery_status == "sent"]
    report.patrol_rows = len(patrols)
    report.patrol_sent = len(sent)
    report.patrolled_users = len({row.user_id for row in sent})
    report.status_counts = dict(sorted(Counter(row.delivery_status for row in patrols).items()))
    if patrols:
        report.patrol_first = min(row.occurred_at for row in patrols)
        report.patrol_last = max(row.occurred_at for row in patrols)

    # Event-time names from this month's patrols, latest snapshot first.
    for row in sorted(patrols, key=lambda item: (item.occurred_at, item.event_id)):
        name = _snapshot_name(row)
        if name is not None:
            report.snapshot_names[row.user_id] = name

    share_counts = Counter(row.user_id for row in shares)
    report.top_sharers = _leaders(share_counts)
    # Every detection counts, whatever its delivery status.
    detected = {row.user_id for row in patrols}
    report.zero_collision = sorted(
        (Ranked(user, count) for user, count in share_counts.items()
         if count > ZERO_COLLISION_MIN_SHARES and user not in detected),
        key=lambda item: (-item.value, _id_order(item.user_id)))

    report.most_patrolled = _leaders(Counter(row.user_id for row in sent))
    cross = [row for row in sent if row.reference_channel_id and row.reference_channel_id != row.channel_id]
    report.cross_channel_sent = len(cross)
    report.cross_channel = _leaders(Counter(row.user_id for row in cross))
    followed = [row for row in sent if row.user_id != row.first_user_id]
    report.first_movers = _leaders(Counter(row.first_user_id for row in followed))

    share_index = {(row.channel_id, row.platform, row.source_message_id): row for row in shares}
    gaps = []
    for row in followed:
        first = share_index.get((row.reference_channel_id or row.channel_id, row.platform,
                                 row.first_source_message_id))
        if first is None:
            continue
        gap = row.occurred_at - first.occurred_at
        if gap >= 0:
            gaps.append((gap, row))
    if gaps:
        best = min(gap for gap, _ in gaps)
        report.fastest = [FastestPatrol(row.user_id, gap, row.platform)
                          for gap, row in sorted(gaps, key=lambda item: (item[0], item[1].event_id))
                          if gap == best]

    posts = defaultdict(list)
    for row in shares:
        posts[(row.platform, row.post_key)].append(row)
    repeated = sorted(((len(rows), platform, key, min(row.canonical_url for row in rows))
                       for (platform, key), rows in posts.items() if len(rows) >= 2),
                      key=lambda item: (-item[0], item[1], item[2]))
    counts = Counter(item[0] for item in repeated)
    rank = 0
    for position, (count, platform, key, url) in enumerate(repeated[:POPULAR_POST_LIMIT], start=1):
        if position == 1 or count != repeated[position - 2][0]:
            rank = position  # Competition ranking: 1, 1, 3.
        if rank > POPULAR_POST_PLACES:
            break
        report.popular_posts.append(PopularPost(rank, platform, key, url, count, counts[count] > 1))

    daily = Counter(taipei_date(row.occurred_at) for row in sent)
    if daily:
        best = max(daily.values())
        report.busiest_days = [BusyDay(day, count) for day, count in sorted(daily.items()) if count == best]
    return report
