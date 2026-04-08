# DiscordAISummaryBot

Discord AI 總結討論機器人

## 功能總覽

目前主要有 **12 項功能**：

### 使用者功能

1. **24 小時摘要** `聊那麼多誰看的完`
   - 讀取頻道近 24 小時內最多 2000 則非 bot 訊息，產生重點摘要。

2. **1 小時快速摘要** `整理廢話的魔法`
   - 讀取近 1 小時訊息，適合快速回顧剛剛聊了什麼。

3. **7 天深度摘要** `命運探知之魔眼`
   - 讀取近 7 天內的大量訊息，整理較長週期的討論重點。

4. **對話問答** `你要不要聽聽看你現在在講什麼`
   - 根據最近對話內容回答使用者提問，適合查詢「剛剛誰說了什麼」。

5. **世界線測試** `測試d-mail`
   - 測試 bot 狀態並模擬世界線變動率事件。

6. **LLM 問答** `解答之書`
   - 取樣最近訊息後，交給本地或雲端 LLM 回答問題。

7. **角色風格問答** `el_psy_kongroo`
   - 以命運石之門風格回應問題，並使用 webhook 角色化發送。

8. **DeepFaker 偽裝發言** `deepfaker`
   - 透過 webhook 偽裝成指定成員發送訊息，並帶有隨機失敗機制。

9. **Threads 連結預覽**
   - 自動解析 Threads 貼文，顯示文字、圖片、影片直連與原連結按鈕。

10. **Facebook 連結預覽**
    - 自動擷取 Facebook 貼文 Open Graph 資訊，建立預覽卡片與媒體內容。

### 系統支援功能

11. **通知與紀錄**
    - 將摘要與問答結果寫入 SQLite / PostgreSQL，並可轉發通知到 Discord 頻道，也可選擇啟用 Gmail 通知。

12. **部署與維運支援**
    - 提供 Flask health check、Docker 啟動配置，以及 PostgreSQL 同步到本地 SQLite 的備份工具。

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

# PostgreSQL summaries 備份成本地 SQLite 的輸出路徑
LOCAL_BACKUP_SQLITE_PATH=postgres_summaries_backup.db

# 本地 LLM 的 API 端點（選用）
LOCAL_LLM_URL=http://127.0.0.1:9453

# 世界線變動率預設值，特殊ID調整機率
WORLDLINE_PROB_DEFAULT=0.01048596
WORLDLINE_PROB_ADMIN=0.1
WORLDLINE_ADMIN_IDS=114514,1919

# Discord GUILD (伺服器) ID，用於限制指令範圍或快速更新指令
DISCORD_GUILD_ID=114514

# 通知轉發設定（將 log 同步轉發到 Discord 指定頻道）
# 必填：目標頻道 ID
DISCORD_NOTIFY_FORWARD_CHANNEL_ID=123456789012345678
# 選填：若要固定轉發到特定伺服器，設定目標伺服器 ID
DISCORD_NOTIFY_FORWARD_GUILD_ID=114514

# 資料庫種類，可設為 "sqlite" 或 "postgres"
DB_TYPE=sqlite

# LLM 調用模式："local" 或 "cloud" (僅在部分指令作用)
ROLE_MODE=local

# Gmail 設定
GMAIL_NOTIFY_ENABLED=1
GMAIL_CLIENT_SECRET_PATH=client_secret.json
GMAIL_CLIENT_ID=gmail_client_id
GMAIL_CLIENT_SECRET=gmail_client_secret
GMAIL_REFRESH_TOKEN=token.pickle_refresh_token
GMAIL_SEND_TO=gmail_send_to@gmail.com
GMAIL_REFRESH_TOKEN_ISSUED_AT='YYYY-MM-DDTHH:MM:SS+08:00'

# Threads/Facebook 預覽功能開關
# 設為 0 表示關閉，1 表示啟用（預設 1）
THREADS_PREVIEW_ENABLED=1
FACEBOOK_PREVIEW_ENABLED=1
THREADS_USE_NO_SANDBOX=1

# 偽裝功能設定
# DEEPFAKER_FAILURE_NOTICE 使用"|"分隔，隨機選擇台詞
DEEPFAKER_FAILURE_NOTICE='台詞A|台詞B' 
DEEPFAKER_FAILURE_PROB=浮點數0~1

# BotLog
DISCORD_NOTIFY_FORWARD_CHANNEL_ID=12345678
DISCORD_NOTIFY_FORWARD_GUILD_ID=12345678

```
