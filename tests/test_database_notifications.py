import ast
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.support import install_discord_stub

install_discord_stub()

from discord_bot.db.health import DatabaseHealth
from discord_bot.features.notifications import database as notifications


class DatabaseHealthTests(unittest.TestCase):
    def test_initial_failure_reminder_and_recovery(self):
        health = DatabaseHealth()
        with patch("discord_bot.db.health.time.monotonic", return_value=0) as clock:
            health.succeeded("summaries", "sqlite", "initialize")
            self.assertTrue(health.events.empty())
            health.failed("summaries", "sqlite", "initialize")
            clock.return_value = 299
            health.failed("summaries", "sqlite", "initialize")
            self.assertEqual(health.events.qsize(), 1)
            clock.return_value = 300
            health.failed("summaries", "sqlite", "initialize")
            health.succeeded("summaries", "sqlite", "initialize")
            health.succeeded("summaries", "sqlite", "write")
        events = [health.events.get_nowait() for _ in range(3)]
        self.assertEqual([event.recovered for event in events], [False, False, True])
        self.assertTrue(health.events.empty())

    def test_pending_events_are_bounded_and_keep_latest_transition(self):
        health = DatabaseHealth(max_events=2)
        for _ in range(4):
            health.failed("summaries", "postgres", "write")
            health.succeeded("summaries", "postgres", "write")
        self.assertEqual(health.events.qsize(), 2)
        self.assertFalse(health.events.get_nowait().recovered)
        self.assertTrue(health.events.get_nowait().recovered)

    def test_only_safe_error_metadata_enters_event_and_message(self):
        error = RuntimeError("credential-sentinel SQL record-sentinel")
        error.pgcode = "08006"
        health = DatabaseHealth()
        health.failed("summaries", "postgres", "write", error=error)
        event = health.events.get_nowait()
        message = notifications.format_database_event(event)
        self.assertIn("RuntimeError", message)
        self.assertIn("08006", message)
        self.assertNotIn("credential-sentinel", repr(event) + message)
        self.assertNotIn("record-sentinel", repr(event) + message)
        self.assertIn("30 秒", message)
        self.assertIn("Server log", message)
        self.assertLess(len(message), 2000)
        health.succeeded("summaries", "postgres", "initialize")
        self.assertIn("不會自動補寫", notifications.format_database_event(health.events.get_nowait()))

    def test_invalid_sqlstate_is_discarded(self):
        error = RuntimeError("secret")
        error.pgcode = "postgres://credential-sentinel"
        health = DatabaseHealth()
        health.failed("summaries", "postgres", "write", error=error)
        self.assertIsNone(health.events.get_nowait().sqlstate)


class DatabaseNotificationTests(unittest.IsolatedAsyncioTestCase):
    def make_event(self):
        health = DatabaseHealth()
        health.failed("summaries", "postgres", "initialize", error=RuntimeError("private-sentinel"))
        return health, health.events.get_nowait()

    async def test_unset_channel_skips_discord_entirely(self):
        _, event = self.make_event()
        bot = Mock()
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": ""}):
            await notifications.send_database_event(bot, event)
        bot.get_channel.assert_not_called()

    async def test_fetches_configured_channel_without_source_guild_and_disables_mentions(self):
        _, event = self.make_event()
        channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1001), send=AsyncMock())
        bot = Mock(get_channel=Mock(return_value=None), fetch_channel=AsyncMock(return_value=channel))
        marker = object()
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "2001",
                                     "DISCORD_NOTIFY_FORWARD_GUILD_ID": "1001"}), \
             patch.object(notifications.discord.AllowedMentions, "none", return_value=marker):
            await notifications.send_database_event(bot, event)
        bot.fetch_channel.assert_awaited_once_with(2001)
        self.assertIs(channel.send.await_args.kwargs["allowed_mentions"], marker)
        self.assertNotIn("private-sentinel", channel.send.await_args.args[0])

    async def test_guild_mismatch_does_not_send(self):
        _, event = self.make_event()
        channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1002), send=AsyncMock())
        bot = Mock(get_channel=Mock(return_value=channel))
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "2001",
                                     "DISCORD_NOTIFY_FORWARD_GUILD_ID": "1001"}):
            await notifications.send_database_event(bot, event)
        channel.send.assert_not_awaited()

    async def test_startup_event_waits_for_ready_and_worker_survives_delivery_error(self):
        health = DatabaseHealth()
        health.failed("summaries", "sqlite", "initialize")
        ready = asyncio.Event()
        bot = types.SimpleNamespace(wait_until_ready=ready.wait)
        send = AsyncMock(side_effect=[RuntimeError("not deliverable"), None])
        with patch.object(notifications, "send_database_event", send):
            worker = asyncio.create_task(notifications.run_database_notifications(bot, health=health))
            try:
                await asyncio.sleep(0)
                send.assert_not_awaited()
                ready.set()
                health.succeeded("summaries", "sqlite", "initialize")
                await asyncio.wait_for(health.events.join(), timeout=1)
                self.assertEqual(send.await_count, 2)
                self.assertTrue(health.events.empty())
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def test_slow_delivery_times_out_without_blocking_later_events(self):
        health = DatabaseHealth()
        health.failed("summaries", "sqlite", "initialize")
        health.succeeded("summaries", "sqlite", "initialize")
        bot = types.SimpleNamespace(wait_until_ready=AsyncMock())
        calls = []

        async def send(_bot, event):
            calls.append(event)
            if not event.recovered:
                await asyncio.Event().wait()

        with patch.object(notifications, "send_database_event", send), \
             patch.object(notifications, "NOTIFICATION_TIMEOUT_SECONDS", 0.01):
            worker = asyncio.create_task(notifications.run_database_notifications(bot, health=health))
            try:
                await asyncio.wait_for(health.events.join(), timeout=1)
                self.assertEqual([event.recovered for event in calls], [False, True])
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def test_bot_lifecycle_starts_one_worker_and_cancels_it(self):
        # Load only the class, avoiding bot.py's dotenv/bootstrap side effects.
        tree = ast.parse((Path(__file__).resolve().parents[1] / "discord_bot/bot.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DiscordSummaryBot")

        class BaseBot:
            def __init__(self, **kwargs):
                pass

            async def close(self):
                self.closed = True

        worker_started = asyncio.Event()

        async def worker(bot):
            worker_started.set()
            await asyncio.Event().wait()

        namespace = {"commands": types.SimpleNamespace(Bot=BaseBot), "asyncio": asyncio,
                     "suppress": suppress, "load_extensions": AsyncMock(), "run_database_notifications": worker,
                     "discord": types.SimpleNamespace(Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace()),
                                                      AllowedMentions=types.SimpleNamespace(none=lambda: None))}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), namespace)
        bot = namespace["DiscordSummaryBot"]()
        await bot.setup_hook()
        task = bot._database_notifications_task
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        await bot.setup_hook()
        self.assertIs(bot._database_notifications_task, task)
        await bot.close()
        self.assertTrue(task.cancelled())
        self.assertTrue(bot.closed)
        self.assertIsNone(bot._database_notifications_task)


if __name__ == "__main__":
    unittest.main()
