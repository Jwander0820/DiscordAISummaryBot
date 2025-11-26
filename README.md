# DiscordAISummaryBot

Discord AI 總結討論機器人

# 🔧 環境變數設定

請於專案根目錄或discord_bot內建立 `.env` 檔案，並填入以下變數，切勿將實際金鑰上傳到公開倉庫：

```env
# Discord Bot Token，用於連接 Discord API
DISCORD_BOT_TOKEN=your-discord-token

# Google Generative AI (Gemini) API 金鑰
GOOGLE_GENAI_API_KEY=your-gemini-api-key

# 停用 Discord Bot（Render 上可設為 1 停用，0 為啟用）
DISABLE_DISCORD_BOT=0

# PostgreSQL 連線字串（Render 範例）
DATABASE_URL=postgresql://user:password@host:port/database

# 本地 LLM 的 API 端點（選用）
LOCAL_LLM_URL=http://127.0.0.1:9453

# 世界線變動率預設值，特殊ID調整機率
WORLDLINE_PROB_DEFAULT=0.01048596
WORLDLINE_PROB_ADMIN=0.1
WORLDLINE_ADMIN_IDS=114514,1919

# Discord GUILD (伺服器) ID，用於限制指令範圍或快速更新指令
DISCORD_GUILD_ID=114514

# 資料庫種類，可設為 "sqlite" 或 "postgres"
DB_TYPE=sqlite

# LLM 調用模式："local" 或 "cloud" (僅在部分指令作用)
ROLE_MODE=local

# Gmail 設定
GMAIL_CLIENT_SECRET_PATH=client_secret.json
GMAIL_CLIENT_ID=gmail_client_id
GMAIL_CLIENT_SECRET=gmail_client_secret
GMAIL_REFRESH_TOKEN=token.pickle_refresh_token
GMAIL_SEND_TO=gmail_send_to@gmail.com
GMAIL_REFRESH_TOKEN_ISSUED_AT='YYYY-MM-DDTHH:MM:SS+08:00'

# Threads Setting THREADS_PREVIEW_ENABLED=0 就會關閉Threads預覽功能
THREADS_PREVIEW_ENABLED=0
THREADS_USE_NO_SANDBOX=1

# 偽裝功能設定
# DEEPFAKER_FAILURE_NOTICE 使用"|"分隔，隨機選擇台詞
DEEPFAKER_FAILURE_NOTICE='台詞A|台詞B' 
DEEPFAKER_FAILURE_PROB=浮點數0~1
```
