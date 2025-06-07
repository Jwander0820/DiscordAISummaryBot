# DiscordAISummaryBot

Discord AI 總結討論機器人

# 🔧 環境變數設定

請於專案根目錄建立 .env 檔案以設定必要的參數。切勿將實際金鑰上傳到 GitHub 或公開倉庫。

```
# Discord Bot Token，用於連接 Discord API
DISCORD_BOT_TOKEN=your-discord-token

# Google Generative AI (Gemini) API 金鑰
GOOGLE_GENAI_API_KEY=your-gemini-api-key

# 若設為 1 則停用 Discord Bot（Render）
DISABLE_DISCORD_BOT=0

# PostgreSQL 資料庫連線字串（Render 範例格式）
DATABASE_URL=postgresql://user:password@host:port/database

# 本地 LLM 的 API 端點（選用）
LOCAL_LLM_URL=http://127.0.0.1:9453

# 本地 LLM 系統提示的角色（選用）
SYSTEM_PROMPT_ROLE=basic

# Discord GUILD (伺服器) 的 ID，用於限制 BOT 的使用範圍
DISCORD_GUILD_ID=114514

# 儲存的資料庫種類：可為 "sqlite" 或 "postgres"
DB_TYPE=sqlite

```
