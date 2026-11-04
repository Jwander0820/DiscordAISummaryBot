"""Monthly report text: trial-issue layout, names, ordered original URLs, Unicode length and pages."""
import re
import unittest

from discord_bot.features.monthly_report.aggregation import compute_report
from discord_bot.features.monthly_report.render import (
    MESSAGE_LIMIT, NO_RECORD, escape_name, fallback_name, render_pages, utf16_len)
from tests.test_monthly_report_aggregation import LO, MONTH, patrol, share


def sample(names=None):
    rows = [share(user="1", post="A") for _ in range(3)] + [share(user="2", post="B") for _ in range(3)]
    rows += [share(user="1", post="C") for _ in range(2)]
    patrols = [patrol(user="2", first_user="1", post="A", name=names)]
    return compute_report("1", MONTH, rows, patrols)


class RenderTests(unittest.TestCase):
    def test_single_message_follows_trial_layout(self):
        pages = render_pages(sample("範例成員"), {"1": "範例首發"})
        self.assertEqual(len(pages), 1)
        text = pages[0]
        self.assertTrue(text.startswith("**🚨 10 月不看訊息警察月報**\n\n這期記錄到 **2 位成員、8 次分享**，"
                                        "警察共成功出勤 **1 次**。"))
        headings = re.findall(r"^\S+ \*\*(.+?)\*\*$", text, flags=re.M)
        self.assertEqual(headings, ["被出警王", "分享王", "零撞車分享榜", "跨區撞車王", "首發帶動王",
                                    "光速撞車", "最常被分享的貼文", "警察最忙日"])
        self.assertIn("🚓 **被出警王**\n**範例成員 — 1 次**\n本期最常與警察見面的人。", text)
        self.assertIn("📣 **分享王**\n**範例首發 — 5 次分享**", text)
        self.assertIn("🌱 **首發帶動王**\n**範例首發 — 引發別人 1 次重貼警報**", text)
        self.assertIn("📅 **警察最忙日**\n**10 月 1 日 — 一天出勤 1 次**\n本期超過一半的警報", text)
        self.assertIn("🧭 **跨區撞車王**\n" + NO_RECORD, text)
        self.assertIn("下次分享前，記得往上滑一下。", text)
        self.assertTrue(text.endswith("分享數為結算時留存的登記。*"))
        self.assertIn("*統計區間 2026/10/01 00:00 – 2026/11/01 00:00（台北時間）。", text)
        self.assertNotIn("頁）", text)

    def test_ties_share_one_line_with_each(self):
        rows = [share(user=user) for user in ("1", "1", "2", "2")]
        text = render_pages(compute_report("1", MONTH, rows, [
            patrol(user="1", reference="11", name="甲"), patrol(user="2", reference="11", name="乙")]), {})[0]
        self.assertIn("**甲、乙 — 各 2 次分享**", text)
        self.assertIn("**甲、乙 — 各 1 次跨頻道警報**", text)
        self.assertIn("**甲、乙 — 各 1 次**", text)

    def test_popular_posts_are_ordered_clickable_original_urls_with_ties(self):
        lines = [line for line in render_pages(sample(), {})[0].splitlines() if re.match(r"\d+\. ", line)]
        self.assertEqual(lines, [
            "1. <https://example.com/posts/A> · **3 次分享**（並列第一）",
            "2. <https://example.com/posts/B> · **3 次分享**（並列第一）",
            "3. <https://example.com/posts/C> · **2 次分享**",
        ])

    def test_zero_collision_line_format(self):
        rows = [share(user="9", post=f"Z{index}") for index in range(63)]
        text = render_pages(compute_report("1", MONTH, rows, []), {"9": "範例成員"})[0]
        self.assertIn("🏅 **零撞車分享榜**\n範例成員 · **63 筆**", text)

    def test_names_snapshot_then_lookup_then_fallback(self):
        text = render_pages(sample("快照名稱"), {"1": "查詢名稱", "2": "不該使用"})[0]
        self.assertIn("**快照名稱 — 1 次**", text)
        self.assertIn("**查詢名稱 — 5 次分享**", text)
        self.assertIn(fallback_name("1"), render_pages(sample(), {})[0])
        self.assertEqual(fallback_name("1234567890"), "未知成員（ID 末四碼 7890）")
        self.assertIn(fallback_name("2"), render_pages(sample("   "), {})[0])

    def test_names_are_escaped_and_cannot_mention_or_format(self):
        self.assertEqual(escape_name("**粗體** <@123> @everyone"), r"\*\*粗體\*\* \<\@123\> \@everyone")
        self.assertEqual(escape_name("# 標題\n- 清單"), r"\# 標題 \- 清單")
        self.assertEqual(escape_name("a‮b‏\x00c"), "abc")
        self.assertEqual(len(escape_name("長" * 80)), 32)
        self.assertIn(r"**\-\# 假字幕 — 1 次**", render_pages(sample("-# 假字幕"), {})[0])

    def test_footer_notes_data_coverage(self):
        text = render_pages(compute_report("1", MONTH, [share(at=LO + 86400 * 19)], []), {})[0]
        self.assertIn("（台北時間），資料自 10/20 起。", text)
        self.assertNotIn("測試貼文", text)

    def test_fastest_wording(self):
        first = share(user="1", message=500, at=LO + 100)
        report = compute_report("1", MONTH, [first], [
            patrol(user="2", first_user="1", first_message=500, at=LO + 100 + 938, name="快手")])
        text = render_pages(report, {})[0]
        self.assertIn("⚡ **光速撞車**\n**快手 — 約 15 分 38 秒**\n前一位分享後，不到 16 分鐘就再次登場！", text)

    def test_long_reports_drop_flavor_before_paginating(self):
        report = sample()
        full = render_pages(report, {})[0]
        trimmed = render_pages(report, {}, limit=utf16_len(full) - 5)
        self.assertEqual(len(trimmed), 1)
        for flavor in ("情報補給擔當", "下次分享前"):
            self.assertNotIn(flavor, trimmed[0])
        self.assertIn("-# 分享次數包含首次。", trimmed[0])
        self.assertIn("*統計區間", trimmed[0])

    def test_pages_keep_every_tied_member_and_respect_utf16_limit(self):
        rows = [share(user=str(1000 + index)) for index in range(150)]
        names = {str(1000 + index): "😀測試成員名稱" + "𠀀" * 20 for index in range(150)}
        pages = render_pages(compute_report("1", MONTH, rows, []), names)
        self.assertGreater(len(pages), 1)
        for index, page in enumerate(pages, start=1):
            self.assertLessEqual(utf16_len(page), MESSAGE_LIMIT)
            self.assertTrue(page.endswith(f"-# （第 {index}/{len(pages)} 頁）"))
        joined = "\n".join(pages)
        self.assertEqual(joined.count(" — 1 次分享**"), 150)
        self.assertIn("📣 **分享王**（續）", joined)


if __name__ == "__main__":
    unittest.main()
