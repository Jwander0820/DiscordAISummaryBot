import unittest
from unittest.mock import MagicMock, patch

from tools import send_patrol_preview_test as tool
from tools.send_patrol_preview_test import build_patrol_payloads


class PatrolPayloadTests(unittest.TestCase):
    def test_final_style_preserves_card_but_omits_simulation_labels(self):
        v2, embed = build_patrol_payloads("https://example.com/dog.gif", "<@123>",
                                          "https://example.com", avatar_url="https://example.com/dog.png",
                                          share_count=2, final_style=True)
        card = v2["components"][0]
        self.assertEqual(len(card["components"]), 1)
        self.assertEqual(card["components"][0]["components"][0]["content"], "**不看訊息警察**")
        self.assertEqual(card["components"][0]["components"][2]["content"],
                         "⬆️ [查看原始連結](https://example.com)")
        self.assertEqual(v2["avatar_url"], "https://example.com/dog.png")
        self.assertNotIn("footer", embed["embeds"][0])

    def test_local_destination_and_cli_priority_do_not_mutate_environment(self):
        path = MagicMock()
        path.is_file.return_value = True
        path.read_text.return_value = '{"channel_id":"111","guild_id":"222"}'
        original = dict(zip(tool.KEYS, ("fake-token", "333", "444")))
        with patch.object(tool, "LOCAL_SETTINGS_PATH", path), \
                patch.object(tool, "load_settings", side_effect=lambda: original.copy()), \
                patch.object(tool, "send_demo", new_callable=MagicMock) as send, patch.object(tool.asyncio, "run"):
            self.assertEqual(tool.main(["--gif-url", "https://example.com/dog.gif", "--send",
                                       "--channel-id", "555", "--final-style", "--layout", "v2"]), 0)
            passed = send.call_args.args[0]
            self.assertEqual(passed[tool.KEYS[1]], "555")
            self.assertEqual(passed[tool.KEYS[2]], "222")
            self.assertEqual(original[tool.KEYS[1]], "333")
            self.assertTrue(send.call_args.kwargs["final_style"])

    def test_v2_and_embed_same_identity_text_and_media(self):
        media = "https://example.com/alarm.gif"
        v2, embed = build_patrol_payloads(media, "<@123>", "https://example.com", avatar_gif=True)
        for payload in (v2, embed):
            self.assertEqual(payload["username"], "不看訊息警察")
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertEqual(payload["avatar_url"], media)
        self.assertEqual(v2["flags"], 32768)
        self.assertNotIn("embeds", v2)
        self.assertNotIn("content", v2)
        section = v2["components"][0]["components"][0]
        self.assertIn("<@123>", section["components"][1]["content"])
        self.assertEqual(section["accessory"]["media"]["url"], media)
        self.assertEqual(embed["embeds"][0]["thumbnail"]["url"], media)
        self.assertIn("<@123>", embed["embeds"][0]["description"])

    def test_avatar_override_optional(self):
        for payload in build_patrol_payloads("https://example.com/test.gif", "@測試對象", "https://example.com"):
            self.assertNotIn("avatar_url", payload)

    def test_only_explicit_user_allowed(self):
        for payload in build_patrol_payloads("https://example.com/test.gif", "<@123>",
                                             "https://example.com", allow_user="123"):
            self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": ["123"]})
