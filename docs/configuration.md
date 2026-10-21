# 環境變數設定

[`.env.example`](../.env.example) 是完整設定範本。新安裝複製成根目錄 `.env`；既有安裝只補需要的欄位，保留原值。表中的「省略時」是目前程式行為，「範本」是新安裝的起始選擇，兩者不同不代表程式預設已改變。

## 設定來源與格式

- 一行一個 `KEY=value`，相同名稱只寫一次。開關使用 `0` 或 `1`；機率填 `0`～`1` 的數字，不能把說明文字當成值。
- 建議只保留一份根目錄 `.env`。正常從 `bot.py` 啟動時，dotenv 由 bot 模組位置向上尋找，`discord_bot/.env` 會優先於根目錄；既有程序環境不被覆蓋。除錯器／互動執行的搜尋起點可能不同。
- Cookie 診斷工具也是先查程序環境，再找 `discord_bot/.env`、根目錄 `.env`。備份工具則先載入根目錄、再由 `discord_bot/.env` 補缺值，既有環境優先；不要依賴兩份檔案混用。
- 備份工具的 env 解析較簡單。為了共用，避免 `export`、跨行值、行尾註解與變數插值；註解獨立成行。Cookie 整段用單引號包住並保持單行。
- 本機修改後重啟 bot。Docker `env_file` 需重建容器，Restart 不會更新值；依[部署指南](../tools/deploy/README.md#修改環境設定)保留目前 image 與持久資料。

## Discord 與 AI

| 變數 | 省略時 | 範本與用途 |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | 無；bot 不啟動 | 留空待填，啟動 bot 必填 |
| `GOOGLE_GENAI_API_KEY` | 無 | 留空待填；摘要、對話問答及 cloud 角色問答需要 |
| `PORT` | `8080` | `8080`；health server 使用。`app.py` 不自行讀 `.env`，本機須設定程序環境，容器由 `env_file` 注入 |
| `ROLE_MODE` | `local` | `cloud`；只控制 `/解答之書`、`/el_psy_kongroo`，不切換摘要指令的來源 |
| `LOCAL_LLM_URL` | 無 | `http://127.0.0.1:9453`；local 模式的 OpenAI-compatible base URL，程式會加上 `/v1/chat/completions` |

選 `local` 前須自行啟動對應 LLM 服務。容器內的 `127.0.0.1` 指向容器自己，需按實際網路填可達的服務位址。角色 prompt 可由本機 `discord_bot/integrations/system_prompt_role.json` 提供；缺檔或缺角色時使用內建 basic prompt，此檔不在 Git 中。

Gemini 模型順序在 `discord_bot/integrations/gemini_client.py` 的 `SUMMARY_MODEL_NAMES`／`ROLE_MODEL_NAMES` 定義，沒有模型 env 開關。只有服務忙線／不可用才切換備援；429 限流不切模型。

`DISABLE_DISCORD_BOT`、`DISCORD_GUILD_ID` 是舊範例殘留，目前程式不讀取。停止 bot 請停止程序／容器；slash commands 目前全域同步。

## 資料庫與備份

| 變數 | 省略時 | 範本與用途 |
| --- | --- | --- |
| `DB_TYPE` | `sqlite` | `sqlite`；正式部署建議 `postgres` |
| `SQLITE_PATH` | `summaries.db` | `summaries.db`；僅 SQLite 模式使用，相對於工作目錄 |
| `DATABASE_URL` | 無 | 留空；PostgreSQL 模式與備份工具必填，不把真實連線字串填回範本 |
| `LOCAL_BACKUP_SQLITE_PATH` | `postgres_summaries_backup.db` | 備份工具輸出，不是 bot 的主資料庫 |

資料庫包含 `summaries`、`deepfaker_events`、`guild_social_preview_settings`、`social_share_events` 與 `guild_social_duplicate_settings`。SQLite 放在容器內預設路徑不保證跨重建保留；正式環境使用持久 PostgreSQL，或將 SQLite 檔案置於持久 volume。

摘要、DeepFaker 與社群預覽設定 repository 連線失效後會冷卻 30 秒，在下一次實際 DB 需求時重新初始化；正常連線持續重用，不固定輪詢。摘要／DeepFaker 歷史寫入失敗會先 rollback；連線仍可用時讓下一次寫入繼續使用。出警 repository 發生 SQL 錯誤時關閉連線，由出警服務重建快取恢復，見下方說明。失敗／跳過的紀錄不自動補寫，commit 結果不確定時也不立即重送，避免重複資料。DB 故障與恢復通知見下節。

同步工具預設只備份兩個歷史表 `summaries`、`deepfaker_events`，不包含 `social_share_events` 或兩張 guild 設定表，也不能取代 PostgreSQL 全庫備份。命令與模式見[開發與工具指南](development.md#歷史紀錄備份)。

## 社群預覽與出警

| 變數 | 省略時 | 範本與用途 |
| --- | --- | --- |
| `THREADS_PREVIEW_ENABLED` | `0` | `0`；Threads 全域預設 |
| `FACEBOOK_PREVIEW_ENABLED` | `0` | `0`；Facebook 全域預設 |
| `INSTAGRAM_PREVIEW_ENABLED` | `0` | `0`；Instagram 全域預設 |
| `SOCIAL_PREVIEW_DELETE_TIMEOUT_SECONDS` | `0` | `0`；正數為互動刪除按鈕逾時秒數，非正數為同一程序內不自動逾時 |
| `SOCIAL_DUPLICATE_ENABLED` | `1` | `1`；同頻道重複分享出警的全域預設，guild 可覆蓋 |
| `SOCIAL_DUPLICATE_WINDOW_DAYS` | `7` | `7`；啟動載入最近 N 天，正整數，無效值退回七天 |
| `THREADS_COOKIE` | 空白 | Threads 獨立登入備援，可選 |
| `FACEBOOK_COOKIE` | 空白 | Facebook 獨立登入備援，可選 |
| `INSTAGRAM_COOKIE` | 空白 | Instagram 獨立登入備援，可選 |
| `SOCIAL_PREVIEW_COOKIE_AUTO_SAVE` | `1` | `1`；有設定該平台 Cookie 才生效，`0` 停用保存檔讀寫 |
| `SOCIAL_PREVIEW_COOKIE_STORE_DIR` | 專案 `secrets/social-preview-cookies` | 留空沿用預設；容器需指定持久掛載中的目錄 |

Guild 管理者可用 `/社群預覽設定` 即時覆蓋全域預設，包括在 env 關閉時開啟；`/社群預覽狀態` 可查看生效來源。設定存 DB，查詢失敗會明確回報。

`/社群出警設定` 可單獨調整開關、天數或恢復環境預設，需管理伺服器權限；`/社群出警狀態` 查看設定與快取是否就緒。Threads／Facebook／Instagram 只處理已啟用平台且成功送達完整預覽的分享；X／Twitter 直接偵測人類原留言的 status 連結，不依賴預覽開關、不製作預覽、不刪改原留言。兩種模式共用出警設定。同頻道重複時以專用 webhook「不看訊息警察」發送，附上原分享的 Discord 訊息跳轉連結（X 為原留言，其他平台為預覽），只標記本次分享者。固定頭像隨程式打包，無需填入圖片網址；名稱、頭像與台詞沒有 env 設定，目前不附 GIF。無法使用 webhook 時由 bot 原生回覆備援。三平台只到登入牆、未展開 share 或失敗卡不記錄／出警，自己重貼也算重複。

分享歷史永久留在 DB，不保存正文或媒體。啟動時按各 guild 最近 N 天重建記憶體索引；期間只增量記錄，不逐次檢查期限、不排程清理。重啟前即使略超過 N 天仍可出警；長期不重啟時記憶體及判重範圍會增加。改設定會即時重建該 guild 快取。關閉時不記錄新分享，既有 DB 歷史保留；重開不回掃 Discord 歷史。

出警 DB 工作在 worker 執行，連線重用；啟動恢復失敗／寫入故障時暫停出警並每 30 秒重試重建，正常時不輪詢。此恢復流程不依賴容器重啟排程。失聯期間未保存的事件不自動補寫；出警失敗不影響預覽送達與原訊息清理。分享歷史與出警設定不在現有兩表同步工具範圍內，需另做全庫備份。

防雷和前四項混合媒體不另設 env 開關。手動 `||連結||` 遮全部正文與媒體；Threads 原生防雷依取得的文字片段／單一媒體標記顯示。互動刪除按鈕未跨 bot 重啟恢復，`0` 不代表永久有效。Cookie 取得與保存規則見[社群預覽指南](social-preview.md)。

## 通知與 Gmail 維護

| 變數 | 省略時 | 範本與用途 |
| --- | --- | --- |
| `DISCORD_NOTIFY_FORWARD_CHANNEL_ID` | 無；略過轉發 | 留空；填有效數字頻道 ID 才轉發摘要／問答、三平台預覽異常與 DB 故障／恢復，不是轉發所有 log |
| `DISCORD_NOTIFY_FORWARD_GUILD_ID` | 一般通知使用來源 guild；DB 通知由目標頻道決定 | 留空；可選的通知目標 guild ID，DB 通知會檢查頻道是否符合 |
| `GMAIL_NOTIFY_ENABLED` | `1` | `0`；準備好 OAuth 與收件者後才開啟 |
| `GMAIL_CLIENT_ID` | 無 | 留空待填，Gmail 啟用時需要 |
| `GMAIL_CLIENT_SECRET` | 無 | 留空待填，Gmail 啟用時需要 |
| `GMAIL_REFRESH_TOKEN` | 無 | 留空待填，Gmail 啟用時需要 |
| `GMAIL_SEND_TO` | 無；不寄通知 | 留空待填，Gmail 收件者 |
| `GMAIL_REFRESH_TOKEN_ISSUED_AT` | 無；不附日期提醒 | 留空；需要時填含時區的有效 ISO 8601 日期，例如 `2026-10-12T12:00:00+08:00` |
| `GMAIL_CLIENT_SECRET_PATH` | `../client_secret.json` | `client_secret.json`；僅手動 OAuth 維護 helper 使用，路徑相對於工作目錄 |

Gmail 關閉時完全略過 Gmail API，不影響 Discord 通知。日期欄位目前只是以輸入日期加 7 天產生郵件提醒，並非向 Google 查得的實際有效期；不需要提醒就留空，不填 `YYYY-MM-DD` 等占位文字。

DB botlog 通知包含資料表、DB 類型、失敗階段、例外型別／SQLSTATE 與恢復方式，不含 SQL、紀錄內容、連線字串或原始例外訊息。啟動故障等 Discord ready 後送出；首次故障與恢復各通知，同一 repository 持續失敗最多每五分鐘提醒一次，沒有操作時不額外查 DB。通知在有限事件佇列中獨立處理，單次送出最多五秒，失敗不影響使用者指令，也不寄 Gmail。詳細 traceback 留在 Server log；持續故障先檢查 DB 服務、網路或 volume／權限，再決定是否重啟容器。恢復不代表舊紀錄已補回。

`generate_gmail_env_tokens()`／`extract_gmail_env_tokens()` 會輸出或寫回敏感資訊，只能在本機明確需要時手動使用，不屬於啟動或日常診斷流程。

## 世界線與 DeepFaker

| 變數 | 省略時與範本 | 用途 |
| --- | --- | --- |
| `WORLDLINE_PROB_DEFAULT` | `0.01048596` | 一般使用者的世界線事件機率 |
| `WORLDLINE_PROB_ADMIN` | `0.1` | 名單內使用者的世界線事件機率 |
| `WORLDLINE_ADMIN_IDS` | 空白 | 逗號分隔的使用者 ID，只影響機率，不授予管理權限 |
| `DEEPFAKER_FAILURE_PROB` | `0.05` | DeepFaker 失敗機率，填 `0`～`1` |
| `DEEPFAKER_FAILURE_NOTICE` | `抓到你了！炸彈魔！` | 多句台詞用 `\|` 分隔，整段可用單引號包住 |
