import json
import unittest

from discord_bot.features.social_preview import instagram_logging as diagnostic


class InstagramLoggingTests(unittest.TestCase):
    def test_only_safe_diagnostic_fields_are_serialized(self):
        with self.assertLogs(diagnostic.logger, level="DEBUG") as logs:
            diagnostic.log_event("fetch_stage", trace_id="validtrace", shortcode="ABC123", stage="cookie",
                                 result="checkpoint_required", http_status=400, elapsed_ms=10,
                                 reason="sessionid=synthetic-secret\nforged-line", cookie="synthetic-secret",
                                 url="https://example.test/private?token=synthetic-secret", text="private-caption")
        message = logs.records[0].getMessage()
        record = json.loads(message.split(" ", 1)[1])
        self.assertEqual(record["reason"], "redacted")
        self.assertEqual(record["http_status"], 400)
        self.assertEqual(record["stage"], "cookie")
        for value in ("synthetic-secret", "private-caption", "forged-line", "https", "url", "text"):
            self.assertNotIn(value, message)

    def test_identifiers_cannot_inject_urls_or_new_lines(self):
        with self.assertLogs(diagnostic.logger, level="DEBUG") as logs:
            diagnostic.log_event("fetch_complete", trace_id="trace\nprivate", shortcode="https://example.test/private")
        record = json.loads(logs.records[0].getMessage().split(" ", 1)[1])
        self.assertEqual(record["trace_id"], "redacted")
        self.assertEqual(record["shortcode"], "redacted")

    def test_normal_diagnostics_are_hidden_at_info(self):
        events = [
            ("fetch_stage", {"stage": "anonymous", "http_status": 200, "result": "success"}),
            ("fetch_stage", {"stage": "anonymous", "http_status": 200, "result": "no_content"}),
            ("fallback", {"stage": "cookie", "reason": "missing_video"}),
            ("fetch_stage", {"stage": "cookie", "http_status": 200, "result": "success"}),
            ("fetch_complete", {"kind": "p", "result": "success", "source": "anonymous", "outcome": "video",
                                "cookie_result": "not_attempted", "kk_result": "not_attempted", "image_count": 7, "video_count": 1}),
            ("fetch_complete", {"kind": "reel", "result": "success", "source": "cookie", "outcome": "video",
                                "cookie_result": "success", "kk_result": "not_attempted"}),
            ("preview_prepared", {"result": "success", "selected_count": 4, "rendered_count": 4,
                                  "dropped_count": 0, "video_count": 0}),
            ("preview_delivery", {"result": "sent", "outcome": "content"}),
        ]
        with self.assertNoLogs(diagnostic.logger, level="INFO"):
            for event, fields in events:
                diagnostic.log_event(event, trace_id="trace", shortcode="ABC123", **fields)
        with self.assertLogs(diagnostic.logger, level="DEBUG") as logs:
            for event, fields in events:
                diagnostic.log_event(event, trace_id="trace", shortcode="ABC123", **fields)
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG"] * len(events))

    def test_abnormal_results_keep_warning_or_error_details(self):
        events = [
            ("fetch_stage", {"stage": "cookie", "http_status": 400, "result": "checkpoint_required"}, "WARNING"),
            ("fetch_stage", {"stage": "anonymous", "http_status": 429, "result": "rate_limited"}, "WARNING"),
            ("fetch_stage", {"stage": "anonymous", "result": "fetch_failed", "reason": "timeout"}, "WARNING"),
            ("fetch_stage", {"stage": "kkinstagram", "http_status": 302, "result": "redirect_rejected"}, "WARNING"),
            ("fetch_complete", {"result": "success", "kk_result": "success", "source": "kkinstagram"}, "WARNING"),
            ("fetch_complete", {"result": "success", "cookie_result": "checkpoint_required", "kk_result": "unavailable"}, "WARNING"),
            ("fetch_complete", {"result": "success", "kind": "reel", "outcome": "images_only"}, "WARNING"),
            ("preview_prepared", {"result": "success", "dropped_count": 1}, "WARNING"),
            ("preview_prepared", {"result": "fetch_failed", "outcome": "error_card"}, "WARNING"),
            ("preview_delivery", {"result": "failed", "http_status": 400, "error_code": 50035}, "ERROR"),
        ]
        with self.assertLogs(diagnostic.logger, level="WARNING") as logs:
            for event, fields, _ in events:
                diagnostic.log_event(event, trace_id="trace", shortcode="ABC123", **fields)
        self.assertEqual([r.levelname for r in logs.records], [level for _, _, level in events])
        self.assertEqual(len(logs.records), len(events))
