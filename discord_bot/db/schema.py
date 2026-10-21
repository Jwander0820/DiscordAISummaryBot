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

SOCIAL_PATROL_EVENT_COLUMNS = (
    "event_id", "guild_id", "guild_name", "channel_id", "channel_name", "user_id",
    "username", "global_name", "display_name", "platform", "post_key", "canonical_url",
    "source_message_id", "source_message_created_at", "current_preview_message_id",
    "first_source_message_id", "first_user_id", "reference_message_id", "share_count",
    "occurred_at", "delivery_status", "delivery_method", "patrol_message_id", "completed_at", "error_type",
    "taunt_text", "taunt_source", "reference_channel_id",
)

CREATE_SOCIAL_PATROL_TABLES_SQL = (
    """CREATE TABLE IF NOT EXISTS social_patrol_events (
        event_id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL, guild_name TEXT, channel_id TEXT NOT NULL, channel_name TEXT,
        user_id TEXT NOT NULL, username TEXT, global_name TEXT, display_name TEXT,
        platform TEXT NOT NULL, post_key TEXT NOT NULL, canonical_url TEXT NOT NULL,
        source_message_id TEXT NOT NULL, source_message_created_at DOUBLE PRECISION,
        current_preview_message_id TEXT NOT NULL, first_source_message_id TEXT NOT NULL,
        first_user_id TEXT NOT NULL, reference_message_id TEXT NOT NULL,
        share_count INTEGER NOT NULL CHECK (share_count >= 2), occurred_at DOUBLE PRECISION NOT NULL,
        delivery_status TEXT NOT NULL CHECK (delivery_status IN ('pending', 'sent', 'failed', 'unknown')),
        delivery_method TEXT CHECK (delivery_method IN ('webhook', 'bot_reply', 'bot_notice')),
        patrol_message_id TEXT, completed_at DOUBLE PRECISION, error_type TEXT,
        taunt_text TEXT, taunt_source TEXT, reference_channel_id TEXT,
        UNIQUE (guild_id, channel_id, platform, source_message_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_social_patrol_guild_time ON social_patrol_events (guild_id, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_social_patrol_user_time ON social_patrol_events (guild_id, user_id, occurred_at);",
)

CREATE_SOCIAL_DUPLICATE_TABLES_SQL = (
    """CREATE TABLE IF NOT EXISTS guild_social_cross_channel_settings (
        guild_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
        channel_ids_json TEXT NOT NULL,
        updated_by TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS guild_social_duplicate_settings (
        guild_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
        window_days INTEGER NOT NULL CHECK (window_days > 0),
        updated_by TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS social_share_events (
        guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, platform TEXT NOT NULL,
        source_message_id TEXT NOT NULL, user_id TEXT NOT NULL,
        post_key TEXT NOT NULL, canonical_url TEXT NOT NULL,
        preview_message_id TEXT NOT NULL, occurred_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (guild_id, channel_id, platform, source_message_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_social_share_events_time ON social_share_events (occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_social_share_events_guild_time ON social_share_events (guild_id, occurred_at);",
) + CREATE_SOCIAL_PATROL_TABLES_SQL

CREATE_DEEPFAKER_EVENT_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_guild_time "
    "ON deepfaker_events (guild_id, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_actor_time "
    "ON deepfaker_events (actor_user_id, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_deepfaker_events_target_time "
    "ON deepfaker_events (target_user_id, occurred_at);",
)
