from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("discord_digest_bot")

try:
    import psycopg2
except ImportError:  # pragma: no cover - depends on runtime extras
    psycopg2 = None


TAIPEI_TZ = timezone(timedelta(hours=8), "Asia/Taipei")
MARKET_1X2 = "1x2"
MARKET_TOTAL_GOALS_2_5 = "total_goals_2_5"
MARKET_CORRECT_SCORE = "correct_score"
SUPPORTED_MARKETS = (MARKET_1X2, MARKET_TOTAL_GOALS_2_5, MARKET_CORRECT_SCORE)
SETTLEMENT_MARKETS = SUPPORTED_MARKETS

SELECTION_HOME = "HOME"
SELECTION_DRAW = "DRAW"
SELECTION_AWAY = "AWAY"
SELECTION_OVER_2_5 = "OVER_2_5"
SELECTION_UNDER_2_5 = "UNDER_2_5"
SELECTION_OTHER = "OTHER"

MATCH_STATUS_FINISHED = "FINISHED"
MATCH_STATUS_CANCELLED = "CANCELLED"
MATCH_STATUS_POSTPONED = "POSTPONED"
SETTLEMENT_PENDING = "pending"
SETTLEMENT_SETTLED = "settled"

BET_STATUS_OPEN = "open"
BET_STATUS_WON = "won"
BET_STATUS_LOST = "lost"
BET_STATUS_REFUNDED = "refunded"

ODDS_BASIS = 100
FIXED_ODDS_BPS = {
    (MARKET_1X2, SELECTION_HOME): 200,
    (MARKET_1X2, SELECTION_DRAW): 300,
    (MARKET_1X2, SELECTION_AWAY): 200,
    (MARKET_TOTAL_GOALS_2_5, SELECTION_OVER_2_5): 190,
    (MARKET_TOTAL_GOALS_2_5, SELECTION_UNDER_2_5): 190,
    (MARKET_CORRECT_SCORE, SELECTION_OTHER): 400,
}
DEFAULT_CORRECT_SCORE_ODDS_BPS = 800

MATCHES_PAGE_LIMIT = 10
MY_BETS_LIMIT = 10
LEADERBOARD_LIMIT = 10


SQLITE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS world_cup_players (
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0,
        claimed_date TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        provider_match_id TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        kickoff_at TEXT NOT NULL,
        status TEXT NOT NULL,
        home_score_90 INTEGER,
        away_score_90 INTEGER,
        settlement_status TEXT NOT NULL DEFAULT 'pending',
        settled_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (guild_id, provider_match_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        match_id INTEGER NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        payout INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER NOT NULL,
        market TEXT NOT NULL,
        winning_selection TEXT,
        total_pool INTEGER NOT NULL,
        winning_pool INTEGER NOT NULL,
        settled_by TEXT,
        settled_at TEXT NOT NULL,
        UNIQUE (match_id, market)
    );
    """,
)

POSTGRES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS world_cup_players (
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0,
        claimed_date TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_matches (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        provider_match_id TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        kickoff_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        home_score_90 INTEGER,
        away_score_90 INTEGER,
        settlement_status TEXT NOT NULL DEFAULT 'pending',
        settled_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (guild_id, provider_match_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_bets (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        match_id INTEGER NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        payout INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS world_cup_settlements (
        id SERIAL PRIMARY KEY,
        match_id INTEGER NOT NULL,
        market TEXT NOT NULL,
        winning_selection TEXT,
        total_pool INTEGER NOT NULL,
        winning_pool INTEGER NOT NULL,
        settled_by TEXT,
        settled_at TIMESTAMPTZ NOT NULL,
        UNIQUE (match_id, market)
    );
    """,
)


@dataclass(frozen=True)
class FootballMatchPayload:
    provider_match_id: str
    home_team: str
    away_team: str
    kickoff_at: str
    status: str
    home_score_90: Optional[int]
    away_score_90: Optional[int]


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    balance: int
    claimed_date: str
    amount: int


@dataclass(frozen=True)
class BetResult:
    success: bool
    message: str
    balance: Optional[int] = None
    bet_id: Optional[int] = None


@dataclass(frozen=True)
class SettlementMarketResult:
    market: str
    winning_selection: Optional[str]
    total_pool: int
    winning_pool: int
    winner_count: int
    refunded_count: int
    already_settled: bool = False


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat()


def _today_taipei(now: Optional[datetime] = None) -> str:
    current = now or _now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(TAIPEI_TZ).date().isoformat()


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime_taipei(value: str) -> str:
    parsed = _parse_datetime(value).astimezone(TAIPEI_TZ)
    return parsed.strftime("%m/%d %H:%M")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s must be an integer; using %s", name, default)
        return default
    return value


def _choice_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _configured_admin_user_ids() -> set[str]:
    raw = os.getenv("WORLD_CUP_BETTING_ADMIN_USER_IDS", "")
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def _has_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    return bool(getattr(permissions, "manage_guild", False))


def _is_world_cup_admin(interaction: discord.Interaction) -> bool:
    user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    return _has_manage_guild(interaction) or user_id in _configured_admin_user_ids()


