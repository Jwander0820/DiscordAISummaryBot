# DiscordAISummaryBot

Discord AI 總結討論機器人

## 功能總覽

目前主要有 **12 項功能**：

### 使用者功能

1. **24 小時摘要** `聊那麼多誰看的完`
   - 讀取頻道近 24 小時內的非 bot 訊息，預設 5,000 則，產生重點摘要。

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

摘要與對話問答的 `len_msg` 可填 1～20,000；省略時沿用各指令預設值（1,000／5,000／5,000／10,000），實際只取時間範圍內存在的訊息。Threads 每次最多嘗試 6 次匿名頁面請求，另保留分享連結解析、各一次官方 embed / oEmbed 後援。選填 `THREADS_COOKIE` 後，僅在仍無正文或媒體時再嘗試一次登入抓取。

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

# PostgreSQL summaries / deepfaker_events 備份成本地 SQLite 的輸出路徑
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
# 正式部署建議使用 postgres，讓 summaries 與伺服器社群預覽設定能跨版本保留。
# 若使用 sqlite，SQLITE_PATH 必須放在部署平台的 persistent volume。
DB_TYPE=postgres
SQLITE_PATH=summaries.db

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

# Threads/Facebook/Instagram 預覽功能的全域預設
# 設為 0 表示關閉，1 表示啟用；未設定時預設為 0。
# guild 管理者透過 slash command 設定的 override 會存入 DB_TYPE 指定的資料庫。
THREADS_PREVIEW_ENABLED=1
FACEBOOK_PREVIEW_ENABLED=1
INSTAGRAM_PREVIEW_ENABLED=1

# Threads 登入備援（選填；空白表示停用）
# 只填瀏覽器 Request Headers 的 Cookie 值，不含「Cookie:」。
THREADS_COOKIE=
FACEBOOK_COOKIE=
# 登入備援成功後，自動保存網站回傳的 Cookie 更新；預設開啟。
SOCIAL_PREVIEW_COOKIE_AUTO_SAVE=1
# 選填；預設為專案 secrets/social-preview-cookies，容器部署請改為持久 volume。
# SOCIAL_PREVIEW_COOKIE_STORE_DIR=/persistent/social-preview-cookies

# 偽裝功能設定
# DEEPFAKER_FAILURE_NOTICE 使用"|"分隔，隨機選擇台詞
DEEPFAKER_FAILURE_NOTICE='台詞A|台詞B'
DEEPFAKER_FAILURE_PROB=浮點數0~1

# BotLog
DISCORD_NOTIFY_FORWARD_CHANNEL_ID=12345678
DISCORD_NOTIFY_FORWARD_GUILD_ID=12345678

```

## Threads Cookie 備援

預設先匿名抓取；匿名流程仍無有效正文或媒體時，才使用 `THREADS_COOKIE` 嘗試一次。Cookie 只送到 `https://www.threads.com` 的目標貼文頁，不跟隨跳轉，也不會送到圖片／影片 CDN、oEmbed、Facebook 或 Instagram。登入 HTML 不寫入 `_threads_debug`，log 只記錄固定失敗分類、HTTP 狀態與解析計數。

在 Chrome 取得 Cookie：

1. 登入你要提供給 bot 使用的 Threads 帳號，開啟該貼文，先確認瀏覽器裡確實看得到內容。
2. 按 `F12`，切到 **Network（網路）**，勾選 **Disable cache（停用快取）**，重新整理頁面。
3. 用 **Doc** 篩選，點選網址為 `https://www.threads.com/@.../post/...` 的主文件請求。
4. 開啟 **Headers → Request Headers → Cookie**，複製整段 Cookie 的「值」。也可切換 **view source** 後只選取 `cookie:` 後面的內容；不要複製 Response Headers 的 `Set-Cookie` 或整份 HAR。
5. 在 bot 實際使用的 `.env` 加入下列一行，以自己的值取代範例，維持單行；外層單引號包住整個值：

   ```env
   THREADS_COOKIE='cookie_name=value; another_cookie=value'
   ```

