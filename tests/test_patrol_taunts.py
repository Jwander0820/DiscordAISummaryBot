import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from discord_bot.features.social_preview import patrol_taunts as taunts

GOOD = "這篇都快被你分享成每日簽到任務了。別人簽到領獎勵，你簽到領警報，連警車都記得停哪裡了。要不要先往上滑一下，讓大家知道你除了分享，也有解鎖閱讀功能？"


class OutputTests(unittest.TestCase):
    def test_all_fallbacks_are_in_range_and_retain_daily_checkin_joke(self):
        self.assertGreaterEqual(len(taunts.FALLBACK_TAUNTS), 4)
        for text in taunts.FALLBACK_TAUNTS:
            self.assertEqual(taunts._valid_output(text), text)
        self.assertTrue(any("這篇都快被你分享成每日簽到任務了。" in t for t in taunts.FALLBACK_TAUNTS))

    def test_rejects_wrong_length_mentions_links_or_explanatory_wrappers(self):
        self.assertEqual(taunts._valid_output("字" * 20), "字" * 20)
        self.assertEqual(taunts._valid_output("字" * 100), "字" * 100)
        for value in (None, "太短", "字" * 19, "a" * 60, "字" * 101, GOOD + "\n補充", GOOD + "@everyone", GOOD + "<@123>",
                      GOOD + " https://example.com", "以下是吐槽：" + GOOD, '"' + GOOD + '"', "**" + GOOD + "**"):
            with self.subTest(value=value):
                self.assertIsNone(taunts._valid_output(value))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_counts_never_call_gemini(self):
        with patch.object(taunts, "_role_model") as factory:
            self.assertEqual(await taunts.choose_taunt(2), taunts.PatrolTaunt("", "none"))
            self.assertEqual((await taunts.choose_taunt(3)).source, "fixed")
            self.assertEqual((await taunts.choose_taunt(5)).text, "第 5 次了！警察都認得這篇了，你還不認得！")
            factory.assert_not_called()

    async def test_prompt_contains_only_count_and_bounded_display_name(self):
        model = SimpleNamespace(generate_content_async=AsyncMock(return_value=SimpleNamespace(text=GOOD)))
        with patch.object(taunts, "_role_model", return_value=model):
            result = await taunts.choose_taunt(8, "範例暱稱\n" + "字" * 60)
        self.assertEqual(result, taunts.PatrolTaunt("第 8 次了！" + GOOD, "gemini"))
        args = model.generate_content_async.await_args.kwargs
        data = json.loads(args["contents"])
        self.assertEqual(set(data), {"share_count", "display_name"})
        self.assertEqual(data["share_count"], 8)
        self.assertLessEqual(len(data["display_name"]), 32)
        config = args["generation_config"]
        self.assertEqual(config["max_output_tokens"], 512)
        self.assertIn("20～100", config["system_instruction"])
        self.assertIn("只輸出", config["system_instruction"])

    async def test_missing_model_invalid_response_and_api_failure_pick_fallback(self):
        cases = [None, SimpleNamespace(generate_content_async=AsyncMock(return_value=SimpleNamespace(text="太短"))),
                 SimpleNamespace(generate_content_async=AsyncMock(side_effect=RuntimeError("private payload")))]
        for model in cases:
            with self.subTest(model=model), patch.object(taunts, "_role_model", return_value=model), \
                    patch.object(taunts.random, "choice", return_value=GOOD) as choose:
                self.assertEqual(await taunts.choose_taunt(6), taunts.PatrolTaunt("第 6 次了！" + GOOD, "fallback"))
                choose.assert_called_once_with(taunts.FALLBACK_TAUNTS)

    async def test_timeout_cancels_generation_and_uses_fallback(self):
        cancelled = asyncio.Event()
        async def slow(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        model = SimpleNamespace(generate_content_async=slow)
        with patch.object(taunts, "_role_model", return_value=model), \
                patch.object(taunts, "GENERATION_TIMEOUT_SECONDS", 0.01):
            result = await taunts.choose_taunt(7)
        self.assertEqual(result.source, "fallback")
        self.assertTrue(cancelled.is_set())

    async def test_external_cancellation_does_not_generate_fallback(self):
        with patch.object(taunts, "_generate", AsyncMock(side_effect=asyncio.CancelledError)), \
                patch.object(taunts.random, "choice") as choose:
            with self.assertRaises(asyncio.CancelledError):
                await taunts.choose_taunt(7)
            choose.assert_not_called()

    async def test_queue_wait_shares_same_deadline_and_never_calls_model(self):
        locked = asyncio.Semaphore(0)
        with patch.object(taunts, "_generation_slots", locked), \
                patch.object(taunts, "GENERATION_TIMEOUT_SECONDS", 0.01), \
                patch.object(taunts, "_role_model") as factory:
            self.assertEqual((await taunts.choose_taunt(8)).source, "fallback")
            factory.assert_not_called()