def normalize_selection(market: str, selection: str) -> str:
    market = market.strip()
    normalized = selection.strip().upper().replace(" ", "").replace("：", ":")
    aliases = {
        "主勝": SELECTION_HOME,
        "HOME": SELECTION_HOME,
        "H": SELECTION_HOME,
        "1": SELECTION_HOME,
        "平手": SELECTION_DRAW,
        "平": SELECTION_DRAW,
        "DRAW": SELECTION_DRAW,
        "D": SELECTION_DRAW,
        "X": SELECTION_DRAW,
        "客勝": SELECTION_AWAY,
        "AWAY": SELECTION_AWAY,
        "A": SELECTION_AWAY,
        "2": SELECTION_AWAY,
        "大": SELECTION_OVER_2_5,
        "大2.5": SELECTION_OVER_2_5,
        "OVER": SELECTION_OVER_2_5,
        "OVER2.5": SELECTION_OVER_2_5,
        "OVER_2_5": SELECTION_OVER_2_5,
        "小": SELECTION_UNDER_2_5,
        "小2.5": SELECTION_UNDER_2_5,
        "UNDER": SELECTION_UNDER_2_5,
        "UNDER2.5": SELECTION_UNDER_2_5,
        "UNDER_2_5": SELECTION_UNDER_2_5,
        "其他": SELECTION_OTHER,
        "OTHER": SELECTION_OTHER,
    }
    normalized = aliases.get(normalized, normalized)

    if market == MARKET_1X2 and normalized in {SELECTION_HOME, SELECTION_DRAW, SELECTION_AWAY}:
        return normalized
    if market == MARKET_TOTAL_GOALS_2_5 and normalized in {SELECTION_OVER_2_5, SELECTION_UNDER_2_5}:
        return normalized
    if market == MARKET_CORRECT_SCORE:
        if normalized == SELECTION_OTHER:
            return normalized
        score = normalized.replace(":", "-")
        parts = score.split("-")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            home, away = (int(parts[0]), int(parts[1]))
            if 0 <= home <= 7 and 0 <= away <= 7:
                return f"{home}-{away}"
    raise ValueError(
        "投注選項不符合玩法規則。勝平負請填：主勝 / 平手 / 客勝；"
        "總進球 2.5 請填：大 / 小；正確比分請填「主隊-客隊」，"
        "例如 2-1 代表主隊 2 分、客隊 1 分；高比分請填 OTHER。"
    )


def resolve_winning_selection(market: str, home_score: int, away_score: int) -> str:
    if market == MARKET_1X2:
        if home_score > away_score:
            return SELECTION_HOME
        if home_score < away_score:
            return SELECTION_AWAY
        return SELECTION_DRAW
    if market == MARKET_TOTAL_GOALS_2_5:
        return SELECTION_OVER_2_5 if home_score + away_score > 2.5 else SELECTION_UNDER_2_5
    if market == MARKET_CORRECT_SCORE:
        if 0 <= home_score <= 7 and 0 <= away_score <= 7:
            return f"{home_score}-{away_score}"
        return SELECTION_OTHER
    raise ValueError(f"Unsupported market: {market}")


def fixed_odds_bps(market: str, selection: str) -> int:
    if market == MARKET_CORRECT_SCORE and selection != SELECTION_OTHER:
        return DEFAULT_CORRECT_SCORE_ODDS_BPS
    return FIXED_ODDS_BPS.get((market, selection), ODDS_BASIS)


def fixed_odds_label(market: str, selection: str) -> str:
    odds = fixed_odds_bps(market, selection)
    if odds % ODDS_BASIS == 0:
        return f"{odds // ODDS_BASIS}.0x"
    return f"{odds / ODDS_BASIS:.2f}x".rstrip("0").rstrip(".") + "x"


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