6. 重新啟動 bot，再貼一次原本失敗的 Threads 連結；既有 Discord 訊息不會自動重建。

若看不到 Request Headers 的 Cookie，先確認已登入，保持 DevTools 開啟且停用快取後重新整理，再選一次主文件。操作位置可參考 [Chrome DevTools 官方 Network 文件](https://developer.chrome.com/docs/devtools/network/reference#headers)。不要使用 `document.cookie` 當完整來源，它不包含 HttpOnly Cookie。

Cookie 相當於登入憑證，只保留在自己的環境設定與本機受保護的 Cookie 儲存檔；不要貼進聊天、log、截圖或 Git。登入備援不保證每篇都成功：過期、驗證挑戰、限流、需執行 JavaScript 或頁面格式變動仍會失敗。

- 卡片提示「重新登入或驗證」：先在瀏覽器完成登入／驗證，重新複製 Cookie、更新 `.env` 並重啟 bot。401／403 也可能是存取被拒，不能單憑狀態碼斷言 Cookie 已過期。
- 提示「暫時限制請求」：稍後再試，程式不會自動重試或切換帳號。
- 提示「仍無法取得」：沒有找到能對應這則貼文的資料，保留原連結供手動開啟。
- 提示「登入備援仍收到限制觀看頁」：請用提供 Cookie 的同一帳號在瀏覽器確認；這個狀態不會被誤報為 Cookie 過期。平台限制頁不再當成正文，會正常觸發備援。
- 清空或移除 `THREADS_COOKIE` 並重啟，就會恢復全程匿名抓取。

`cookie_no_content` 不等於 Cookie 過期。log 的 `reason=parse_budget_exhausted` 代表解析額度不足，`json_missing` 代表未找到 JSON 資料，`target_not_found` 代表沒有對應目標貼文的節點；`viewer=present` 僅是頁面包含登入狀態線索，不能單獨保證登入有效。

需要單次本機診斷時，可手動執行：

```powershell
.\.venv\Scripts\python.exe tools/diagnose_threads_cookie.py 'https://www.threads.com/@使用者/post/貼文代碼'
```

工具會使用環境或 bot 對應 `.env` 中的 `THREADS_COOKIE`，只請求一次，只輸出狀態、是否有正文與媒體數量等診斷資訊；不輸出 Cookie、正文、媒體網址或 HTML，不保存回應，也不更新設定。

Threads 與 Facebook 的 Cookie 自動保存設定共用，行為見下方「Cookie 自動保存」。診斷工具刻意只使用環境初始值，不讀寫自動保存檔，因此其結果可能與已更新 Cookie 的 bot 不同。

## Facebook 影片與 Cookie 備援

Facebook 公開影片支援第一支可用的 MP4 直連，會從 Open Graph 或目標貼文的 JSON 資料取得，同一影片優先使用 HD。成功時直接放入現有 V2 卡片，不下載／重新上傳影片，也不額外下載封面；沒有可用 MP4 時維持文字、封面與原連結。DASH／HLS manifest、分段網址、已知無聲影像、直播或需 DRM 的來源不會當成完整 MP4。

選填 `FACEBOOK_COOKIE` 可啟用獨立 Facebook 登入備援：匿名流程無可用內容，或影片頁只有封面卻沒有 MP4 時，最多對已解析的目標貼文增加一次請求。若匿名已取得影片，或一般圖片貼文已有內容，就不使用 Cookie。

取得方式與 Threads 相同：在已登入的 Facebook 目標頁開啟 Chrome DevTools → Network → 重新整理 → 選主文件 → Headers → Request Headers → Cookie，複製整段值，放入 bot 實際使用的 `.env`：

```env
FACEBOOK_COOKIE='在這裡貼上複製的整段 Cookie 值'
```

外層單引號保留，整段維持同一行；不要包含 `Cookie:` 字樣，也不要提供 `Set-Cookie`。填妥後重啟 bot，重新貼文測試。Cookie 只送到 `https://www.facebook.com`，不跟隨登入請求的轉址、不送至媒體 CDN、不使用 Threads Cookie，也不會寫進 log、Git 或 HTML 除錯檔。

登入回應只接受與目標貼文綁定、且 privacy 欄位能確認為公開的內容。私人、朋友、自訂或無法確認的範圍不轉發；有匿名封面時即使 Cookie 失敗也保留原本預覽。沒有匿名內容且登入也不可用時，沿用既有失敗卡片與原連結。

需手動診斷時，使用已解析的一般貼文、Reel 或影片網址（不要使用 `/share/...` 短連結）：

```powershell
.\.venv\Scripts\python.exe tools/diagnose_facebook_cookie.py 'https://www.facebook.com/reel/影片代碼/'
```

工具只請求一次，僅輸出狀態、計數與公開狀態；不顯示正文、圖片／影片網址或登入資料，不讀寫自動保存的 Cookie。CDN 直連可能失效，實際播放與聲音仍應在 Discord 驗收。

加上 `--check-config` 可只檢查 Cookie 格式與登入欄位是否存在，不發出網路請求。格式有效不代表登入仍有效；`cookie_request_rejected` 代表 Facebook 回覆 HTTP 400，不能直接視為 Cookie 過期。工具可額外回報固定的錯誤提示分類，不輸出原始錯誤頁。

## Cookie 自動保存

預設開啟，適用於 Threads 與 Facebook；Instagram 維持原本流程。兩個平台仍先匿名抓取，只有需要登入備援時才帶 Cookie，不增加背景或定時請求。

- 首次備援以 `.env` 的 `THREADS_COOKIE` / `FACEBOOK_COOKIE` 建立各自的保存檔；成功解析可轉發的目標貼文後，合併該回應的 `Set-Cookie`。後續備援與重啟沿用更新值，不改寫 `.env`。
- 處理網站指定的有效期、刪除、網域與路徑；登入失敗、限流、限制頁、私人內容或解析失敗不更新 Cookie。網站沒有回傳更新時不會自行產生新 Cookie。
- 手動換成新的環境 Cookie 並重啟，會取代該平台快取；清空該環境變數並重啟即停用登入備援，即使舊保存檔仍在也不會使用。`SOCIAL_PREVIEW_COOKIE_AUTO_SAVE=0` 則停用快取讀寫，回到固定使用環境值。
- 預設目錄為專案內的 `secrets/social-preview-cookies/`，平台檔名分別為 `threads.cookies`、`facebook.cookies`；已由 Git 與 Docker 排除。可用 `SOCIAL_PREVIEW_COOKIE_STORE_DIR` 指定持久目錄；容器必須掛載持久 volume 才能跨重建保留。
- Windows 使用 [DPAPI CurrentUser](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) 加密，須以同一個 Windows 帳號執行 bot。Linux / POSIX 使用 0600 檔案權限、新目錄 0700；檔案內容仍是敏感憑證，不應公開或加入版本控制。
- 檔案無法讀取、解密或寫入時會記錄不含憑證的警告，繼續嘗試環境初始值，不覆蓋無法讀取的檔案。若需重建，先停止 bot，再刪除該平台的 `.cookies` 檔，重啟後由環境初始值建立。每個目錄只供一個 bot 程序寫入。

自動保存只能接收網站已發出的 Cookie 更新，無法自動登入或解除 checkpoint／MFA。長期沒有觸發備援、登入被撤銷，或網站要求驗證時，仍需在瀏覽器完成登入後重新複製 Cookie。

## PostgreSQL 歷史紀錄同步到本地 SQLite

同步工具保留原檔名 `sync_postgres_summaries_to_sqlite.py`，預設會同時拉取 `summaries` 與 `deepfaker_events`：

```bash
python3 tools/sync_postgres_summaries_to_sqlite.py
```

也可以只同步 DeepFaker 紀錄：

```bash
python3 tools/sync_postgres_summaries_to_sqlite.py --table deepfaker_events
```

預設為 incremental，各資料表會分別依本地最大 `id` 拉取新紀錄。`channel_name` 會原樣同步事件發生時保存的頻道名稱快照；穩定識別與敏感頻道過濾請使用 `channel_id`。
