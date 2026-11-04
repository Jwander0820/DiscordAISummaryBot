"""Monthly settlement rules over fictional rows; no Discord, DB or network."""
import unittest

from discord_bot.features.monthly_report.aggregation import (
    PatrolRow, ShareRow, compute_report)
from discord_bot.features.monthly_report.periods import month_bounds

MONTH = "2026-10"
LO, HI = month_bounds(MONTH)
_counter = iter(range(10_000, 1_000_000))


def share(user="1", post="P1", *, at=LO + 3600, channel="10", platform="threads", message=None):
    return ShareRow(channel, platform, str(message or next(_counter)), user, post,
                    f"https://example.com/posts/{post}", at)


def patrol(user="2", first_user="1", *, post="P1", at=LO + 7200, status="sent", channel="10",
           reference=None, first_message="0", platform="threads", name=None, event=None):
    return PatrolRow(str(event or next(_counter)), channel, user, None, None, name, platform, post,
                     str(first_message), first_user, reference, at, status)


class SettlementTests(unittest.TestCase):
    def test_boundaries_are_left_closed_right_open(self):
        rows = [share(at=LO), share(at=HI - 0.5), share(at=LO - 0.5), share(at=HI)]
        patrols = [patrol(at=LO), patrol(at=HI)]
        report = compute_report("1", MONTH, rows, patrols)
        self.assertEqual(report.share_rows, 2)
        self.assertEqual(report.patrol_rows, 1)
        self.assertEqual((report.period_start, report.period_end), (LO, HI))

    def test_empty_month_is_empty_and_awards_have_no_records(self):
        report = compute_report("1", MONTH, [], [])
        self.assertTrue(report.is_empty)
        self.assertEqual(report.top_sharers, [])
        self.assertEqual(report.popular_posts, [])
        self.assertFalse(compute_report("1", MONTH, [], [patrol(status="failed")]).is_empty)

    def test_deleted_share_still_counts_successful_patrol(self):
        report = compute_report("1", MONTH, [], [patrol(user="5")])
        self.assertEqual(report.patrol_sent, 1)
        self.assertEqual([(item.user_id, item.value) for item in report.most_patrolled], [("5", 1)])
        self.assertEqual(report.fastest, [])

    def test_award_ties_are_kept_and_only_sent_patrols_count(self):
        rows = [share(user="1"), share(user="1"), share(user="2"), share(user="2"), share(user="3")]
        patrols = [patrol(user="7"), patrol(user="8"), patrol(user="9", status="failed"),
                   patrol(user="9", status="unknown"), patrol(user="9", status="pending")]
        report = compute_report("1", MONTH, rows, patrols)
        self.assertEqual([(item.user_id, item.value) for item in report.top_sharers], [("1", 2), ("2", 2)])
        self.assertEqual([item.user_id for item in report.most_patrolled], ["7", "8"])
        self.assertEqual(report.patrol_sent, 2)
        self.assertEqual(report.status_counts, {"failed": 1, "pending": 1, "sent": 2, "unknown": 1})

    def test_cross_channel_requires_known_different_reference(self):
        patrols = [patrol(user="2", reference="11"), patrol(user="2", reference="11"),
                   patrol(user="3", reference="10"), patrol(user="4", reference=None),
                   patrol(user="5", reference="12", status="failed")]
        report = compute_report("1", MONTH, [], patrols)
        self.assertEqual(report.cross_channel_sent, 2)
        self.assertEqual([(item.user_id, item.value) for item in report.cross_channel], [("2", 2)])

    def test_first_movers_exclude_self_repeats(self):
        patrols = [patrol(user="2", first_user="1"), patrol(user="3", first_user="1"),
                   patrol(user="4", first_user="4"), patrol(user="4", first_user="4"),
                   patrol(user="5", first_user="6", status="failed")]
        report = compute_report("1", MONTH, [], patrols)
        self.assertEqual([(item.user_id, item.value) for item in report.first_movers], [("1", 2)])

    def test_fastest_patrol_excludes_self_and_missing_or_negative_sources(self):
        first = share(user="1", message=500, at=LO + 1000)
        cross_first = share(user="1", message=600, at=LO + 2000, channel="11")
        own = share(user="2", message=700, at=LO + 5000)
        patrols = [
            patrol(user="1", first_user="1", first_message=500, at=LO + 1001),   # self repeat
            patrol(user="2", first_user="1", first_message=999, at=LO + 1002),   # source missing
            patrol(user="3", first_user="1", first_message=500, at=LO + 1900),   # 900 s
            patrol(user="4", first_user="1", first_message=600, at=LO + 2300, channel="10", reference="11"),
            patrol(user="5", first_user="2", first_message=700, at=LO + 4000),   # negative gap
            patrol(user="6", first_user="1", first_message=500, at=LO + 1100, status="unknown"),
        ]
        report = compute_report("1", MONTH, [first, cross_first, own], patrols)
        self.assertEqual([(item.user_id, item.seconds) for item in report.fastest], [("4", 300)])

    def test_fastest_source_must_be_in_the_same_month(self):
        before = share(user="1", message=500, at=LO - 60)
        report = compute_report("1", MONTH, [before], [patrol(user="3", first_message=500, at=LO + 10)])
        self.assertEqual(report.fastest, [])

    def test_popular_posts_count_across_channels_with_competition_ranks(self):
        rows = []
        for post, count in (("A", 3), ("B", 3), ("C", 2), ("D", 2), ("E", 1)):
            rows += [share(post=post, channel=str(10 + index)) for index in range(count)]
        report = compute_report("1", MONTH, rows, [])
        self.assertEqual([(item.rank, item.post_key, item.shares, item.tied) for item in report.popular_posts],
                         [(1, "A", 3, True), (1, "B", 3, True), (3, "C", 2, True), (3, "D", 2, True)])
        self.assertEqual(report.popular_posts[0].canonical_url, "https://example.com/posts/A")

    def test_popular_posts_stop_after_third_place_and_sort_ties_stably(self):
        rows = []
        for post, count in (("Z", 5), ("Y", 4), ("X", 3), ("W", 3), ("V", 2)):
            rows += [share(post=post) for _ in range(count)]
        report = compute_report("1", MONTH, rows, [])
        self.assertEqual([(item.rank, item.post_key) for item in report.popular_posts],
                         [(1, "Z"), (2, "Y"), (3, "W"), (3, "X")])
        self.assertFalse(report.popular_posts[0].tied)

    def test_same_key_on_different_platforms_is_a_different_post(self):
        rows = [share(post="K", platform="threads"), share(post="K", platform="x")]
        self.assertEqual(compute_report("1", MONTH, rows, []).popular_posts, [])

    def test_popular_posts_cap_includes_at_most_five_even_when_third_place_ties(self):
        rows = []
        for post in reversed("ABCDEFG"):
            rows.extend(share(post=post) for _ in range(3 if post in "AB" else 2))
        report = compute_report("1", MONTH, rows, [])
        self.assertEqual([(item.rank, item.post_key, item.tied) for item in report.popular_posts],
                         [(1, "A", True), (1, "B", True), (3, "C", True), (3, "D", True), (3, "E", True)])

    def test_busiest_day_uses_taipei_date(self):
        late_utc = LO + 86400 * 5 + 3600 * 23  # 10/06 23:00 Taipei
        patrols = [patrol(at=late_utc), patrol(at=late_utc + 2 * 3600), patrol(at=late_utc + 3 * 3600),
                   patrol(at=LO + 86400 * 2), patrol(at=LO + 86400 * 2 + 1, status="failed")]
        report = compute_report("1", MONTH, [], patrols)
        self.assertEqual([(str(day.day), day.patrols) for day in report.busiest_days], [("2026-10-07", 2)])

    def test_snapshot_names_prefer_latest_event_and_display_name(self):
        patrols = [patrol(user="2", name="舊暱稱", at=LO + 10), patrol(user="2", name="新暱稱", at=LO + 20),
                   PatrolRow("e", "10", "3", "user3", "全域名稱", None, "x", "P", "0", "1", None, LO + 5, "sent")]
        report = compute_report("1", MONTH, [], patrols)
        self.assertEqual(report.snapshot_names, {"2": "新暱稱", "3": "全域名稱"})


