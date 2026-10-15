import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import install_discord_stub

install_discord_stub()

from discord_bot.features.chat.history import split_for_discord
from discord_bot.cogs import conversation_cog as conversation


class DiscordReplyPagesTests(unittest.TestCase):
    def test_complete_reply_including_question_and_emoji_is_bounded_and_preserved(self):
        for question, answer in (("問" * 200, "答" * 1900), ("問🙂" * 2000, "答🪐" * 1900),
                                 ("問", "答"), ("", "")):
            with self.subTest(question_length=len(question)):
                content = f"<@2001> 問了：{question}\n\n{answer}"
                pages = split_for_discord(content)
                self.assertEqual("".join(pages), content)
                self.assertTrue(all(0 < len(page.encode("utf-16-le")) // 2 <= 2000 for page in pages))

    def test_exact_boundary_and_pair_boundary(self):
        self.assertEqual(split_for_discord("a" * 2000), ["a" * 2000])
        self.assertEqual(split_for_discord("a" * 1999 + "🙂"), ["a" * 1999, "🙂"])


class ConversationReplyLengthTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_and_webhook_send_all_pages_but_only_first_can_ping(self):
        content = "<@2001> 問了：" + "問題🙂" * 2000 + "\n\n" + "答" * 1900
        requester = object()
        interaction = types.SimpleNamespace(user=requester, followup=types.SimpleNamespace(send=AsyncMock()))
        webhook = types.SimpleNamespace(name="Amadeus", send=AsyncMock())
        channel = types.SimpleNamespace(webhooks=AsyncMock(return_value=[webhook]))
        cog = conversation.ConversationCog(None)
        no_mentions = object()
        with patch.object(conversation.discord.AllowedMentions, "none", return_value=no_mentions):
            await cog._send_requester_followup(interaction, content)
            await cog._send_as_amadeus(channel, content, requester)
        for sender in (interaction.followup.send, webhook.send):
            self.assertGreater(sender.await_count, 1)
            self.assertEqual("".join(call.args[0] for call in sender.await_args_list), content)
            self.assertTrue(all(len(call.args[0].encode("utf-16-le")) // 2 <= 2000 for call in sender.await_args_list))
            self.assertEqual(sender.await_args_list[0].kwargs["allowed_mentions"].users, [requester])
            for call in sender.await_args_list[1:]:
                self.assertIs(call.kwargs["allowed_mentions"], no_mentions)

    async def test_all_three_callbacks_send_long_question_within_limit(self):
        for method_name in ("ask_about_conversation", "answer_book", "el_psy_kongroo"):
            with self.subTest(command=method_name):
                requester = types.SimpleNamespace(mention="<@2001>", name="synthetic", global_name=None)
                interaction = types.SimpleNamespace(
                    user=requester, guild=object(), client=object(),
                    response=types.SimpleNamespace(defer=AsyncMock()),
                    followup=types.SimpleNamespace(send=AsyncMock()), delete_original_response=AsyncMock())
                webhook = types.SimpleNamespace(name="Amadeus", send=AsyncMock())
                channel = types.SimpleNamespace(name="synthetic", webhooks=AsyncMock(return_value=[webhook]))
                model = types.SimpleNamespace(generate_content_async=AsyncMock(
                    return_value=types.SimpleNamespace(parts=[object()], text="答" * 1900)))
                with patch.object(conversation, "_require_text_channel", return_value=channel), \
                     patch.object(conversation, "collect_non_bot_messages", AsyncMock(return_value=[object()])), \
                     patch.object(conversation, "format_message_history", return_value="synthetic history"), \
                     patch.object(conversation, "gemini_model", model), \
                     patch.object(conversation, "query_local_llm", AsyncMock(return_value="答" * 1900)), \
                     patch.object(conversation, "call_cloud_llm", AsyncMock(return_value="答" * 1900)), \
                     patch.object(conversation.summary_repository, "insert_summary", return_value=True), \
                     patch.object(conversation.notification_service, "dispatch", AsyncMock()):
                    cog = conversation.ConversationCog(None)
                    method = getattr(cog, method_name)
                    callback = getattr(method, "callback", None)
                    if callback:
                        await callback(cog, interaction, "問" * 200)
                    else:
                        await method(interaction, "問" * 200)
                sender = webhook.send if method_name == "el_psy_kongroo" else interaction.followup.send
                self.assertEqual(sender.await_count, 2)
                self.assertEqual("".join(call.args[0] for call in sender.await_args_list),
                                 "<@2001> 問了：" + "問" * 200 + "\n\n" + "答" * 1900)
                self.assertTrue(all(len(call.args[0].encode("utf-16-le")) // 2 <= 2000 for call in sender.await_args_list))


if __name__ == "__main__":
    unittest.main()
