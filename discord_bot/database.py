import os
import sqlite3
import psycopg2
import logging

logger = logging.getLogger('discord_digest_bot')

DB_TYPE = os.getenv('DB_TYPE', 'sqlite').lower()
DB_ENABLED = True
placeholder = '?'  # default for sqlite
conn = None
cursor = None


def init_db():
    global conn, cursor, placeholder, DB_ENABLED, DB_TYPE

    if DB_TYPE not in ('sqlite', 'postgres'):
        logger.error(f"不支援的 DB_TYPE: {DB_TYPE}，預設改用 sqlite")
        DB_TYPE = 'sqlite'

    if DB_TYPE == 'postgres':
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            logger.error("使用 PostgreSQL 時，必須設定 DATABASE_URL，停用 DB 寫入")
            DB_ENABLED = False
            return
        try:
            conn = psycopg2.connect(database_url, connect_timeout=5)
            cursor = conn.cursor()
            cursor.execute(
                """
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
            )
            conn.commit()
            placeholder = "%s"
            logger.info("✅ PostgreSQL 連線及建表成功")
        except Exception as e:
            logger.error(f"❌ PostgreSQL 初始化失敗，已停用 DB 寫入: {e}", exc_info=True)
            DB_ENABLED = False
    else:
        sqlite_path = os.getenv('SQLITE_PATH', 'summaries.db')
        try:
            conn = sqlite3.connect(sqlite_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT,
                    user_id TEXT,
                    command TEXT,
                    question TEXT,
                    prompt TEXT,
                    summary TEXT,
                    call_time TEXT
                );
                """
            )
            conn.commit()
            placeholder = "?"
            logger.info(f"✅ SQLite 連線及建表成功 ({sqlite_path})")
        except Exception as e:
            logger.error(f"❌ SQLite 初始化失敗，已停用 DB 寫入: {e}", exc_info=True)
            DB_ENABLED = False


def insert_summary(record: dict):
    if not DB_ENABLED:
        logger.warning("insert_summary: DB_DISABLED，跳過寫入")
        return

    cols = list(record.keys())
    vals = list(record.values())
    phs = ",".join([placeholder] * len(cols))
    sql = f"INSERT INTO summaries ({','.join(cols)}) VALUES ({phs});"

    try:
        cursor.execute(sql, vals)
        conn.commit()
        logger.info("✅ summaries 寫入成功")
    except Exception as e:
        logger.error(f"❌ summaries 寫入失敗: {e}", exc_info=True)
