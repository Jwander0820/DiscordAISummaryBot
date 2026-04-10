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
