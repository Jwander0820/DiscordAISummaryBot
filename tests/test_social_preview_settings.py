import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from discord_bot.db.social_preview_settings_repository import SocialPreviewSettingsRepository
from discord_bot.features.social_preview.settings import SocialPreviewSettingsService


class SocialPreviewSettingsTests(unittest.TestCase):
    def _make_service(self, sqlite_path):
        repository = SocialPreviewSettingsRepository()
        return SocialPreviewSettingsService(repository=repository), repository

    def test_missing_env_default_without_override_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path},
                clear=True,
            ):
                service, repository = self._make_service(sqlite_path)

                status = service.resolve_status("123", "threads")

                self.assertFalse(status.effective_enabled)
                self.assertEqual(status.source, "global_default")
                repository.close()

    def test_env_default_enabled_without_override_is_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "THREADS_PREVIEW_ENABLED": "1"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)

                status = service.resolve_status("123", "threads")

                self.assertTrue(status.effective_enabled)
                self.assertEqual(status.source, "global_default")
                repository.close()

    def test_guild_enabled_override_can_enable_when_global_default_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "THREADS_PREVIEW_ENABLED": "0"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)
                repository.set_setting("123", "threads", True, updated_by="42")

                status = service.resolve_status("123", "threads")

                self.assertTrue(status.effective_enabled)
                self.assertTrue(status.guild_override)
                self.assertEqual(status.source, "guild_override")
                repository.close()

    def test_guild_disabled_override_disables_when_global_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "FACEBOOK_PREVIEW_ENABLED": "1"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)
                repository.set_setting("123", "facebook", False, updated_by="42")

                status = service.resolve_status("123", "facebook")

                self.assertFalse(status.effective_enabled)
                self.assertFalse(status.guild_override)
                self.assertEqual(status.source, "guild_override")
                repository.close()

    def test_clear_override_falls_back_to_env_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "FACEBOOK_PREVIEW_ENABLED": "1"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)
                repository.set_setting("123", "facebook", False, updated_by="42")
                repository.clear_setting("123", "facebook")

                status = service.resolve_status("123", "facebook")

                self.assertTrue(status.effective_enabled)
                self.assertIsNone(status.guild_override)
                self.assertEqual(status.source, "global_default")
                repository.close()

    def test_instagram_env_default_enabled_without_override_is_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "INSTAGRAM_PREVIEW_ENABLED": "1"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)

                status = service.resolve_status("123", "instagram")

                self.assertTrue(status.effective_enabled)
                self.assertEqual(status.source, "global_default")
                repository.close()

    def test_instagram_guild_override_can_enable_when_global_default_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(
                os.environ,
                {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path, "INSTAGRAM_PREVIEW_ENABLED": "0"},
                clear=False,
            ):
                service, repository = self._make_service(sqlite_path)
                repository.set_setting("123", "instagram", True, updated_by="42")

                status = service.resolve_status("123", "instagram")

                self.assertTrue(status.effective_enabled)
                self.assertTrue(status.guild_override)
                self.assertEqual(status.source, "guild_override")
                repository.close()

    def test_unknown_platform_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                service, repository = self._make_service(sqlite_path)

                with self.assertRaises(ValueError):
                    service.resolve_status("123", "bluesky")

                repository.close()

    def test_repository_persists_sqlite_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = SocialPreviewSettingsRepository()
                repository.set_setting("123", "threads", False, updated_by="42")
                repository.close()

                conn = sqlite3.connect(sqlite_path)
                try:
                    row = conn.execute(
                        "SELECT guild_id, platform, enabled, updated_by FROM guild_social_preview_settings"
                    ).fetchone()
                finally:
                    conn.close()

                self.assertEqual(row, ("123", "threads", 0, "42"))

    def test_repository_retries_after_transient_initialization_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            real_connect = sqlite3.connect
            connect_attempts = 0

            def flaky_connect(path):
                nonlocal connect_attempts
                connect_attempts += 1
                if connect_attempts == 1:
                    raise sqlite3.OperationalError("temporary failure")
                return real_connect(path)

            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                repository = SocialPreviewSettingsRepository(retry_interval_seconds=0)
                with patch("discord_bot.db.social_preview_settings_repository.sqlite3.connect", side_effect=flaky_connect):
                    self.assertFalse(repository.init())
                    self.assertTrue(repository.set_setting("123", "threads", True, updated_by="42"))

                self.assertTrue(repository.get_setting("123", "threads"))
                self.assertEqual(connect_attempts, 2)
                repository.close()


if __name__ == "__main__":
    unittest.main()