class FootballDataClient:
    """Tiny football-data.org client scoped to the temporary World Cup game."""

    def __init__(self, *, token: Optional[str] = None, competition_code: Optional[str] = None) -> None:
        self.token = token if token is not None else os.getenv("FOOTBALL_DATA_API_TOKEN")
        self.competition_code = competition_code or os.getenv("WORLD_CUP_COMPETITION_CODE", "WC")
        self.base_url = os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4")

    def fetch_matches(self) -> list[FootballMatchPayload]:
        if not self.token:
            raise RuntimeError("FOOTBALL_DATA_API_TOKEN is not configured")

        url = f"{self.base_url}/competitions/{urllib.parse.quote(self.competition_code)}/matches"
        request = urllib.request.Request(url, headers={"X-Auth-Token": self.token, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        return [self._parse_match(item) for item in payload.get("matches", [])]

    def _parse_match(self, item: dict[str, Any]) -> FootballMatchPayload:
        score = item.get("score") or {}
        regular_time = score.get("regularTime") or {}
        full_time = score.get("fullTime") or {}
        home_score = regular_time.get("home")
        away_score = regular_time.get("away")
        if home_score is None:
            home_score = full_time.get("home")
        if away_score is None:
            away_score = full_time.get("away")

        return FootballMatchPayload(
            provider_match_id=str(item["id"]),
            home_team=(item.get("homeTeam") or {}).get("name") or "TBD",
            away_team=(item.get("awayTeam") or {}).get("name") or "TBD",
            kickoff_at=_parse_datetime(item["utcDate"]).isoformat(),
            status=str(item.get("status") or "SCHEDULED"),
            home_score_90=home_score if home_score is None else int(home_score),
            away_score_90=away_score if away_score is None else int(away_score),
        )


class WorldCupBettingRepository:
    """Self-contained persistence for the temporary World Cup betting game."""

    def __init__(self, *, retry_interval_seconds: float = 30.0) -> None:
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        self.placeholder = "?"
        self.conn: Optional[Any] = None
        self.cursor: Optional[Any] = None
        self._initialized = False
        self._last_init_attempt: Optional[float] = None
        self.retry_interval_seconds = retry_interval_seconds

    def init(self, *, force: bool = False) -> bool:
        if self._initialized and not force:
            return self._ready
        if force:
            self.close()

        self._last_init_attempt = time.monotonic()
        self.db_type = (os.getenv("DB_TYPE", "sqlite") or "sqlite").lower()
        self.db_enabled = True
        if self.db_type not in {"sqlite", "postgres"}:
            logger.error("Unsupported DB_TYPE for World Cup betting: %s; falling back to sqlite", self.db_type)
            self.db_type = "sqlite"

        if self.db_type == "postgres":
            self._init_postgres()
        else:
            self._init_sqlite()

        self._initialized = True
        return self._ready

    def _init_sqlite(self) -> None:
        sqlite_path = os.getenv("SQLITE_PATH", "summaries.db")
        try:
            self.conn = sqlite3.connect(sqlite_path)
            self.cursor = self.conn.cursor()
            for ddl in SQLITE_DDL:
                self.cursor.execute(ddl)
            self.conn.commit()
            self.placeholder = "?"
            logger.info("World Cup betting SQLite initialized at %s", sqlite_path)
        except Exception as exc:
            logger.error("World Cup betting SQLite initialization failed: %s", exc, exc_info=True)
            self.db_enabled = False

    def _init_postgres(self) -> None:
        if psycopg2 is None:
            logger.error("psycopg2 is unavailable; World Cup betting DB disabled")
            self.db_enabled = False
            return
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            logger.error("DATABASE_URL is required for World Cup betting PostgreSQL")
            self.db_enabled = False
            return
        try:
            self.conn = psycopg2.connect(database_url, connect_timeout=5)
            self.cursor = self.conn.cursor()
            for ddl in POSTGRES_DDL:
                self.cursor.execute(ddl)
            self.conn.commit()
            self.placeholder = "%s"
            logger.info("World Cup betting PostgreSQL initialized")
        except Exception as exc:
            logger.error("World Cup betting PostgreSQL initialization failed: %s", exc, exc_info=True)
            self.db_enabled = False

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                logger.debug("Failed to close World Cup betting DB", exc_info=True)
        self.conn = None
        self.cursor = None
        self._initialized = False

    @property
    def _ready(self) -> bool:
        return self.db_enabled and self.conn is not None and self.cursor is not None

    def _ensure_ready(self) -> bool:
        if self.init():
            return True
        now = time.monotonic()
        if self._last_init_attempt is None or now - self._last_init_attempt >= self.retry_interval_seconds:
            return self.init(force=True)
        return False

    def _mark_unavailable(self) -> None:
        self.db_enabled = False
        self.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if not self._ensure_ready():
            raise RuntimeError("World Cup betting DB is unavailable")
        assert self.cursor is not None
        return self.cursor.execute(sql, params)

    def _commit(self) -> None:
        assert self.conn is not None
        self.conn.commit()

    def _rollback(self) -> None:
        if self.conn is not None:
            self.conn.rollback()

    def get_player(self, guild_id: str, user_id: str) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT guild_id, user_id, balance, claimed_date FROM world_cup_players "
            f"WHERE guild_id = {self.placeholder} AND user_id = {self.placeholder};"
        )
        self._execute(sql, (guild_id, user_id))
        row = self.cursor.fetchone()
        return None if row is None else _row_to_dict(self.cursor, row)

    def ensure_player(self, guild_id: str, user_id: str) -> dict[str, Any]:
        player = self.get_player(guild_id, user_id)
        if player is not None:
            return player
        now = _iso_now()
        sql = (
            "INSERT INTO world_cup_players (guild_id, user_id, balance, claimed_date, created_at, updated_at) "
            f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
            f"{self.placeholder}, {self.placeholder});"
        )
        self._execute(sql, (guild_id, user_id, 0, None, now, now))
        self._commit()
        return self.get_player(guild_id, user_id) or {
            "guild_id": guild_id,
            "user_id": user_id,
            "balance": 0,
            "claimed_date": None,
        }

    def claim_daily(self, guild_id: str, user_id: str, amount: int, *, now: Optional[datetime] = None) -> ClaimResult:
        player = self.ensure_player(guild_id, user_id)
        claim_date = _today_taipei(now)
        if player.get("claimed_date") == claim_date:
            return ClaimResult(False, int(player["balance"]), claim_date, amount)

        updated_at = _iso_now()
        sql = (
            "UPDATE world_cup_players SET balance = balance + "
            f"{self.placeholder}, claimed_date = {self.placeholder}, updated_at = {self.placeholder} "
            f"WHERE guild_id = {self.placeholder} AND user_id = {self.placeholder};"
        )
        self._execute(sql, (amount, claim_date, updated_at, guild_id, user_id))
        self._commit()
        updated = self.get_player(guild_id, user_id)
        return ClaimResult(True, int(updated["balance"]), claim_date, amount)

    def upsert_matches(self, guild_id: str, matches: list[FootballMatchPayload]) -> int:
        now = _iso_now()
        count = 0
        for match in matches:
            score_updates = (
                match.home_score_90,
                match.away_score_90,
            )
            sql = (
                "INSERT INTO world_cup_matches "
                "(guild_id, provider_match_id, home_team, away_team, kickoff_at, status, "
                "home_score_90, away_score_90, settlement_status, updated_at) "
                f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
                f"{self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
                f"{self.placeholder}, {self.placeholder}) "
                "ON CONFLICT(guild_id, provider_match_id) DO UPDATE SET "
                "home_team = excluded.home_team, "
                "away_team = excluded.away_team, "
                "kickoff_at = excluded.kickoff_at, "
                "status = excluded.status, "
                "home_score_90 = excluded.home_score_90, "
                "away_score_90 = excluded.away_score_90, "
                "updated_at = excluded.updated_at;"
            )
            self._execute(
                sql,
                (
                    guild_id,
                    match.provider_match_id,
                    match.home_team,
                    match.away_team,
                    match.kickoff_at,
                    match.status,
                    score_updates[0],
                    score_updates[1],
                    SETTLEMENT_PENDING,
                    now,
                ),
            )
            count += 1
        self._commit()
        return count

    def get_match(self, guild_id: str, match_id: int) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT id, guild_id, provider_match_id, home_team, away_team, kickoff_at, status, "
            "home_score_90, away_score_90, settlement_status, settled_at "
            "FROM world_cup_matches "
            f"WHERE guild_id = {self.placeholder} AND id = {self.placeholder};"
        )
        self._execute(sql, (guild_id, match_id))
        row = self.cursor.fetchone()
        return None if row is None else _row_to_dict(self.cursor, row)

    def list_today_matches(self, guild_id: str, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        current = now or _now_utc()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        start = current.astimezone(TAIPEI_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=2)
        sql = (
            "SELECT id, home_team, away_team, kickoff_at, status, home_score_90, away_score_90, settlement_status "
            "FROM world_cup_matches "
            f"WHERE guild_id = {self.placeholder} AND kickoff_at >= {self.placeholder} AND kickoff_at < {self.placeholder} "
            "ORDER BY kickoff_at ASC LIMIT 20;"
        )
        self._execute(sql, (guild_id, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()))
        return [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]

    def list_pending_settlements(self, guild_id: str) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, home_team, away_team, kickoff_at, status, home_score_90, away_score_90 "
            "FROM world_cup_matches "
            f"WHERE guild_id = {self.placeholder} AND status = {self.placeholder} "
            f"AND settlement_status = {self.placeholder} "
            "ORDER BY kickoff_at ASC LIMIT 20;"
        )
        self._execute(sql, (guild_id, MATCH_STATUS_FINISHED, SETTLEMENT_PENDING))
        return [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]

    def place_bet(
        self,
        guild_id: str,
        user_id: str,
        match_id: int,
        market: str,
        selection: str,
        amount: int,
        *,
        lock_minutes: int,
        now: Optional[datetime] = None,
    ) -> BetResult:
        if amount <= 0:
            return BetResult(False, "下注金額必須大於 0")
        if market not in SUPPORTED_MARKETS:
            return BetResult(False, "不支援的玩法")
        try:
            normalized_selection = normalize_selection(market, selection)
        except ValueError as exc:
            return BetResult(False, str(exc))

        player = self.ensure_player(guild_id, user_id)
        balance = int(player["balance"])
        if balance < amount:
            return BetResult(False, f"代幣不足，目前餘額 {balance}", balance)

        match = self.get_match(guild_id, match_id)
        if match is None:
            return BetResult(False, "找不到這場比賽", balance)
        if match["status"] in {MATCH_STATUS_FINISHED, MATCH_STATUS_CANCELLED, MATCH_STATUS_POSTPONED}:
            return BetResult(False, "這場比賽已不可下注", balance)

        current = now or _now_utc()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        kickoff = _parse_datetime(match["kickoff_at"])
        if current.astimezone(timezone.utc) >= kickoff - timedelta(minutes=lock_minutes):
            return BetResult(False, f"這場比賽已鎖盤（開賽前 {lock_minutes} 分鐘）", balance)

        created_at = _iso_now()
        try:
            update_sql = (
                f"UPDATE world_cup_players SET balance = balance - {self.placeholder}, updated_at = {self.placeholder} "
                f"WHERE guild_id = {self.placeholder} AND user_id = {self.placeholder};"
            )
            self._execute(update_sql, (amount, created_at, guild_id, user_id))
            insert_sql = (
                "INSERT INTO world_cup_bets "
                "(guild_id, user_id, match_id, market, selection, amount, status, payout, created_at) "
                f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
                f"{self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder});"
            )
            self._execute(
                insert_sql,
                (guild_id, user_id, match_id, market, normalized_selection, amount, BET_STATUS_OPEN, 0, created_at),
            )
            bet_id = getattr(self.cursor, "lastrowid", None)
            self._commit()
        except Exception:
            self._rollback()
            raise

        updated = self.get_player(guild_id, user_id)
        return BetResult(True, "下注成功", int(updated["balance"]), bet_id)

    def list_user_bets(self, guild_id: str, user_id: str, *, limit: int = MY_BETS_LIMIT) -> list[dict[str, Any]]:
        sql = (
            "SELECT b.id, b.market, b.selection, b.amount, b.status, b.payout, b.created_at, "
            "m.home_team, m.away_team, m.kickoff_at "
            "FROM world_cup_bets b "
            "JOIN world_cup_matches m ON b.match_id = m.id "
            f"WHERE b.guild_id = {self.placeholder} AND b.user_id = {self.placeholder} "
            "ORDER BY b.created_at DESC "
            f"LIMIT {self.placeholder};"
        )
        self._execute(sql, (guild_id, user_id, limit))
        return [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]

    def leaderboard(self, guild_id: str, *, limit: int = LEADERBOARD_LIMIT) -> list[dict[str, Any]]:
        sql = (
            "SELECT user_id, balance FROM world_cup_players "
            f"WHERE guild_id = {self.placeholder} "
            "ORDER BY balance DESC, user_id ASC "
            f"LIMIT {self.placeholder};"
        )
        self._execute(sql, (guild_id, limit))
        return [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]

    def settle_match(self, guild_id: str, match_id: int, *, settled_by: str) -> list[SettlementMarketResult]:
        match = self.get_match(guild_id, match_id)
        if match is None:
            raise ValueError("找不到這場比賽")
        if match["status"] != MATCH_STATUS_FINISHED:
            raise ValueError("只有 API 標記為完賽的比賽可以結算")
        if match["home_score_90"] is None or match["away_score_90"] is None:
            raise ValueError("缺少比分，不能結算")

        results = []
        try:
            for market in SETTLEMENT_MARKETS:
                results.append(self._settle_market(match, market, settled_by=settled_by))

            if all(result.already_settled for result in results):
                self._commit()
                return results

            settled_at = _iso_now()
            sql = (
                f"UPDATE world_cup_matches SET settlement_status = {self.placeholder}, settled_at = {self.placeholder} "
                f"WHERE guild_id = {self.placeholder} AND id = {self.placeholder};"
            )
            self._execute(sql, (SETTLEMENT_SETTLED, settled_at, guild_id, match_id))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return results

    def _settle_market(self, match: dict[str, Any], market: str, *, settled_by: str) -> SettlementMarketResult:
        existing_sql = (
            "SELECT winning_selection, total_pool, winning_pool FROM world_cup_settlements "
            f"WHERE match_id = {self.placeholder} AND market = {self.placeholder};"
        )
        self._execute(existing_sql, (match["id"], market))
        existing = self.cursor.fetchone()
        if existing is not None:
            existing_row = _row_to_dict(self.cursor, existing)
            return SettlementMarketResult(
                market,
                existing_row["winning_selection"],
                int(existing_row["total_pool"]),
                int(existing_row["winning_pool"]),
                0,
                0,
                already_settled=True,
            )

        winning_selection = resolve_winning_selection(market, int(match["home_score_90"]), int(match["away_score_90"]))
        bets_sql = (
            "SELECT id, guild_id, user_id, amount, selection FROM world_cup_bets "
            f"WHERE match_id = {self.placeholder} AND market = {self.placeholder} AND status = {self.placeholder};"
        )
        self._execute(bets_sql, (match["id"], market, BET_STATUS_OPEN))
        bets = [_row_to_dict(self.cursor, row) for row in self.cursor.fetchall()]
        total_pool = sum(int(bet["amount"]) for bet in bets)
        winning_bets = [bet for bet in bets if bet["selection"] == winning_selection]
        winning_pool = sum(int(bet["amount"]) for bet in winning_bets)
        losing_pool = total_pool - winning_pool

        winner_count = 0
        refunded_count = 0
        if bets and winning_pool <= 0:
            for bet in bets:
                self._update_bet_status(int(bet["id"]), BET_STATUS_LOST, 0)
        else:
            for bet in bets:
                amount = int(bet["amount"])
                if bet["selection"] == winning_selection:
                    fixed_payout = amount * fixed_odds_bps(market, winning_selection) // ODDS_BASIS
                    loser_pool_bonus = amount * losing_pool // winning_pool if winning_pool else 0
                    payout = fixed_payout + loser_pool_bonus
                    self._credit_player(str(bet["guild_id"]), str(bet["user_id"]), payout)
                    self._update_bet_status(int(bet["id"]), BET_STATUS_WON, payout)
                    winner_count += 1
                else:
                    self._update_bet_status(int(bet["id"]), BET_STATUS_LOST, 0)

        settlement_sql = (
            "INSERT INTO world_cup_settlements "
            "(match_id, market, winning_selection, total_pool, winning_pool, settled_by, settled_at) "
            f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
            f"{self.placeholder}, {self.placeholder}, {self.placeholder});"
        )
        self._execute(
            settlement_sql,
            (match["id"], market, winning_selection, total_pool, winning_pool, settled_by, _iso_now()),
        )
        return SettlementMarketResult(
            market,
            winning_selection,
            total_pool,
            winning_pool,
            winner_count,
            refunded_count,
        )

    def _credit_player(self, guild_id: str, user_id: str, amount: int) -> None:
        sql = (
            f"UPDATE world_cup_players SET balance = balance + {self.placeholder}, updated_at = {self.placeholder} "
            f"WHERE guild_id = {self.placeholder} AND user_id = {self.placeholder};"
        )
        self._execute(sql, (amount, _iso_now(), guild_id, user_id))

    def _update_bet_status(self, bet_id: int, status: str, payout: int) -> None:
        sql = (
            f"UPDATE world_cup_bets SET status = {self.placeholder}, payout = {self.placeholder} "
            f"WHERE id = {self.placeholder};"
        )
        self._execute(sql, (status, payout, bet_id))


