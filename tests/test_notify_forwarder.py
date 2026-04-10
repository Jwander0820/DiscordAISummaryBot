import os
import unittest
from unittest.mock import patch

from tests.support import install_discord_stub, reload_module

install_discord_stub()
notify_forwarder = reload_module("discord_bot.features.notifications.discord_forwarder")


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeGuild:
    def __init__(self, *, guild_id=1, channel=None, fetched_channel=None):
        self.id = guild_id
        self._channel = channel
        self._fetched_channel = fetched_channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        if self._fetched_channel is None:
            raise RuntimeError("not found")
        return self._fetched_channel


class FakeBot:
    def __init__(self, guild=None):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild


class NotifyForwarderTests(unittest.IsolatedAsyncioTestCase):
    def test_truncate_returns_empty_for_none(self):
        self.assertEqual(notify_forwarder._truncate(None), "")

    def test_resolve_int_env_rejects_non_digits(self):
        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "abc"}, clear=False):
            self.assertIsNone(notify_forwarder._resolve_int_env("DISCORD_NOTIFY_FORWARD_CHANNEL_ID"))

    async def test_forward_notify_to_channel_sends_formatted_message(self):
        channel = FakeChannel()
        guild = FakeGuild(channel=channel)
        record = {
            "user_id": "alice",
            "channel_id": "general",
            "command": "解答之書",
            "call_time": "2026-04-10T10:00:00+08:00",
            "question": "問題",
            "summary": "摘要",
        }

        with patch.dict(
            os.environ,
            {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": ""},
            clear=False,
        ):
            result = await notify_forwarder.forward_notify_to_channel(
                record=record,
                guild=guild,
                notify_type="success",
                email_sent=True,
                email_message_id="msg-1",
            )

        self.assertTrue(result)
        self.assertEqual(len(channel.messages), 1)
        self.assertIn("SERN Notify 轉發", channel.messages[0])
        self.assertIn("Email: `sent`", channel.messages[0])
        self.assertIn("問題: `問題`", channel.messages[0])

    async def test_forward_notify_to_channel_uses_bot_to_resolve_target_guild(self):
        channel = FakeChannel()
        target_guild = FakeGuild(guild_id=99, channel=channel)
        bot = FakeBot(guild=target_guild)
        record = {
            "user_id": "alice",
            "channel_id": "general",
            "command": "解答之書",
            "call_time": "2026-04-10T10:00:00+08:00",
        }

        with patch.dict(
            os.environ,
            {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "99"},
            clear=False,
        ):
            result = await notify_forwarder.forward_notify_to_channel(record=record, bot=bot)

        self.assertTrue(result)
        self.assertEqual(len(channel.messages), 1)

    async def test_forward_notify_to_channel_falls_back_to_fetch_channel(self):
        channel = FakeChannel()
        guild = FakeGuild(channel=None, fetched_channel=channel)
        record = {
            "user_id": "alice",
            "channel_id": "general",
            "command": "解答之書",
            "call_time": "2026-04-10T10:00:00+08:00",
        }

        with patch.dict(os.environ, {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123"}, clear=False):
            result = await notify_forwarder.forward_notify_to_channel(record=record, guild=guild)

        self.assertTrue(result)
        self.assertEqual(len(channel.messages), 1)


if __name__ == "__main__":
    unittest.main()
