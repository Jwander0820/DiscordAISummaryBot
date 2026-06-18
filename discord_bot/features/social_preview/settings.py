from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ...db.social_preview_settings_repository import (
    SocialPreviewSettingsRepository,
    social_preview_settings_repository,
)

PLATFORM_THREADS = "threads"
PLATFORM_FACEBOOK = "facebook"
PLATFORM_INSTAGRAM = "instagram"
SUPPORTED_PLATFORMS = (PLATFORM_THREADS, PLATFORM_FACEBOOK, PLATFORM_INSTAGRAM)

_PLATFORM_ENV_VARS = {
    PLATFORM_THREADS: "THREADS_PREVIEW_ENABLED",
    PLATFORM_FACEBOOK: "FACEBOOK_PREVIEW_ENABLED",
    PLATFORM_INSTAGRAM: "INSTAGRAM_PREVIEW_ENABLED",
}


@dataclass(frozen=True)
class SocialPreviewSettingStatus:
    platform: str
    global_enabled: bool
    guild_override: Optional[bool]
    effective_enabled: bool
    source: str


def validate_platform(platform: str) -> str:
    normalized = (platform or "").strip().lower()
    if normalized not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported social preview platform: {platform}")
    return normalized


def env_platform_enabled(platform: str) -> bool:
    normalized = validate_platform(platform)
    raw = os.getenv(_PLATFORM_ENV_VARS[normalized], "0").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


class SocialPreviewSettingsService:
    """Resolve effective Social Preview settings for messages and admin commands."""

    def __init__(self, repository: SocialPreviewSettingsRepository = social_preview_settings_repository) -> None:
        self.repository = repository

    def resolve_status(self, guild_id: Optional[str], platform: str) -> SocialPreviewSettingStatus:
        normalized = validate_platform(platform)
        global_enabled = env_platform_enabled(normalized)
        guild_override = None

        if guild_id is not None:
            guild_override = self.repository.get_setting(str(guild_id), normalized)

        if guild_override is not None:
            return SocialPreviewSettingStatus(
                platform=normalized,
                global_enabled=global_enabled,
                guild_override=guild_override,
                effective_enabled=guild_override,
                source="guild_override",
            )

        return SocialPreviewSettingStatus(
            platform=normalized,
            global_enabled=global_enabled,
            guild_override=None,
            effective_enabled=global_enabled,
            source="global_default",
        )

    def is_enabled(self, guild_id: Optional[str], platform: str) -> bool:
        return self.resolve_status(guild_id, platform).effective_enabled

    def set_override(
        self,
        guild_id: str,
        platform: str,
        enabled: bool,
        *,
        updated_by: Optional[str] = None,
    ) -> bool:
        normalized = validate_platform(platform)
        return self.repository.set_setting(str(guild_id), normalized, enabled, updated_by=updated_by)

    def clear_override(self, guild_id: str, platform: str) -> bool:
        normalized = validate_platform(platform)
        return self.repository.clear_setting(str(guild_id), normalized)

    def list_statuses(self, guild_id: Optional[str]) -> dict[str, SocialPreviewSettingStatus]:
        return {platform: self.resolve_status(guild_id, platform) for platform in SUPPORTED_PLATFORMS}

    def settings_available(self) -> bool:
        return self.repository.is_available()


social_preview_settings_service = SocialPreviewSettingsService()


def is_social_preview_enabled(guild_id: Optional[str], platform: str) -> bool:
    return social_preview_settings_service.is_enabled(guild_id, platform)