class WorldCupBettingService:
    def __init__(
        self,
        *,
        repository: Optional[WorldCupBettingRepository] = None,
        football_client: Optional[FootballDataClient] = None,
    ) -> None:
        self.repository = repository or WorldCupBettingRepository()
        self.football_client = football_client or FootballDataClient()

    @property
    def daily_claim_amount(self) -> int:
        return _env_int("WORLD_CUP_DAILY_CLAIM_AMOUNT", 20000)

    @property
    def bet_lock_minutes(self) -> int:
        return _env_int("WORLD_CUP_BET_LOCK_MINUTES", 10)

    def claim_daily(self, guild_id: str, user_id: str, *, now: Optional[datetime] = None) -> ClaimResult:
        return self.repository.claim_daily(guild_id, user_id, self.daily_claim_amount, now=now)

    def place_bet(
        self,
        guild_id: str,
        user_id: str,
        match_id: int,
        market: str,
        selection: str,
        amount: int,
        *,
        now: Optional[datetime] = None,
    ) -> BetResult:
        return self.repository.place_bet(
            guild_id,
            user_id,
            match_id,
            market,
            selection,
            amount,
            lock_minutes=self.bet_lock_minutes,
            now=now,
        )

    def sync_matches(self, guild_id: str) -> int:
        matches = self.football_client.fetch_matches()
        return self.repository.upsert_matches(guild_id, matches)

    def settle_match(self, guild_id: str, match_id: int, *, settled_by: str) -> list[SettlementMarketResult]:
        return self.repository.settle_match(guild_id, match_id, settled_by=settled_by)


