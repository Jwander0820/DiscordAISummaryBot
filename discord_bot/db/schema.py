from __future__ import annotations

SUMMARY_COLUMNS = (
    "id",
    "channel_id",
    "user_id",
    "command",
    "question",
    "prompt",
    "summary",
    "call_time",
)

SQLITE_CREATE_SUMMARIES_SQL = """
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    channel_id TEXT,
    user_id TEXT,
    command TEXT,
    question TEXT,
    prompt TEXT,
    summary TEXT,
    call_time TEXT
);
"""

POSTGRES_CREATE_SUMMARIES_SQL = """
CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    channel_id TEXT,
    user_id TEXT,
    command TEXT,
    question TEXT,
    prompt TEXT,
    summary TEXT,
    call_time TIMESTAMPTZ
);
"""

SQLITE_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS guild_social_preview_settings (
    guild_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, platform)
);
"""

POSTGRES_CREATE_GUILD_SOCIAL_PREVIEW_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS guild_social_preview_settings (
    guild_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, platform)
);
"""