class ZeroCollisionTests(unittest.TestCase):
    def shares(self, user, count, **kwargs):
        return [share(user=user, post=f"{user}-{index}", **kwargs) for index in range(count)]

    def test_fifty_does_not_qualify_and_fifty_one_does(self):
        report = compute_report("1", MONTH, self.shares("50", 50) + self.shares("51", 51), [])
        self.assertEqual([(item.user_id, item.value) for item in report.zero_collision], [("51", 51)])

    def test_any_detection_this_month_disqualifies_even_if_not_delivered(self):
        rows = self.shares("8", 60) + self.shares("9", 70) + self.shares("7", 52)
        patrols = [patrol(user="9", status="failed"), patrol(user="7", status="unknown")]
        report = compute_report("1", MONTH, rows, patrols)
        self.assertEqual([item.user_id for item in report.zero_collision], ["8"])

    def test_previous_month_detection_does_not_disqualify_and_list_is_sorted(self):
        rows = self.shares("8", 60) + self.shares("9", 70) + self.shares("6", 60)
        report = compute_report("1", MONTH, rows, [patrol(user="9", at=LO - 10)])
        self.assertEqual([(item.user_id, item.value) for item in report.zero_collision],
                         [("9", 70), ("6", 60), ("8", 60)])


if __name__ == "__main__":
    unittest.main()
