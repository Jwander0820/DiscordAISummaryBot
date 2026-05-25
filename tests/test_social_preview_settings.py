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

    def test_unknown_platform_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = os.path.join(temp_dir, "settings.db")
            with patch.dict(os.environ, {"DB_TYPE": "sqlite", "SQLITE_PATH": sqlite_path}, clear=False):
                service, repository = self._make_service(sqlite_path)

                with self.assertRaises(ValueError):
                    service.resolve_status("123", "instagram")

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


if __name__ == "__main__":
    unittest.main()
