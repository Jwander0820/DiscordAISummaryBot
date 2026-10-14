import os
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.support import install_discord_stub, reload_module

install_discord_stub()
notify_forwarder = reload_module("discord_bot.features.notifications.discord_forwarder")


class FakeChannel:
    def __init__(self):
        self.messages = []
        self.send_kwargs = []

    async def send(self, message, **kwargs):
        self.messages.append(message)
        self.send_kwargs.append(kwargs)


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
        self.assertIn("allowed_mentions", channel.send_kwargs[0])
        self.assertIn("SERN Notify 轉發", channel.messages[0])
        self.assertIn("Email: `sent`", channel.messages[0])
        self.assertIn("問題: `問題`", channel.messages[0])
        self.assertIn("使用者: `alice`", channel.messages[0])
        self.assertIn("來源頻道: `general`", channel.messages[0])

    async def test_explicit_source_names_use_nickname_then_global_name_then_username(self):
        record = {"user_id": "10", "channel_id": "20", "command": "Facebook 預覽"}
        original = dict(record)
        for user, expected in (
            (types.SimpleNamespace(nick="暱稱", global_name="全域名稱", name="帳號"), "暱稱（10）"),
            (types.SimpleNamespace(nick=None, global_name="全域名稱", name="帳號"), "全域名稱（10）"),
            (types.SimpleNamespace(nick=None, global_name=None, name="帳號"), "帳號（10）"),
            (types.SimpleNamespace(), "10"),
        ):
            channel = FakeChannel()
            with self.subTest(expected=expected), patch.dict(os.environ, {
                "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "",
            }):
                self.assertTrue(await notify_forwarder.forward_notify_to_channel(
                    record=record, guild=FakeGuild(channel=channel), source_user=user,
                    source_channel=types.SimpleNamespace(name="來源頻道"),
                ))
            self.assertIn(f"使用者: `{expected}`", channel.messages[0])
            self.assertIn("來源頻道: `來源頻道（20）`", channel.messages[0])
            self.assertEqual(record, original)

    async def test_cached_names_come_from_source_guild_while_routing_uses_target_guild(self):
        channel = FakeChannel()
        source_channel = types.SimpleNamespace(name="來源討論區")
        source_guild = types.SimpleNamespace(
            id=1, get_member=Mock(return_value=types.SimpleNamespace(display_name="伺服器暱稱")),
            get_channel=Mock(return_value=source_channel), fetch_member=AsyncMock(), fetch_channel=AsyncMock(),
        )
        target_guild = FakeGuild(guild_id=99, channel=channel)
        bot = types.SimpleNamespace(get_guild=Mock(return_value=target_guild),
                                    get_user=Mock(), get_channel=Mock(), fetch_user=AsyncMock())
        record = {"user_id": "10", "channel_id": "20"}
        with patch.dict(os.environ, {
            "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "99",
        }):
            self.assertTrue(await notify_forwarder.forward_notify_to_channel(record=record, guild=source_guild, bot=bot))
        self.assertIn("使用者: `伺服器暱稱（10）`", channel.messages[0])
        self.assertIn("來源頻道: `來源討論區（20）`", channel.messages[0])
        source_guild.get_member.assert_called_once_with(10)
        source_guild.get_channel.assert_called_once_with(20)
        bot.get_guild.assert_called_once_with(99)
        bot.get_user.assert_not_called()
        bot.get_channel.assert_not_called()
        bot.fetch_user.assert_not_awaited()
        source_guild.fetch_member.assert_not_awaited()
        source_guild.fetch_channel.assert_not_awaited()

    async def test_dm_uses_cached_user_name_and_keeps_id_for_unnamed_channel(self):
        channel = FakeChannel()
        bot = types.SimpleNamespace(
            get_guild=Mock(return_value=FakeGuild(guild_id=99, channel=channel)),
            get_user=Mock(return_value=types.SimpleNamespace(global_name="私訊使用者")),
            get_channel=Mock(return_value=None), fetch_user=AsyncMock(), fetch_channel=AsyncMock(),
        )
        with patch.dict(os.environ, {
            "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "99",
        }):
            self.assertTrue(await notify_forwarder.forward_notify_to_channel(
                record={"user_id": "10", "channel_id": "20"}, bot=bot,
                source_channel=types.SimpleNamespace(name=None),
            ))
        self.assertIn("使用者: `私訊使用者（10）`", channel.messages[0])
        self.assertIn("來源頻道: `20`", channel.messages[0])
        bot.get_user.assert_called_once_with(10)
        bot.get_channel.assert_called_once_with(20)
        bot.fetch_user.assert_not_awaited()
        bot.fetch_channel.assert_not_awaited()

    async def test_names_are_bounded_single_line_inline_code_and_preserve_ids(self):
        channel = FakeChannel()
        long_name = "**名稱** `code`\n\t<@10> " + "長" * 500
        record = {"user_id": "123456789012345678", "channel_id": "234567890123456789"}
        with patch.dict(os.environ, {
            "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "",
        }):
            self.assertTrue(await notify_forwarder.forward_notify_to_channel(
                record=record, guild=FakeGuild(channel=channel),
                source_user=types.SimpleNamespace(nick=long_name),
                source_channel=types.SimpleNamespace(name=long_name),
            ))
        lines = channel.messages[0].splitlines()
        self.assertEqual(len(lines), 5)
        for line, identifier in zip(lines[1:3], record.values()):
            self.assertEqual(line.count("`"), 2)
            label = line.split("`", 2)[1]
            self.assertLessEqual(len(label), 260)
            self.assertTrue(label.endswith(f"（{identifier}）"))
            self.assertIn("′code′", label)
        self.assertLess(len(channel.messages[0]), 2000)
        self.assertIn("allowed_mentions", channel.send_kwargs[0])

    async def test_names_remove_control_and_bidi_characters_but_keep_emoji_joiners(self):
        channel = FakeChannel()
        controls = "\x00\x1b\x7f\x80\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        name = "名稱\n\t" + controls + "👩\u200d💻"
        record = {"user_id": "123456789012345678", "channel_id": "234567890123456789"}
        with patch.dict(os.environ, {
            "DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": "",
        }):
            self.assertTrue(await notify_forwarder.forward_notify_to_channel(
                record=record, guild=FakeGuild(channel=channel),
                source_user=types.SimpleNamespace(nick=name), source_channel=types.SimpleNamespace(name=name),
            ))
        diagnostic = channel.messages[0]
        self.assertIn("使用者: `名稱 👩\u200d💻（123456789012345678）`", diagnostic)
        self.assertIn("來源頻道: `名稱 👩\u200d💻（234567890123456789）`", diagnostic)
        for char in controls:
            self.assertNotIn(char, diagnostic)
        self.assertEqual(record, {"user_id": "123456789012345678", "channel_id": "234567890123456789"})

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

        with patch.dict(
            os.environ,
            {"DISCORD_NOTIFY_FORWARD_CHANNEL_ID": "123", "DISCORD_NOTIFY_FORWARD_GUILD_ID": ""},
            clear=False,
        ):
            result = await notify_forwarder.forward_notify_to_channel(record=record, guild=guild)

        self.assertTrue(result)
        self.assertEqual(len(channel.messages), 1)


if __name__ == "__main__":
    unittest.main()
