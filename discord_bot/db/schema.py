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

DEEPFAKER_EVENT_COLUMNS = (
    "guild_id",
    "guild_name",
    "channel_id",
    "channel_name",
    "actor_user_id",
    "actor_username",
    "actor_global_name",
    "actor_display_name",
    "actor_is_bot",
    "target_user_id",
    "target_username",
    "target_global_name",
    "target_display_name",
    "target_is_bot",
    "outcome_success",
    "failure_probability",
    "random_roll",
    "requested_content",
    "webhook_content",
    "failure_notice",
    "failure_exposed_content",
    "delivery_status",
    "occurred_at",
)

SQLITE_CREATE_DEEPFAKER_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS deepfaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    guild_name TEXT,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    actor_user_id TEXT NOT NULL,
    actor_username TEXT,
    actor_global_name TEXT,
    actor_display_name TEXT,
    actor_is_bot INTEGER NOT NULL,
    target_user_id TEXT NOT NULL,
    target_username TEXT,
    target_global_name TEXT,
    target_display_name TEXT,
    target_is_bot INTEGER NOT NULL,
    outcome_success INTEGER NOT NULL,
    failure_probability REAL NOT NULL,
    random_roll REAL NOT NULL,
    requested_content TEXT NOT NULL,
    webhook_content TEXT,
    failure_notice TEXT,
    failure_exposed_content TEXT,
    delivery_status TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""

POSTGRES_CREATE_DEEPFAKER_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS deepfaker_events (
    id BIGSERIAL PRIMARY KEY,
    guild_id TEXT NOT NULL,
    guild_name TEXT,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    actor_user_id TEXT NOT NULL,
    actor_username TEXT,
    actor_global_name TEXT,
    actor_display_name TEXT,
    actor_is_bot BOOLEAN NOT NULL,
    target_user_id TEXT NOT NULL,
    target_username TEXT,
    target_global_name TEXT,
    target_display_name TEXT,
    target_is_bot BOOLEAN NOT NULL,
    outcome_success BOOLEAN NOT NULL,
    failure_probability DOUBLE PRECISION NOT NULL,
    random_roll DOUBLE PRECISION NOT NULL,
    requested_content TEXT NOT NULL,
    webhook_content TEXT,
    failure_notice TEXT,
    failure_exposed_content TEXT,
    delivery_status TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);
"""

CREATE_DEEPFAKER_EVENT_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_guild_time "
    "ON deepfaker_events (guild_id, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_actor_time "
    "ON deepfaker_events (actor_user_id, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_target_time "
    "ON deepfaker_events (target_user_id, occurred_at);",
)
