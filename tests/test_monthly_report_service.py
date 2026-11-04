"""Scheduling, atomic claims and delivery recovery with a fake Discord transport."""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.db.social_monthly_report_repository import SocialMonthlyReportRepository
from discord_bot.features.monthly_report.service import (
    DeliveryRejected, DeliveryUncertain, MonthlyReportService)
from tests.test_monthly_report_repository import LO, insert_patrol, insert_share

NOV_15 = datetime(2026, 11, 15, 4, tzinfo=timezone.utc).timestamp()
NOV_4 = datetime(2026, 11, 4, 4, tzinfo=timezone.utc).timestamp()


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.attempts = 0
        self.effects = []
        self.notices = []
        self.find_error = None
        self.hide_delivered = False
        self.release = None
        self.guilds = None  # None means the bot is in every guild.

    def has_guild(self, guild_id):
        return self.guilds is None or str(guild_id) in self.guilds

    def _post(self, channel, content):
        message_id = str(9000 + len(self.sent))
        self.sent.append((channel, content, message_id))
        return message_id

    async def send_page(self, guild_id, channel_id, content):
        self.attempts += 1
        if self.release is not None:
            await self.release.wait()
        effect = self.effects.pop(0) if self.effects else None
        if effect == "delivered-but-uncertain":
            self._post(channel_id, content)
            raise DeliveryUncertain("Timeout")
        if isinstance(effect, Exception):
            raise effect
        return self._post(channel_id, content)

    async def find_page(self, guild_id, channel_id, content, *, after):
        if self.find_error is not None:
            raise self.find_error
        if self.hide_delivered:
            return None
        return next((mid for channel, text, mid in self.sent if channel == channel_id and text == content), None)

    async def resolve_names(self, guild_id, user_ids):
        return {}

    async def notify_admin(self, text):
        self.notices.append(text)
        return True


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DB_TYPE": "sqlite",
                                           "SQLITE_PATH": str(Path(self.temp.name) / "report.db")})
        self.env.start()
        self.now = NOV_15
        self.repos = []
        self.service = self.make_service()
        self.transport = FakeTransport()

    async def asyncTearDown(self):
        for repo in self.repos:
            repo.close()
        self.env.stop()
        self.temp.cleanup()

    def make_service(self, **kwargs):
        repo = SocialMonthlyReportRepository(retry_interval_seconds=0)
        self.repos.append(repo)
        return MonthlyReportService(repo, clock=lambda: self.now, recheck_delay=0, **kwargs)

    def seed(self, sharers=1, guild="1"):
        for index in range(sharers):
            insert_share(self.service.repository, message=str(100 + index), user=str(1000 + index), guild=guild)
        insert_patrol(self.service.repository, message="5000" if guild == "1" else f"5000{guild}", guild=guild)

    def enable_from(self, month="2026-10", channel="55", guild="1"):
        self.service.repository.save_settings(guild, enabled=True, channel_id=channel,
                                              first_report_month=month, updated_by=None)

    def stored_run(self, month="2026-10", guild="1"):
        return self.service.repository.get_run(guild, month)

    async def test_disabled_by_default_and_enabling_starts_at_next_boundary(self):
        self.now = NOV_4
        self.seed()
        delay = await self.service.tick(self.transport)
        # A guild nobody configured has no settings row and is never scheduled.
        self.assertIsNone(self.service.repository.get_settings("1"))
        self.assertIsNone(self.stored_run())
        settings = await self.service.settings("1")
        self.assertEqual((settings["enabled"], settings["channel_id"]), (False, None))
        with self.assertRaises(ValueError):
            await self.service.configure("1", state="enabled", updated_by="42")
        settings = await self.service.configure("1", state="enabled", channel_id=55, updated_by="42")
        self.assertEqual((settings["enabled"], settings["channel_id"], settings["first_report_month"]),
                         (True, "55", "2026-11"))
        await self.service.tick(self.transport)
        self.assertIsNone(self.stored_run())
        self.assertEqual(self.transport.sent, [])
        boundary = datetime(2026, 11, 30, 16, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(delay, boundary - NOV_4 + 1)

    async def test_catch_up_after_downtime_sends_once_with_fixed_content(self):
        self.seed()
        self.enable_from("2026-10")
        await self.service.tick(self.transport)
        run = self.stored_run()
        self.assertEqual(run["status"], "sent")
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0][0], "55")
        self.assertEqual(run["message_ids"], ["9000"])
        self.assertEqual(run["pages"][0], self.transport.sent[0][1])
        # A restarted process with a fresh memory does not resend the settled month.
        await self.make_service().tick(self.transport)
        self.assertEqual(len(self.transport.sent), 1)

    async def test_empty_month_is_settled_without_sending(self):
        self.enable_from("2026-10")
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport, scheduled=True), "empty")
        self.assertEqual((self.stored_run()["status"], self.transport.sent), ("empty", []))

    async def test_disabled_guild_is_not_scheduled(self):
        self.seed()
        self.service.repository.save_settings("1", enabled=False, channel_id="55",
                                              first_report_month="2026-10", updated_by=None)
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport, scheduled=True),
                         "disabled")
        await self.service.tick(self.transport)
        self.assertEqual((self.stored_run(), self.transport.sent), (None, []))

    async def test_every_enabled_guild_gets_its_own_report(self):
        self.seed(guild="1")
        self.seed(guild="2")
        self.enable_from("2026-10", channel="55", guild="1")
        self.enable_from("2026-10", channel="66", guild="2")
        self.enable_from("2026-11", channel="77", guild="3")  # Enabled this month: first report next month.
        await self.service.tick(self.transport)
        self.assertEqual(sorted(channel for channel, _, _ in self.transport.sent), ["55", "66"])
        self.assertEqual((self.stored_run(guild="1")["status"], self.stored_run(guild="2")["status"]),
                         ("sent", "sent"))
        self.assertIsNone(self.stored_run(guild="3"))

    async def test_one_failing_guild_does_not_block_others_and_is_retried(self):
        self.seed(guild="1")
        self.seed(guild="2")
        self.enable_from("2026-10", channel="55", guild="1")
        self.enable_from("2026-10", channel="66", guild="2")
        repo = self.service.repository
        original = repo.month_events

        def flaky(guild_id, *args):
            if guild_id == "1":
                raise RuntimeError("db read failed")
            return original(guild_id, *args)
        repo.month_events = flaky
        self.assertEqual(await self.service.tick(self.transport), self.service.retry_seconds)
        self.assertEqual([channel for channel, _, _ in self.transport.sent], ["66"])
        self.assertEqual(self.stored_run(guild="1")["status"], "pending")
        repo.month_events = original
        await self.service.tick(self.transport)
        self.assertEqual(sorted(channel for channel, _, _ in self.transport.sent), ["55", "66"])

    async def test_guild_the_bot_left_is_skipped_without_a_run(self):
        self.seed()
        self.enable_from("2026-10")
        self.transport.guilds = set()
        delay = await self.service.tick(self.transport)
        self.assertGreater(delay, self.service.retry_seconds)
        self.assertEqual((self.stored_run(), self.transport.sent, self.transport.notices), (None, [], []))

    async def test_settled_month_skips_database_until_enabling_changes(self):
        self.seed()
        self.enable_from("2026-10")
        await self.service.tick(self.transport)
        calls = []
        original = self.service.repository.enabled_guilds
        self.service.repository.enabled_guilds = lambda month: calls.append(month) or original(month)
        await self.service.tick(self.transport)
        self.assertEqual(calls, [])
        await self.service.configure("2", state="enabled", channel_id=66, updated_by="42")
        await self.service.tick(self.transport)
        self.assertEqual(calls, ["2026-10"])
        self.assertEqual(len(self.transport.sent), 1)

    async def test_override_channel_and_destination_fixed_when_period_starts(self):
        self.seed()
        self.enable_from("2026-10", channel="77")
        repo = self.service.repository
        repo.claim_run("1", "2026-10", token="old", report_id="rid", period_start=LO, period_end=LO + 1,
                       channel_id="77", now=self.now - 1000, lease_seconds=60)
        await self.service.configure("1", channel_id="88", updated_by="42")
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "sent")
        self.assertEqual(self.transport.sent[0][0], "77")
        self.assertEqual(self.service.effective_channel(repo.get_settings("1")), "88")

    async def test_reenabling_starts_from_next_boundary(self):
        self.enable_from("2026-09")
        await self.service.configure("1", state="disabled", updated_by="42")
        settings = await self.service.configure("1", state="enabled", updated_by="42")
        self.assertEqual((settings["first_report_month"], settings["channel_id"]), ("2026-11", "55"))
        with self.assertRaises(ValueError):
            await self.service.configure("1", state="default_channel", updated_by="42")

    async def test_partial_delivery_is_recorded_and_left_for_manual_handling(self):
        self.seed(sharers=150)
        self.enable_from("2026-10")
        self.transport.effects = [None, DeliveryRejected("Forbidden")]
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport, scheduled=True), "failed")
        run = self.stored_run()
        self.assertGreater(len(run["pages"]), 2)
        self.assertEqual(run["message_ids"][:2], ["9000", None])
        self.assertEqual(run["error_type"], "Forbidden")
        self.assertIn("1/", self.transport.notices[-1])
        self.assertIn("手動處理", self.transport.notices[-1])
        await self.make_service().tick(self.transport)
        self.assertEqual(len(self.transport.sent), 1)

    async def test_uncertain_send_found_in_channel_is_not_resent(self):
        self.seed()
        self.enable_from("2026-10")
        self.transport.effects = ["delivered-but-uncertain"]
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "sent")
        self.assertEqual((len(self.transport.sent), self.stored_run()["message_ids"]), (1, ["9000"]))

    async def test_uncertain_send_confirmed_absent_retries_same_content_limited(self):
        self.seed()
        self.enable_from("2026-10")
        self.transport.effects = [DeliveryUncertain("x")]
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "sent")
        self.assertEqual((self.transport.attempts, len(self.transport.sent)), (2, 1))

        self.enable_from("2026-09")
        insert_share(self.service.repository, message="77", at=LO - 86400)
        self.transport.effects = [DeliveryUncertain("x")] * 3
        self.assertEqual(await self.service.process_month("1", "2026-09", self.transport), "failed")
        self.assertEqual(self.stored_run("2026-09")["error_type"], "RetryExhausted")

    async def test_uncertain_send_with_unreadable_history_becomes_unknown(self):
        self.seed()
        self.enable_from("2026-10")
        self.transport.effects = [DeliveryUncertain("x")]
        self.transport.find_error = DeliveryRejected("MissingPermissions")
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "unknown")
        self.assertIn("unknown", self.transport.notices[-1])

    async def test_restart_between_send_and_record_reconciles_by_bot_message(self):
        self.seed()
        self.enable_from("2026-10")
        self.transport.effects = ["delivered-but-uncertain"]
        self.transport.hide_delivered = True
        self.transport.find_error = None
        # Simulate a process that sent page 0 and stopped before recording it.
        repo = self.service.repository
        repo.claim_run("1", "2026-10", token="dead", report_id="rid", period_start=LO, period_end=LO + 1,
                       channel_id="55", now=self.now - 1000, lease_seconds=60)
        repo.store_pages("1", "2026-10", "dead", ["固定頁面"], lease_expires_at=self.now - 900)
        repo.begin_page("1", "2026-10", "dead", 0, lease_expires_at=self.now - 900)
        self.transport._post("55", "固定頁面")
        self.transport.hide_delivered = False
        self.assertEqual(await self.make_service().process_month("1", "2026-10", self.transport), "sent")
        self.assertEqual((len(self.transport.sent), self.stored_run()["message_ids"]), (1, ["9000"]))

    async def test_restart_without_matching_message_waits_for_admin(self):
        self.seed()
        self.enable_from("2026-10")
        repo = self.service.repository
        repo.claim_run("1", "2026-10", token="dead", report_id="rid", period_start=LO, period_end=LO + 1,
                       channel_id="55", now=self.now - 1000, lease_seconds=60)
        repo.store_pages("1", "2026-10", "dead", ["固定頁面"], lease_expires_at=self.now - 900)
        repo.begin_page("1", "2026-10", "dead", 0, lease_expires_at=self.now - 900)
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "unknown")
        self.assertEqual(self.transport.sent, [])
        self.assertIn("不會自動重送", self.transport.notices[-1])
        # Scheduled ticks leave the unknown run alone.
        await self.make_service().tick(self.transport)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.stored_run()["status"], "unknown")

    async def test_two_workers_on_one_database_send_once(self):
        self.seed()
        self.enable_from("2026-10")
        other = self.make_service()
        self.transport.release = asyncio.Event()
        first = asyncio.create_task(self.service.process_month("1", "2026-10", self.transport, scheduled=True))
        await asyncio.sleep(0.2)
        second = await other.process_month("1", "2026-10", self.transport, scheduled=True)
        self.transport.release.set()
        self.assertEqual((await first, second), ("sent", "busy"))
        self.assertEqual(len(self.transport.sent), 1)

    async def test_record_failure_after_send_is_reconciled_later(self):
        self.seed()
        self.enable_from("2026-10")
        repo = self.service.repository
        original = repo.confirm_page

        def broken(*args, **kwargs):
            raise RuntimeError("db down")
        repo.confirm_page = broken
        with self.assertRaises(RuntimeError):
            await self.service.process_month("1", "2026-10", self.transport)
        self.assertIn("RecordFailed", self.transport.notices[-1])
        repo.confirm_page = original
        self.assertEqual(await self.service.process_month("1", "2026-10", self.transport), "busy")
        self.now += 601
        self.assertEqual(await self.make_service().process_month("1", "2026-10", self.transport), "sent")
        self.assertEqual(len(self.transport.sent), 1)

    async def test_database_failure_listing_guilds_retries_later(self):
        def broken(month):
            raise RuntimeError("db down")
        self.service.repository.enabled_guilds = broken
        self.assertEqual(await self.service.tick(self.transport), self.service.retry_seconds)


if __name__ == "__main__":
    unittest.main()