world_cup_betting_service = WorldCupBettingService()


def _guild_id_or_none(interaction: discord.Interaction) -> Optional[str]:
    guild = getattr(interaction, "guild", None)
    guild_id = getattr(guild, "id", None)
    return None if guild_id is None else str(guild_id)


async def world_cup_match_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    guild_id = _guild_id_or_none(interaction)
    if guild_id is None:
        return []
    try:
        matches = world_cup_betting_service.repository.list_today_matches(guild_id)
    except Exception:
        logger.debug("World Cup match autocomplete failed", exc_info=True)
        return []

    query = current.strip().lower()
    choices = []
    for match in matches:
        if not _match_is_bettable(match, lock_minutes=world_cup_betting_service.bet_lock_minutes):
            continue
        name = _match_choice_name(match, lock_minutes=world_cup_betting_service.bet_lock_minutes)
        if query and query not in name.lower() and query != str(match["id"]):
            continue
        choices.append(app_commands.Choice(name=name, value=int(match["id"])))
    return choices[:25]


async def world_cup_selection_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    market = _choice_value(getattr(getattr(interaction, "namespace", None), "market", ""))
    return _selection_choices_for_market(market, current)


def _market_label(market: str) -> str:
    return {
        MARKET_1X2: "勝平負",
        MARKET_TOTAL_GOALS_2_5: "總進球 2.5",
        MARKET_CORRECT_SCORE: "正確比分",
    }.get(market, market)


