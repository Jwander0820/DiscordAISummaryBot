import unittest
from datetime import datetime

from discord_bot.features.chat.records import TZ_8, build_summary_record


class ChatRecordTests(unittest.TestCase):
    def test_build_summary_record_uses_explicit_call_time(self):
        record = build_summary_record(
            channel_id="general",
            user_id="alice",
            command="解答之書",
            question="問題",
            prompt="prompt",
            summary="summary",
            call_time="2026-04-10T10:00:00+08:00",
        )

        self.assertEqual(
            record,
            {
                "channel_id": "general",
                "user_id": "alice",
                "command": "解答之書",
                "question": "問題",
                "prompt": "prompt",
                "summary": "summary",
                "call_time": "2026-04-10T10:00:00+08:00",
            },
        )

    def test_build_summary_record_generates_tz8_timestamp_by_default(self):
        record = build_summary_record(channel_id="general", user_id="alice", command="聊那麼多誰看的完")
        parsed = datetime.fromisoformat(record["call_time"])

        self.assertEqual(parsed.tzinfo, TZ_8)
        self.assertEqual(record["question"], "")
        self.assertEqual(record["prompt"], "")
        self.assertIsNone(record["summary"])


if __name__ == "__main__":
    unittest.main()
