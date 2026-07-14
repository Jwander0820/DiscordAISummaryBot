from types import SimpleNamespace
import unittest

from discord_bot.features.deepfaker.records import build_deepfaker_event


class DeepfakerRecordTests(unittest.TestCase):
    def test_build_event_supports_bot_target_and_identity_snapshots(self):
        guild = SimpleNamespace(id=1001, name="測試伺服器")
        channel = SimpleNamespace(id=2002, name="敏感頻道")
        actor = SimpleNamespace(
            id=3003,
            name="actor_account",
            global_name="操作者全域名稱",
            display_name="操作者暱稱",
            bot=False,
        )
        target = SimpleNamespace(
            id=4004,
            name="target_bot",
            global_name=None,
            display_name="目標機器人",
            bot=True,
        )

        record = build_deepfaker_event(
            guild=guild,
            channel=channel,
            actor=actor,
            target=target,
            outcome_success=False,
            failure_probability=0.05,
            random_roll=0.01,
            requested_content="原始台詞",
            webhook_content="偽裝失敗後實際送出的內容",
            failure_notice="抓到你了！",
            failure_exposed_content="這個人想偽裝成目標機器人說原始台詞",
            delivery_status="sent",
            occurred_at="2026-07-14T12:00:00+00:00",
        )

        self.assertEqual(record["guild_id"], "1001")
        self.assertEqual(record["channel_id"], "2002")
        self.assertEqual(record["actor_user_id"], "3003")
        self.assertEqual(record["target_user_id"], "4004")
        self.assertEqual(record["target_username"], "target_bot")
        self.assertEqual(record["target_display_name"], "目標機器人")
        self.assertIsNone(record["target_global_name"])
        self.assertTrue(record["target_is_bot"])
        self.assertFalse(record["outcome_success"])
        self.assertEqual(record["random_roll"], 0.01)


if __name__ == "__main__":
    unittest.main()