def _selection_label(selection: str) -> str:
    return {
        SELECTION_HOME: "主勝",
        SELECTION_DRAW: "平手",
        SELECTION_AWAY: "客勝",
        SELECTION_OVER_2_5: "大 2.5",
        SELECTION_UNDER_2_5: "小 2.5",
        SELECTION_OTHER: "其他比分",
    }.get(selection, selection)


def _bet_status_label(status: str) -> str:
    return {
        BET_STATUS_OPEN: "未結算",
        BET_STATUS_WON: "中獎",
        BET_STATUS_LOST: "未中",
        BET_STATUS_REFUNDED: "已退款",
    }.get(status, status)


def _match_choice_name(match: dict[str, Any], *, lock_minutes: int) -> str:
    kickoff = _parse_datetime(match["kickoff_at"])
    current = _now_utc()
    locked = not _match_is_bettable(match, lock_minutes=lock_minutes, now=current)
    state = "鎖盤" if locked else "可下注"
    name = (
        f"#{match['id']} {_format_datetime_taipei(match['kickoff_at'])} "
        f"{match['home_team']} vs {match['away_team']} [{state}]"
    )
    return name[:100]


def _match_is_bettable(
    match: dict[str, Any],
    *,
    lock_minutes: int,
    now: Optional[datetime] = None,
) -> bool:
    if match["status"] in {MATCH_STATUS_FINISHED, MATCH_STATUS_CANCELLED, MATCH_STATUS_POSTPONED}:
        return False
    current = now or _now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    kickoff = _parse_datetime(match["kickoff_at"])
    return current.astimezone(timezone.utc) < kickoff - timedelta(minutes=lock_minutes)


