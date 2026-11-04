"""Monthly report persistence on a temporary SQLite file shared by two "processes"."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.db.social_monthly_report_repository import LostClaim, SocialMonthlyReportRepository
from discord_bot.features.monthly_report.periods import month_bounds

LO, HI = month_bounds("2026-10")


def insert_share(repo, *, guild="1", message="100", at=LO + 10, user="7", post="P"):
    repo.execute("INSERT INTO social_share_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (guild, "10", "threads", message, user, post, f"https://example.com/{post}", message, at),
                 write=True)


def insert_patrol(repo, *, guild="1", message="101", at=LO + 20, status="sent", user="8"):
    repo.execute(
        "INSERT INTO social_patrol_events (event_id, guild_id, channel_id, user_id, display_name, platform, "
        "post_key, canonical_url, source_message_id, current_preview_message_id, first_source_message_id, "
        "first_user_id, reference_message_id, share_count, occurred_at, delivery_status) "
        "VALUES (?, ?, '10', ?, '範例', 'threads', 'P', 'https://example.com/P', ?, ?, '100', '7', '100', 2, ?, ?)",
        (f"e{message}", guild, user, message, message, at, status), write=True)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite",
                                           "SQLITE_PATH": str(Path(self.temp.name) / "report.db")})
        self.env.start()
        self.a = SocialMonthlyReportRepository(retry_interval_seconds=0)
        self.b = SocialMonthlyReportRepository(retry_interval_seconds=0)

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.env.stop()
        self.temp.cleanup()

    def claim(self, repo, token, now=1000.0, channel="55"):
        return repo.claim_run("1", "2026-10", token=token, report_id="rid", period_start=LO, period_end=HI,
                              channel_id=channel, now=now, lease_seconds=600)

    def test_month_events_only_reads_this_guild_and_period(self):
        insert_share(self.a, message="100")
        insert_share(self.a, message="200", guild="2")
        insert_share(self.a, message="300", at=HI)
        insert_patrol(self.a, message="101")
        insert_patrol(self.a, message="201", guild="2")
        shares, patrols = self.a.month_events("1", LO, HI)
        self.assertEqual([row[2] for row in shares], ["100"])
        self.assertEqual([row[0] for row in patrols], ["e101"])
        # The snapshot leaves no open transaction or writes behind.
        self.assertFalse(self.a.conn.in_transaction)

    def test_settings_default_disabled_and_saved_settings_are_kept(self):
        settings = self.a.ensure_settings("1", first_report_month="2026-11", now=5.0)
        self.assertEqual((settings["enabled"], settings["channel_id"], settings["first_report_month"]),
                         (False, None, "2026-11"))
        self.assertEqual(self.b.ensure_settings("1", first_report_month="2027-01")["first_report_month"], "2026-11")
        saved = self.a.save_settings("1", enabled=False, channel_id=77, first_report_month="2026-11", updated_by=42)
        self.assertEqual((saved["enabled"], saved["channel_id"], saved["updated_by"]), (False, "77", "42"))

    def test_enabled_guilds_lists_only_enabled_guilds_whose_first_month_arrived(self):
        self.assertEqual(self.a.enabled_guilds("2026-10"), [])
        self.a.save_settings("1", enabled=True, channel_id=55, first_report_month="2026-10", updated_by=None)
        self.a.save_settings("2", enabled=False, channel_id=66, first_report_month="2026-10", updated_by=None)
        self.a.save_settings("3", enabled=True, channel_id=77, first_report_month="2026-11", updated_by=None)
        self.a.save_settings("4", enabled=True, channel_id=88, first_report_month="2026-09", updated_by=None)
        self.assertEqual(self.b.enabled_guilds("2026-10"), ["1", "4"])
        self.assertEqual(self.b.enabled_guilds("2026-11"), ["1", "3", "4"])

    def test_claim_is_atomic_across_connections_and_expired_lease_can_be_taken_over(self):
        run = self.claim(self.a, "token-a")
        self.assertEqual((run["status"], run["channel_id"], run["attempts"]), ("pending", "55", 1))
        self.assertIsNone(self.claim(self.b, "token-b", now=1500.0))
        taken = self.claim(self.b, "token-b", now=1601.0, channel="99")
        self.assertEqual((taken["claim_token"], taken["channel_id"], taken["attempts"]), ("token-b", "55", 2))
        with self.assertRaises(LostClaim):
            self.a.begin_page("1", "2026-10", "token-a", 0, lease_expires_at=2000.0)

    def test_pages_progress_and_terminal_states_are_not_reclaimed(self):
        self.claim(self.a, "t")
        self.a.store_pages("1", "2026-10", "t", ["一", "二"], lease_expires_at=2000.0)
        self.a.begin_page("1", "2026-10", "t", 1, lease_expires_at=2000.0)
        run = self.b.get_run("1", "2026-10")
        self.assertEqual((run["pages"], run["message_ids"], run["sending_page"]), (["一", "二"], [None, None], 1))
        self.a.confirm_page("1", "2026-10", "t", ["900", None])
        self.a.finish("1", "2026-10", "t", status="unknown", error_type="Demo")
        run = self.b.get_run("1", "2026-10")
        self.assertEqual((run["status"], run["message_ids"], run["claim_token"]), ("unknown", ["900", None], None))
        self.assertIsNone(self.claim(self.b, "late", now=99999.0))
        with self.assertRaises(LostClaim):
            self.a.confirm_page("1", "2026-10", "t", ["900", "901"])
        self.assertEqual([item["report_month"] for item in self.a.latest_runs("1")], ["2026-10"])

    def test_invalid_finish_status_is_rejected(self):
        self.claim(self.a, "t")
        with self.assertRaises(ValueError):
            self.a.finish("1", "2026-10", "t", status="pending")


if __name__ == "__main__":
    unittest.main()
