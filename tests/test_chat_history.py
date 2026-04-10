import unittest
from datetime import datetime, timezone

from tests.support import install_discord_stub, reload_module

install_discord_stub()
history = reload_module("discord_bot.features.chat.history")


class FakeAuthor:
    def __init__(self, *, name: str, display_name: str, bot: bool = False):
        self.name = name
        self.display_name = display_name
        self.bot = bot


class FakeMessage:
    def __init__(self, *, name: str, display_name: str, content: str, created_at: datetime, bot: bool = False):
        self.author = FakeAuthor(name=name, display_name=display_name, bot=bot)
        self.content = content
        self.created_at = created_at


class FakeChannel:
    def __init__(self, messages):
        self._messages = list(messages)

    async def history(self, *, limit, after=None, oldest_first=False):
        emitted = 0
        for message in self._messages:
            if after and message.created_at <= after:
                continue
            yield message
            emitted += 1
            if emitted >= limit:
                break


class ChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_non_bot_messages_skips_bots_and_respects_limit(self):
        now = datetime.now(timezone.utc)
        channel = FakeChannel(
            [
                FakeMessage(name="bot1", display_name="Bot 1", content="ignore", created_at=now, bot=True),
                FakeMessage(name="u1", display_name="User 1", content="first", created_at=now),
                FakeMessage(name="u2", display_name="User 2", content="second", created_at=now),
                FakeMessage(name="u3", display_name="User 3", content="third", created_at=now),
            ]
        )

        messages = await history.collect_non_bot_messages(channel, limit=2, fetch_multiplier=2.0)

        self.assertEqual([message.content for message in messages], ["first", "second"])

    async def test_collect_non_bot_messages_applies_after_filter(self):
        older = datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc)
        channel = FakeChannel(
            [
                FakeMessage(name="u1", display_name="User 1", content="old", created_at=older),
                FakeMessage(name="u2", display_name="User 2", content="new", created_at=newer),
            ]
        )

        messages = await history.collect_non_bot_messages(channel, limit=5, after=older)

        self.assertEqual([message.content for message in messages], ["new"])

    def test_format_message_history_can_include_author_id(self):
        messages = [
            FakeMessage(
                name="alpha",
                display_name="Alpha",
                content="第一句",
                created_at=datetime(2026, 4, 10, 10, 5, tzinfo=timezone.utc),
            ),
            FakeMessage(
                name="beta",
                display_name="Beta",
                content="第二句",
                created_at=datetime(2026, 4, 10, 10, 6, tzinfo=timezone.utc),
            ),
        ]

        formatted = history.format_message_history(messages, include_author_id=True)

        self.assertIn("[id:alpha] Alpha: 第一句", formatted)
        self.assertIn("[id:beta] Beta: 第二句", formatted)

    def test_truncate_for_discord_preserves_suffix_within_limit(self):
        text = "x" * 30

        result = history.truncate_for_discord(text, limit=20, suffix="...(cut)")

        self.assertEqual(result, ("x" * 12) + "...(cut)")
        self.assertEqual(len(result), 20)


if __name__ == "__main__":
    unittest.main()