def _correct_score_choices(current: str) -> list[app_commands.Choice[str]]:
    query = current.strip().replace(":", "-").upper()
    choices = []
    for home in range(8):
        for away in range(8):
            value = f"{home}-{away}"
            if query and query not in value:
                continue
            choices.append(
                app_commands.Choice(
                    name=f"{value}（主隊 {home}：客隊 {away}）",
                    value=value,
                )
            )
    if "OTHER".startswith(query) or "其他".startswith(current.strip()):
        choices.append(app_commands.Choice(name="OTHER（任一隊超過 7 球）", value=SELECTION_OTHER))
    return choices[:25]


def _selection_choices_for_market(market: str, current: str) -> list[app_commands.Choice[str]]:
    if market == MARKET_1X2:
        choices = [
            app_commands.Choice(name="主勝（主隊贏）", value=SELECTION_HOME),
            app_commands.Choice(name="平手", value=SELECTION_DRAW),
            app_commands.Choice(name="客勝（客隊贏）", value=SELECTION_AWAY),
        ]
    elif market == MARKET_TOTAL_GOALS_2_5:
        choices = [
            app_commands.Choice(name="大 2.5（兩隊合計 3 球以上）", value=SELECTION_OVER_2_5),
            app_commands.Choice(name="小 2.5（兩隊合計 0 到 2 球）", value=SELECTION_UNDER_2_5),
        ]
    elif market == MARKET_CORRECT_SCORE:
        return _correct_score_choices(current)
    else:
        choices = [
            app_commands.Choice(name="勝平負：選項填 主勝 / 平手 / 客勝", value=SELECTION_HOME),
            app_commands.Choice(name="總進球 2.5：選項填 大 / 小", value=SELECTION_OVER_2_5),
            app_commands.Choice(name="正確比分：填 主隊-客隊，例如 2-1；高比分 OTHER", value="2-1"),
        ]

    query = current.strip().lower()
    if not query:
        return choices
    return [choice for choice in choices if query in choice.name.lower() or query in str(choice.value).lower()][:25]


def _format_matches(matches: list[dict[str, Any]], *, lock_minutes: int, now: Optional[datetime] = None) -> str:
    if not matches:
        return "今明兩天沒有已同步的世足賽事。"
    current = now or _now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    lines = []
    for match in matches[:MATCHES_PAGE_LIMIT]:
        kickoff = _parse_datetime(match["kickoff_at"])
        locked = current.astimezone(timezone.utc) >= kickoff - timedelta(minutes=lock_minutes)
        score = ""
        if match["home_score_90"] is not None and match["away_score_90"] is not None:
            score = f" {match['home_score_90']}-{match['away_score_90']}"
        state = "鎖盤" if locked else "可下注"
        lines.append(
            f"#{match['id']} {_format_datetime_taipei(match['kickoff_at'])} "
            f"{match['home_team']} vs {match['away_team']} [{match['status']}/{state}]{score}"
        )
    return "\n".join(lines)


def _format_bet_confirmation(
    *,
    match: Optional[dict[str, Any]],
    match_id: int,
    market: str,
    selection: str,
    amount: int,
    balance: Optional[int],
) -> str:
    try:
        normalized_selection = normalize_selection(market, selection)
    except ValueError:
        normalized_selection = selection

    if match is None:
        match_line = f"比賽：#{match_id}"
    else:
        match_line = (
            f"比賽：#{match['id']} {_format_datetime_taipei(match['kickoff_at'])} "
            f"{match['home_team']} vs {match['away_team']}"
        )

    return "\n".join(
        [
            "下注成功！",
            match_line,
            f"玩法：{_market_label(market)}",
            f"選項：{_selection_label(normalized_selection)}",
            f"金額：{amount}",
            f"剩餘餘額：{balance}",
        ]
    )


