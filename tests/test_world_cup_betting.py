import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.support import install_discord_stub


class WorldCupBettingTests(unittest.TestCase):
    def setUp(self):
        install_discord_stub()
        sys.modules.pop("discord_bot.cogs.world_cup_betting_cog", None)
        self.module = importlib.import_module("discord_bot.cogs.world_cup_betting_cog")

    def _repository(self, sqlite_path):
        return self.module.WorldCupBettingRepository(retry_interval_seconds=0)

    def test_postgres_queries_use_psycopg2_placeholders(self):
        class FakeCursor:
            def __init__(self):
                self.queries = []
                self.params = []
                self.description = (("id",),)

            def execute(self, sql, params=()):
                self.queries.append(sql)
                self.params.append(params)

            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.cursor_obj = FakeCursor()

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                pass

            def close(self):
                pass

        fake_connection = FakeConnection()
        fake_psycopg2 = types.SimpleNamespace(connect=lambda *_args, **_kwargs: fake_connection)

        with patch.dict(os.environ, {"DB_TYPE": "postgres", "DATABASE_URL": "postgres://example"}, clear=False):
            with patch.object(self.module, "psycopg2", fake_psycopg2):
                repository = self.module.WorldCupBettingRepository(retry_interval_seconds=0)
                self.assertEqual(repository.placeholder, "%s")
                repository.list_today_matches("guild")

        query = fake_connection.cursor_obj.queries[-1]
        self.assertIn("guild_id = %s", query)
        self.assertNotIn("guild_id = ?", query)
        self.assertEqual(repository.placeholder, "%s")
        self.assertIsInstance(fake_connection.cursor_obj.params[-1][1], datetime)
        self.assertIsInstance(fake_connection.cursor_obj.params[-1][2], datetime)

    def test_match_formatting_accepts_postgres_datetime_values(self):
        kickoff = datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc)
        match = {
            "id": 61,
            "home_team": "Norway",
            "away_team": "France",
            "kickoff_at": kickoff,
            "status": "TIMED",
            "home_score_90": None,
            "away_score_90": None,
        }

        formatted = self.module._format_matches(
            [match],
            lock_minutes=10,
            now=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
        )
        choice_name = self.module._match_choice_name(match, lock_minutes=10)

        self.assertIn("Norway vs France", formatted)
        self.assertIn("06/27 03:00", formatted)
        self.assertIn("Norway vs France", choice_name)

    def _match_payload(
        self,
        provider_match_id="m1",
        *,
        kickoff_at=None,
        status="SCHEDULED",
        home_score=None,
        away_score=None,
    ):
        kickoff_at = kickoff_at or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        return self.module.FootballMatchPayload(
            provider_match_id=provider_match_id,
            home_team="Taiwan",
            away_team="Japan",
            kickoff_at=kickoff_at,
            status=status,
            home_score_90=home_score,
            away_score_90=away_score,
        )

    def test_daily_claim_is_once_per_taipei_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "worldcup.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "WORLD_CUP_DAILY_CLAIM_AMOUNT": "20000"},
                clear=False,
            ):
                repository = self._repository(sqlite_path)

                first = repository.claim_daily(
                    "guild",
                    "alice",
                    20000,
                    now=datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc),
                )
                second = repository.claim_daily(
                    "guild",
                    "alice",
                    20000,
                    now=datetime(2026, 6, 10, 15, 30, tzinfo=timezone.utc),
                )
                next_day = repository.claim_daily(
                    "guild",
                    "alice",
                    20000,
                    now=datetime(2026, 6, 11, 16, 30, tzinfo=timezone.utc),
                )

                self.assertTrue(first.claimed)
                self.assertFalse(second.claimed)
                self.assertTrue(next_day.claimed)
                self.assertEqual(next_day.balance, 40000)
                repository.close()

    def test_bet_validation_rejects_bad_selection_insufficient_balance_and_locked_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "worldcup.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = self._repository(sqlite_path)
                now = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
                kickoff = now + timedelta(hours=2)
                repository.upsert_matches("guild", [self._match_payload(kickoff_at=kickoff.isoformat())])
                match_id = repository.list_today_matches("guild", now=now)[0]["id"]
                repository.claim_daily("guild", "alice", 1000, now=now)

                bad_selection = repository.place_bet(
                    "guild",
                    "alice",
                    match_id,
                    self.module.MARKET_1X2,
                    "2-1",
                    100,
                    lock_minutes=10,
                    now=now,
                )
                too_expensive = repository.place_bet(
                    "guild",
                    "alice",
                    match_id,
                    self.module.MARKET_1X2,
                    "主勝",
                    5000,
                    lock_minutes=10,
                    now=now,
                )
                locked = repository.place_bet(
                    "guild",
                    "alice",
                    match_id,
                    self.module.MARKET_1X2,
                    "主勝",
                    100,
                    lock_minutes=10,
                    now=kickoff - timedelta(minutes=5),
                )

                self.assertFalse(bad_selection.success)
                self.assertFalse(too_expensive.success)
                self.assertFalse(locked.success)
                self.assertIn("鎖盤", locked.message)
                repository.close()

    def test_match_bettable_filter_rejects_locked_and_finished_matches(self):
        now = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
        open_match = {
            "status": "SCHEDULED",
            "kickoff_at": (now + timedelta(hours=2)).isoformat(),
        }
        locked_match = {
            "status": "SCHEDULED",
            "kickoff_at": (now + timedelta(minutes=5)).isoformat(),
        }
        finished_match = {
            "status": "FINISHED",
            "kickoff_at": (now + timedelta(hours=2)).isoformat(),
        }

        self.assertTrue(self.module._match_is_bettable(open_match, lock_minutes=10, now=now))
        self.assertFalse(self.module._match_is_bettable(locked_match, lock_minutes=10, now=now))
        self.assertFalse(self.module._match_is_bettable(finished_match, lock_minutes=10, now=now))

    def test_selection_choices_provide_help_before_market_and_score_examples_after_market(self):
        before_market = self.module._selection_choices_for_market("", "")
        correct_score = self.module._selection_choices_for_market(self.module.MARKET_CORRECT_SCORE, "2-")

        self.assertIn("HOME", [choice.value for choice in before_market])
        self.assertIn("OVER_2_5", [choice.value for choice in before_market])
        self.assertIn("2-1", [choice.value for choice in before_market])
        self.assertIn("2-0", [choice.value for choice in correct_score])
        self.assertIn("2-1", [choice.value for choice in correct_score])

    async def _autocomplete_values(self, market):
        interaction = types.SimpleNamespace(namespace=types.SimpleNamespace(market=market))
        choices = await self.module.world_cup_selection_autocomplete(interaction, "")
        return [choice.value for choice in choices]

    def test_selection_autocomplete_branches_by_selected_market(self):
        one_x_two = asyncio.run(self._autocomplete_values(self.module.MARKET_1X2))
        totals = asyncio.run(self._autocomplete_values(self.module.MARKET_TOTAL_GOALS_2_5))
        score = asyncio.run(self._autocomplete_values(self.module.MARKET_CORRECT_SCORE))

        self.assertEqual(one_x_two, ["HOME", "DRAW", "AWAY"])
        self.assertEqual(totals, ["OVER_2_5", "UNDER_2_5"])
        self.assertIn("0-0", score)
        self.assertIn("1-1", score)

    def test_selection_and_winner_resolution_for_supported_markets(self):
        self.assertEqual(self.module.normalize_selection(self.module.MARKET_1X2, "主勝"), "HOME")
        self.assertEqual(self.module.normalize_selection(self.module.MARKET_TOTAL_GOALS_2_5, "小"), "UNDER_2_5")
        self.assertEqual(self.module.normalize_selection(self.module.MARKET_CORRECT_SCORE, "2:1"), "2-1")
        self.assertEqual(self.module.normalize_selection(self.module.MARKET_CORRECT_SCORE, "OTHER"), "OTHER")
        self.assertEqual(self.module.resolve_winning_selection(self.module.MARKET_1X2, 2, 1), "HOME")
        self.assertEqual(self.module.resolve_winning_selection(self.module.MARKET_TOTAL_GOALS_2_5, 1, 1), "UNDER_2_5")
        self.assertEqual(self.module.resolve_winning_selection(self.module.MARKET_CORRECT_SCORE, 8, 1), "OTHER")

    def test_fixed_odds_defaults_are_available_for_supported_markets(self):
        self.assertEqual(self.module.fixed_odds_bps(self.module.MARKET_1X2, "HOME"), 200)
        self.assertEqual(self.module.fixed_odds_bps(self.module.MARKET_1X2, "DRAW"), 300)
        self.assertEqual(self.module.fixed_odds_bps(self.module.MARKET_TOTAL_GOALS_2_5, "OVER_2_5"), 190)
        self.assertEqual(self.module.fixed_odds_bps(self.module.MARKET_CORRECT_SCORE, "2-1"), 800)
        self.assertEqual(self.module.fixed_odds_bps(self.module.MARKET_CORRECT_SCORE, "OTHER"), 400)

    def test_configured_admin_user_id_can_use_admin_commands_without_manage_guild(self):
        interaction = type(
            "Interaction",
            (),
            {
                "user": type(
                    "User",
                    (),
                    {
                        "id": 628547257309986816,
                        "guild_permissions": type("Permissions", (), {"manage_guild": False})(),
                    },
                )()
            },
        )()
        with patch.dict(os.environ, {"WORLD_CUP_BETTING_ADMIN_USER_IDS": "628547257309986816"}, clear=False):
            self.assertTrue(self.module._is_world_cup_admin(interaction))

    def test_bet_confirmation_includes_match_market_selection_and_amount(self):
        message = self.module._format_bet_confirmation(
            match={
                "id": 12,
                "kickoff_at": "2026-06-26T12:00:00+00:00",
                "home_team": "Argentina",
                "away_team": "Japan",
            },
            match_id=12,
            market=self.module.MARKET_CORRECT_SCORE,
            selection="2-1",
            amount=500,
            balance=19500,
        )

        self.assertIn("下注成功", message)
        self.assertIn("#12", message)
        self.assertIn("Argentina vs Japan", message)
        self.assertIn("正確比分", message)
        self.assertIn("2-1", message)
        self.assertIn("500", message)
        self.assertIn("19500", message)

    def test_pool_settlement_distributes_tokens_by_winning_stake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "worldcup.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = self._repository(sqlite_path)
                now = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
                kickoff = now + timedelta(hours=4)
                repository.upsert_matches("guild", [self._match_payload(kickoff_at=kickoff.isoformat())])
                match_id = repository.list_today_matches("guild", now=now)[0]["id"]
                for user in ("alice", "bob", "charlie"):
                    repository.claim_daily("guild", user, 20000, now=now)

                repository.place_bet("guild", "alice", match_id, self.module.MARKET_1X2, "HOME", 100, lock_minutes=10, now=now)
                repository.place_bet("guild", "bob", match_id, self.module.MARKET_1X2, "HOME", 300, lock_minutes=10, now=now)
                repository.place_bet("guild", "charlie", match_id, self.module.MARKET_1X2, "AWAY", 600, lock_minutes=10, now=now)
                repository.upsert_matches(
                    "guild",
                    [
                        self._match_payload(
                            kickoff_at=kickoff.isoformat(),
                            status="FINISHED",
                            home_score=2,
                            away_score=1,
                        )
                    ],
                )

                results = repository.settle_match("guild", match_id, settled_by="admin")
                one_x_two = [result for result in results if result.market == self.module.MARKET_1X2][0]

                self.assertEqual(one_x_two.winning_selection, "HOME")
                self.assertEqual(one_x_two.total_pool, 1000)
                self.assertEqual(one_x_two.winning_pool, 400)
                self.assertEqual(one_x_two.winner_count, 2)
                self.assertEqual(repository.get_player("guild", "alice")["balance"], 20250)
                self.assertEqual(repository.get_player("guild", "bob")["balance"], 20750)
                self.assertEqual(repository.get_player("guild", "charlie")["balance"], 19400)
                repository.close()

    def test_settlement_keeps_losing_pool_when_there_are_no_winners_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "worldcup.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = self._repository(sqlite_path)
                now = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
                kickoff = now + timedelta(hours=4)
                repository.upsert_matches("guild", [self._match_payload(kickoff_at=kickoff.isoformat())])
                match_id = repository.list_today_matches("guild", now=now)[0]["id"]
                repository.claim_daily("guild", "alice", 20000, now=now)
                repository.place_bet(
                    "guild",
                    "alice",
                    match_id,
                    self.module.MARKET_CORRECT_SCORE,
                    "0-0",
                    500,
                    lock_minutes=10,
                    now=now,
                )
                repository.upsert_matches(
                    "guild",
                    [
                        self._match_payload(
                            kickoff_at=kickoff.isoformat(),
                            status="FINISHED",
                            home_score=1,
                            away_score=1,
                        )
                    ],
                )

                first = repository.settle_match("guild", match_id, settled_by="admin")
                second = repository.settle_match("guild", match_id, settled_by="admin")
                correct_score = [result for result in first if result.market == self.module.MARKET_CORRECT_SCORE][0]

                self.assertEqual(correct_score.refunded_count, 0)
                self.assertEqual(repository.get_player("guild", "alice")["balance"], 19500)
                self.assertTrue(all(result.already_settled for result in second))
                self.assertEqual(repository.get_player("guild", "alice")["balance"], 19500)
                repository.close()

    def test_auto_sync_settles_finished_matches_and_announces_payouts(self):
        class FakeFootballClient:
            def __init__(self, payloads):
                self.payloads = payloads

            def fetch_matches(self):
                return self.payloads

        class FakeChannel:
            def __init__(self):
                self.messages = []

            async def send(self, content):
                self.messages.append(content)

        class FakeGuild:
            def __init__(self, channel):
                self.id = "guild"
                self.system_channel = channel
                self.text_channels = [channel]

        class FakeBot:
            def __init__(self, guild):
                self.guilds = [guild]

            async def wait_until_ready(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "worldcup.db")
            with patch.dict(
                os.environ,
                {
                    "DB_TYPE": "sqlite",
                    "SQLITE_PATH": sqlite_path,
                    "WORLD_CUP_AUTO_SETTLEMENT_ENABLED": "0",
                },
                clear=False,
            ):
                repository = self._repository(sqlite_path)
                now = datetime(2026, 6, 10, 1, 0, tzinfo=timezone.utc)
                kickoff = now + timedelta(hours=4)
                repository.upsert_matches("guild", [self._match_payload(kickoff_at=kickoff.isoformat())])
                match_id = repository.list_today_matches("guild", now=now)[0]["id"]
                repository.claim_daily("guild", "alice", 20000, now=now)
                repository.place_bet(
                    "guild",
                    "alice",
                    match_id,
                    self.module.MARKET_1X2,
                    "HOME",
                    100,
                    lock_minutes=10,
                    now=now,
                )

                finished_payload = self._match_payload(
                    kickoff_at=kickoff.isoformat(),
                    status="FINISHED",
                    home_score=2,
                    away_score=1,
                )
                channel = FakeChannel()
                service = self.module.WorldCupBettingService(
                    repository=repository,
                    football_client=FakeFootballClient([finished_payload]),
                )
                cog = self.module.WorldCupBettingCog(FakeBot(FakeGuild(channel)), service=service)

                asyncio.run(cog._run_auto_sync_and_settle())

                self.assertEqual(repository.get_player("guild", "alice")["balance"], 20100)
                self.assertEqual(len(channel.messages), 1)
                self.assertIn("世足結算完成", channel.messages[0])
                self.assertIn("<@alice> 賺 100", channel.messages[0])
                repository.close()

    def test_extension_loader_is_env_gated(self):
        install_discord_stub()
        sys.modules.pop("discord_bot.cogs", None)
        with patch.dict(os.environ, {"WORLD_CUP_BETTING_ENABLED": "0"}, clear=False):
            cogs = importlib.import_module("discord_bot.cogs")
            self.assertNotIn(cogs.WORLD_CUP_BETTING_EXTENSION, cogs.get_extensions())

        sys.modules.pop("discord_bot.cogs", None)
        with patch.dict(os.environ, {"WORLD_CUP_BETTING_ENABLED": "1"}, clear=False):
            cogs = importlib.import_module("discord_bot.cogs")
            self.assertIn(cogs.WORLD_CUP_BETTING_EXTENSION, cogs.get_extensions())


if __name__ == "__main__":
    unittest.main()
