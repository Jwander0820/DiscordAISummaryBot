from __future__ import annotations

from .logging_utils import configure_logging
from ..db.repository import summary_repository
from ..db.social_preview_settings_repository import social_preview_settings_repository


def bootstrap_application() -> None:
    """Run non-Discord startup tasks that should happen before the bot connects."""
    configure_logging()
    summary_repository.init()
    social_preview_settings_repository.init()
