import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.features.social_preview import cookie_store as store


URL = "https://www.facebook.com/demo/posts/example"
SEED = "c_user=synthetic-user; xs=synthetic-initial"


class SocialCookieStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"SOCIAL_PREVIEW_COOKIE_STORE_DIR": self.directory.name,
                                     "SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": "1", "FACEBOOK_COOKIE": SEED})
        env.start()
        self.addCleanup(env.stop)

    def prepare(self, seed=SEED, url=URL, platform="facebook", **kwargs):
        return store.prepare_cookie(platform, url, seed, **kwargs)

    def test_saved_rotation_survives_new_snapshot_with_unchanged_environment_seed(self):
        initial = self.prepare()
        self.assertEqual(store.save_response_cookies(initial, ["xs=synthetic-rotated; Domain=.facebook.com; Path=/; Secure; HttpOnly"]), "saved")
        restarted = self.prepare()
        self.assertIn("xs=synthetic-rotated", restarted.header)
        self.assertNotIn("synthetic-initial", restarted.header)
        self.assertIn("c_user=synthetic-user", restarted.header)
        self.assertNotIn("synthetic", repr(restarted))
        self.assertEqual(os.environ["FACEBOOK_COOKIE"], SEED)

    def test_manual_seed_change_replaces_saved_generation(self):
        old = self.prepare()
        newer = self.prepare("xs=manual-new-login")
        self.assertEqual(newer.header, "xs=manual-new-login")
        self.assertEqual(store.save_response_cookies(old, ["xs=late-old-response; Path=/"]), "stale_response")
        self.assertEqual(self.prepare("xs=manual-new-login").header, "xs=manual-new-login")

    def test_empty_seed_disables_existing_store_without_read_or_write(self):
        self.prepare()
        with patch.object(store, "_read", side_effect=AssertionError("must not read")):
            self.assertEqual(self.prepare("").header, "")

    def test_feature_switch_and_diagnostic_opt_out_never_access_store(self):
        for flag in ("0", "false", "off", "no"):
            with self.subTest(flag=flag), patch.dict(os.environ, {"SOCIAL_PREVIEW_COOKIE_AUTO_SAVE": flag}), patch.object(store, "_read") as read:
                snapshot = self.prepare()
                self.assertEqual(snapshot.header, SEED)
                self.assertEqual(store.save_response_cookies(snapshot, ["xs=new"]), "disabled")
                read.assert_not_called()
        with patch.object(store, "_read") as read:
            self.prepare(persist=False)
            read.assert_not_called()

    def test_platforms_are_isolated_and_foreign_domains_are_ignored(self):
        facebook = self.prepare()
        threads = self.prepare("sessionid=threads-only", "https://www.threads.com/@demo/post/abc", "threads")
        instagram = self.prepare("sessionid=ig-only", "https://www.instagram.com/p/ABC123", "instagram")
        self.assertEqual(len({facebook.path, threads.path, instagram.path}), 3)
        self.assertEqual(store.save_response_cookies(instagram, ["sessionid=wrong; Domain=.threads.com; Path=/"]), "unchanged")
        self.assertEqual(store.save_response_cookies(instagram, ["sessionid=ig-new; Domain=.instagram.com; Path=/"]), "saved")
        self.assertEqual(self.prepare("sessionid=ig-only", "https://www.instagram.com/p/ABC123", "instagram").header, "sessionid=ig-new")
        self.assertNotEqual(facebook.path, threads.path)
        values = ["xs=wrong; Domain=.threads.com; Path=/", "xs=wrong; Domain=.com; Path=/", "xs=wrong; Domain=evil.facebook.com; Path=/"]
        self.assertEqual(store.save_response_cookies(facebook, values), "unchanged")
        self.assertEqual(self.prepare().header, SEED)
        self.assertEqual(store.save_response_cookies(threads, ["sessionid=new-thread; Domain=.threads.com; Path=/"]), "saved")

    def test_cookie_deletion_does_not_resurrect_original_environment_value(self):
        snapshot = self.prepare("xs=initial")
        self.assertEqual(store.save_response_cookies(snapshot, ["xs=deleted; Path=/; Max-Age=0"]), "saved")
        self.assertEqual(self.prepare("xs=initial").header, "")

    def test_max_age_expires_and_precedence(self):
        with patch.object(store.time, "time", return_value=1000):
            snapshot = self.prepare("xs=old")
            store.save_response_cookies(snapshot, ["xs=rotated; Path=/; Max-Age=10; Expires=Thu, 01 Jan 1970 00:00:01 GMT"])
            self.assertEqual(self.prepare("xs=old").header, "xs=rotated")
        with patch.object(store.time, "time", return_value=1011):
            self.assertEqual(self.prepare("xs=old").header, "")
        snapshot = self.prepare()
        store.save_response_cookies(snapshot, ["xs=deleted; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT"])
        self.assertNotIn("xs=", self.prepare().header)

    def test_scoped_and_default_paths_use_cookie_path_boundary(self):
        snapshot = self.prepare()
        store.save_response_cookies(snapshot, ["scoped=one; Path=/demo", "default=two", "exact=three; Path=/demo/posts/example"])
        same = self.prepare().header
        self.assertIn("scoped=one", same)
        self.assertIn("default=two", same)
        self.assertTrue(same.startswith("exact=three"))
        different = self.prepare(url="https://www.facebook.com/demonstration/post").header
        self.assertNotIn("scoped=", different)
        self.assertNotIn("default=", different)

    def test_prefix_and_header_injection_guards(self):
        snapshot = self.prepare()
        headers = ["__Host-bad=a; Path=/; Domain=.facebook.com; Secure", "__Secure-bad=b; Path=/",
                   "bad=value\r\nInjected: nope", "__Host-good=ok; Secure; Path=/"]
        store.save_response_cookies(snapshot, headers)
        result = self.prepare().header
        self.assertIn("__Host-good=ok", result)
        self.assertNotIn("bad", result)
        self.assertNotIn("Injected", result)

    def test_late_parallel_response_cannot_overwrite_first_completed_update(self):
        first, second = self.prepare(), self.prepare()
        store.save_response_cookies(second, ["xs=newest; Path=/"])
        self.assertEqual(store.save_response_cookies(first, ["xs=stale; Path=/"]), "stale_response")
        self.assertIn("xs=newest", self.prepare().header)

    def test_atomic_replace_failure_preserves_file_and_cleans_temporary_file(self):
        snapshot = self.prepare()
        previous = snapshot.path.read_bytes()
        with patch.object(store.os, "replace", side_effect=OSError("synthetic-secret")), self.assertLogs("discord_digest_bot") as logs:
            self.assertEqual(store.save_response_cookies(snapshot, ["xs=next; Path=/"]), "save_failed")
        self.assertEqual(snapshot.path.read_bytes(), previous)
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)
        self.assertNotIn("synthetic-secret", repr(logs.output))

    def test_unreadable_existing_cache_is_not_overwritten_or_logged(self):
        snapshot = self.prepare()
        snapshot.path.write_bytes(b"corrupted-synthetic-secret")
        with self.assertLogs("discord_digest_bot") as logs:
            fallback = self.prepare()
        self.assertEqual(fallback.header, SEED)
        self.assertIsNone(fallback.state)
        self.assertEqual(snapshot.path.read_bytes(), b"corrupted-synthetic-secret")
        self.assertNotIn("corrupted-synthetic-secret", repr(logs.output))

    def test_seed_write_failure_still_allows_configured_fallback(self):
        with patch.object(store, "_write", side_effect=OSError("credential-bearing-error")), self.assertLogs("discord_digest_bot") as logs:
            fallback = self.prepare()
        self.assertEqual(fallback.header, SEED)
        self.assertIsNone(fallback.state)
        self.assertNotIn("credential-bearing-error", repr(logs.output))

    def test_cookie_count_and_header_limits_reject_update_without_truncation(self):
        snapshot = self.prepare()
        with self.assertLogs("discord_digest_bot"):
            self.assertEqual(store.save_response_cookies(snapshot, [f"k{i}=a; Path=/" for i in range(128)]), "save_failed")
        self.assertEqual(self.prepare().header, SEED)
        with self.assertLogs("discord_digest_bot"):
            self.assertEqual(store.save_response_cookies(snapshot, ["long=" + "a" * 16350 + "; Path=/"]), "save_failed")
        self.assertEqual(self.prepare().header, SEED)

    def test_untrusted_target_is_rejected_before_filesystem_access(self):
        for url in ("http://www.facebook.com/post", "https://www.facebook.com.evil/post", "https://cdn.facebook.com/post"):
            with self.subTest(url=url), patch.object(store, "_read") as read, self.assertRaises(ValueError):
                self.prepare(url=url)
            read.assert_not_called()

    def test_repeated_response_headers_remain_separate_including_expires_commas(self):
        from email.message import Message
        headers = Message()
        headers.add_header("Set-Cookie", "a=1; Expires=Wed, 21 Oct 2037 07:28:00 GMT")
        headers.add_header("Set-Cookie", "b=2")
        # requests urllib3 and aiohttp multidict use these two names.
        class Raw:
            def getlist(self, name):
                return headers.get_all(name)
        class Multi:
            def getall(self, name, default):
                return headers.get_all(name, default)
        self.assertEqual(len(store.response_cookies(Raw())), 2)
        self.assertEqual(store.response_cookies(Raw()), store.response_cookies(Multi()))

    def test_disk_protection_and_round_trip_use_only_synthetic_credentials(self):
        snapshot = self.prepare()
        data = snapshot.path.read_bytes()
        if os.name == "nt":
            self.assertTrue(data.startswith(b"DPAPI1\n"))
            self.assertNotIn(b"synthetic", data)
        else:
            self.assertEqual(snapshot.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.prepare().header, SEED)