class WorldCupBettingCog(commands.Cog):
    """Temporary, env-gated World Cup token betting game."""

    def __init__(self, bot: commands.Bot, *, service: Optional[WorldCupBettingService] = None) -> None:
        self.bot = bot
        self.service = service or world_cup_betting_service

    @app_commands.command(name="世足領代幣", description="每天領取一次世足娛樂代幣")
    async def claim_tokens(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return

        result = self.service.claim_daily(guild_id, str(interaction.user.id))
        if result.claimed:
            message = f"領取成功，今天拿到 {result.amount} 代幣。目前餘額：{result.balance}"
        else:
            message = f"今天已經領過囉。目前餘額：{result.balance}"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="世足今日賽事", description="查看今明兩天已同步的世足賽事")
    async def todays_matches(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return

        matches = self.service.repository.list_today_matches(guild_id)
        content = _format_matches(matches, lock_minutes=self.service.bet_lock_minutes)
        await interaction.response.send_message(content, ephemeral=True)

    @app_commands.command(name="世足下注", description="使用娛樂代幣下注世足賽事")
    @app_commands.rename(match_id="比賽編號", market="玩法", selection="選項", amount="金額")
    @app_commands.describe(
        match_id="用 /世足今日賽事 看到的比賽編號，也可直接輸入隊名搜尋",
        market="玩法",
        selection="勝平負填主勝/平手/客勝；大小填大/小；比分填主隊-客隊如2-1；高比分OTHER",
        amount="下注代幣數量",
    )
    @app_commands.autocomplete(match_id=world_cup_match_autocomplete, selection=world_cup_selection_autocomplete)
    @app_commands.choices(
        market=[
            app_commands.Choice(name="勝平負（選項：主勝 / 平手 / 客勝）", value=MARKET_1X2),
            app_commands.Choice(name="總進球 2.5（選項：大 / 小）", value=MARKET_TOTAL_GOALS_2_5),
            app_commands.Choice(name="正確比分（選項：主隊-客隊，例如 2-1）", value=MARKET_CORRECT_SCORE),
        ]
    )
    async def place_bet(
        self,
        interaction: discord.Interaction,
        match_id: int,
        market: app_commands.Choice[str],
        selection: str,
        amount: int,
    ) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return

        result = self.service.place_bet(
            guild_id,
            str(interaction.user.id),
            match_id,
            _choice_value(market),
            selection,
            amount,
        )
        if result.success:
            match = self.service.repository.get_match(guild_id, match_id)
            message = _format_bet_confirmation(
                match=match,
                match_id=match_id,
                market=_choice_value(market),
                selection=selection,
                amount=amount,
                balance=result.balance,
            )
            await interaction.response.send_message(message, ephemeral=False)
        else:
            await interaction.response.send_message(result.message, ephemeral=True)

    @app_commands.command(name="世足我的下注", description="查看自己的世足下注紀錄")
    async def my_bets(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return

        bets = self.service.repository.list_user_bets(guild_id, str(interaction.user.id))
        if not bets:
            await interaction.response.send_message("目前沒有下注紀錄。", ephemeral=True)
            return
        lines = []
        for bet in bets:
            lines.append(
                f"#{bet['id']} {bet['home_team']} vs {bet['away_team']} "
                f"{_market_label(bet['market'])}/{_selection_label(bet['selection'])} "
                f"{bet['amount']} -> {_bet_status_label(bet['status'])} 派彩={bet['payout']}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="世足排行榜", description="查看本伺服器世足代幣排行榜")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return

        rows = self.service.repository.leaderboard(guild_id)
        if not rows:
            await interaction.response.send_message("目前還沒有玩家領取代幣。", ephemeral=True)
            return
        lines = [f"{idx}. <@{row['user_id']}>：{row['balance']}" for idx, row in enumerate(rows, start=1)]
        await interaction.response.send_message("\n".join(lines), ephemeral=False)

    @app_commands.command(name="世足同步賽程", description="管理員：從足球 API 同步世足賽程與比分")
    async def sync_matches(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return
        if not _is_world_cup_admin(interaction):
            await interaction.response.send_message("需要管理伺服器權限或世足活動管理員 ID。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            matches = await asyncio.to_thread(self.service.football_client.fetch_matches)
            count = self.service.repository.upsert_matches(guild_id, matches)
        except Exception as exc:
            logger.error("World Cup match sync failed: %s", exc, exc_info=True)
            await interaction.followup.send(f"同步失敗：{exc}", ephemeral=True)
            return
        await interaction.followup.send(f"同步完成，共更新 {count} 場比賽。", ephemeral=True)

    @app_commands.command(name="世足待結算", description="管理員：查看已完賽但尚未結算的世足比賽")
    async def pending_settlements(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return
        if not _is_world_cup_admin(interaction):
            await interaction.response.send_message("需要管理伺服器權限或世足活動管理員 ID。", ephemeral=True)
            return

        matches = self.service.repository.list_pending_settlements(guild_id)
        if not matches:
            await interaction.response.send_message("目前沒有待結算比賽。", ephemeral=True)
            return
        lines = []
        for match in matches:
            lines.append(
                f"#{match['id']} {match['home_team']} {match['home_score_90']}-"
                f"{match['away_score_90']} {match['away_team']}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="世足確認結算", description="管理員：確認比分並發放分池獎金")
    @app_commands.rename(match_id="比賽編號")
    async def confirm_settlement(self, interaction: discord.Interaction, match_id: int) -> None:
        guild_id = _guild_id_or_none(interaction)
        if guild_id is None:
            await interaction.response.send_message("這個活動只能在伺服器內使用。", ephemeral=True)
            return
        if not _is_world_cup_admin(interaction):
            await interaction.response.send_message("需要管理伺服器權限或世足活動管理員 ID。", ephemeral=True)
            return

        try:
            results = self.service.settle_match(guild_id, match_id, settled_by=str(interaction.user.id))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        lines = []
        for result in results:
            suffix = "（已結算，略過）" if result.already_settled else ""
            lines.append(
                f"{_market_label(result.market)}：{_selection_label(str(result.winning_selection))} "
                f"固定倍率={fixed_odds_label(result.market, str(result.winning_selection))} "
                f"池={result.total_pool} 勝池={result.winning_pool} "
                f"贏家={result.winner_count} 退款={result.refunded_count}{suffix}"
            )
        await interaction.response.send_message("結算完成：\n" + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WorldCupBettingCog(bot))
