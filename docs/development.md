# 開發與工具

依賴版本以 [requirements.txt](../requirements.txt) 為準，Docker 使用 Python 3.11。先依[專案 README](../README.md)建立環境；程式從根目錄啟動，讓 SQLite、log 與相對路徑一致。

## Discord 設定

Bot 使用 Message Content Intent，應用程式也須開啟；邀請時包含 `bot` 與 `applications.commands`。依功能配置檢視頻道、讀取訊息歷史、傳送訊息、嵌入連結與附加檔案權限。Webhook 代發需管理 Webhook；清理使用者原始連結需管理訊息。缺少 Webhook 權限時社群預覽可由 bot 備援發送。

`/社群預覽設定`、`/社群出警設定` 要求管理伺服器權限。出警開關／天數與快取恢復見[社群預覽指南](social-preview.md#同頻道重複分享出警)，`/社群出警狀態` 可確認是否就緒。不要把 `WORLDLINE_ADMIN_IDS` 當成授權名單，它只影響世界線事件機率。

## 指令與訊息範圍

| 指令 | 範圍 | 預設訊息數 |
| --- | --- | --- |
| `聊那麼多誰看的完` | 近 24 小時 | 5,000 |
| `整理廢話的魔法` | 近 1 小時 | 5,000 |
| `命運探知之魔眼` | 近 7 天 | 10,000 |
| `你要不要聽聽看你現在在講什麼` | 近 24 小時對話 | 1,000 |
| `解答之書`、`el_psy_kongroo` | 最近對話 | 固定最多 20 |

前四個指令的 `len_msg` 接受 1～20,000；跳過 bot 訊息，實際數量依時間範圍與可取得內容決定。共同 history helper 的額外讀取也受 20,000 上限約束。`ROLE_MODE` 只控制最後兩個指令。

三個問答指令保留原有答案截斷方式，再將包含使用者 mention、問題與答案的完整回覆分成每頁最多 2,000 UTF-16 units；後續頁不重複通知發問者。一般 followup 與 Amadeus webhook 共用相同長度界線。

## 架構

| 位置 | 責任 |
| --- | --- |
| `bot.py`、`discord_bot/bot.py` | 啟動、載入 cogs、首次連線後同步 slash commands |
| `discord_bot/core/` | logging 與摘要／DeepFaker／預覽設定 repository 初始化；出警由 cog 載入時重建快取 |
| `discord_bot/cogs/` | Discord 指令／事件入口 |
| `discord_bot/features/chat/`、`summaries/` | 歷史訊息、prompt、摘要與紀錄協調 |
| `discord_bot/features/notifications/`、`deepfaker/` | 通知派送及 DeepFaker 事件組裝 |
| `discord_bot/features/social_preview/` | 各平台抓取、Cookie、共用 Gallery／V2 view、發送與重複分享判定（含 X） |
| `discord_bot/integrations/` | google-genai、local LLM、Gmail |
| `discord_bot/db/` | SQLite／PostgreSQL 的 summaries、DeepFaker、社群分享歷史與 guild 設定 |
| `app.py` | 獨立 Flask `/`、`/health`；不啟動 Discord bot |

`python bot.py` 只啟動 Discord bot。若本機另需 health server，可在另一個終端執行 `python app.py`；它不自行載入 `.env`，改 port 時先設定程序環境，例如 PowerShell 的 `$env:PORT='8081'`。Docker 入口會啟動兩者並使用注入的環境。`/health` 正常不代表 Discord Gateway 或 LLM 已通過功能驗證。

DB 層的 `health.py` 只收集去識別化故障／恢復事件；`features/notifications/database.py` 的單一 consumer 由 bot lifecycle 管理，等待 ready 後發送 botlog，關閉 bot 時取消。這條路徑不輪詢 DB、不呼叫 Gmail，也不把通知寫回 DB。

`social_duplicate_cog.py` 獨立接收 X／Twitter 與 YouTube 原留言並管理出警設定；三平台則由既有預覽流程在完整送達後登記分享。YouTube 影片 ID／網址正規化放在 `youtube_links.py`，不抓取頁面。判重與持久資料放在 `duplicate_service.py`／`social_duplicate_repository.py`，關閉出警不會停用三平台預覽。

Instagram 的現行抓取入口是 `fetch_instagram_post()` → `instagram_direct.py`，必要時才走 Cookie／KK 備援。舊 `urllib` 抓取、原生 embed 與猜測 `/media/` URL 的備援入口已移除；共用資料模型與仍使用的解析 helper 保留。

## 一次性社群分享歷史回補

目前沒有內建 Discord 歷史回掃指令。手動回補 `social_share_events` 時，沿用現有事件格式與 URL 正規化規則；每則來源訊息、每個平台只記第一個有效連結。保留真實 guild／channel／user ID、來源訊息 ID 與原分享時間（UTC epoch 秒），不可使用匯入時間或預覽 webhook 的 ID 代替原分享者。

X 原留言仍存在時，`platform` 使用 `x`、`post_key` 使用 status ID，`source_message_id` 與 `preview_message_id` 都填原留言 ID。排除 bot／webhook 訊息，避免把其他 bot 的預覽當成另一次分享；已刪除的原留言若無可靠作者與來源證據，跳過而不猜測。三平台原留言被取代後，僅憑預覽顯示名稱不足以還原分享者。

社群活動 log 可作為找回網址的線索，不能單獨當成完整事件來源：一般 INFO 不含各項 ID，且在抓取前寫入，不證明預覽成功。需和 Discord 預覽或其他可靠來源交叉核對；Facebook `/share/...` 還須解析成最終文章識別。無法可靠還原必要欄位的資料不混入正式事件表。Log 保留條件見[部署指南](../tools/deploy/README.md#log-保留與容器更新)。

先核對目標資料庫、時間與頻道範圍，再以 `ON CONFLICT (guild_id, channel_id, platform, source_message_id) DO NOTHING` 跳過已存在事件，核對實際新增筆數。直接寫 DB 不會發出歷史出警，也不會立即更新運作中的記憶體索引；完成後重啟 bot 重新載入，且僅載入各 guild 設定的最近 N 天。匯入資料與 SQL 含私人識別資訊，保存在專案外，不提交 Git。

## 測試

Windows PowerShell 在專案根目錄執行：

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Linux／CI 使用 `python -m unittest discover -s tests -t .`。CI 在建置的 image 裡禁用網路、掛載 tests 與 tools 執行，成功後才發布。測試多用 fake 資料與 mock；真實 Discord SDK 測試會視載入狀態在子程序隔離執行。

Parser 或預覽變更時先跑對應回歸，例如：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_threads_fetch tests.test_threads_spoilers tests.test_threads_video_view tests.test_media_gallery
```

出警及 X 的 focused tests：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_social_duplicate_service tests.test_social_duplicate_patrol tests.test_social_duplicate_webhook tests.test_x_duplicate_links tests.test_x_duplicate_patrol
.\.venv\Scripts\python.exe -m unittest tests.test_cross_channel_patrol tests.test_cross_channel_patrol_view
.\.venv\Scripts\python.exe -m unittest tests.test_youtube_duplicate_links tests.test_youtube_duplicate_patrol
```

部署腳本測試需要 Bash，透過 fake Docker 執行，不操作實際 Docker daemon。Windows 若誤選 WSL Bash，改用已安裝的 Git for Windows Bash；以此工作區的安裝路徑為例，僅在本次終端調整：

```powershell
$env:PATH='C:\Software\Git\bin;C:\Software\Git\usr\bin;' + $env:PATH
```

其他安裝位置需替換路徑。以最終 `OK`／`FAILED` 判讀測試；DB unavailable 測試刻意產生 traceback，缺少 Discord／aiohttp 可能造成 skip。不要把 skip 當成已驗證。離線測試不等於實際社群抓取或 Discord 播放驗收。

## 獨立 GIF／卡片測試

`tools/send_preview_test.py` 只用 Discord REST API，不匯入 `discord_bot`、不啟動
`bot.py`／Gateway，不載入 cogs、DB 或通知。可與已運作的 bot 分開執行。

| `--layout` | 外觀 | GIF 與版面 |
| --- | --- | --- |
| `thumbnail`（預設） | 傳統 Embed 右側小縮圖 | 最接近「文字左、圖片右」的緊湊卡片 |
| `image` | 傳統 Embed 下方大圖 | 支援 GIF，通常較佔高度 |
| `v2-thumbnail` | Container 裡的 Section + Thumbnail | 小 GIF 與文字並排，可擴充元件配置 |
| `v2-gallery` | Container 裡的 Media Gallery | 適合媒體展示，通常較佔版面 |
| `all` | 各送一則，共四則 | 方便在同一頻道比較 |
| `v2-section` | 無 Container 外框的 Section + Thumbnail | 文字與小 GIF 並排，沒有色條 |
| `author-icon` | Embed 作者列小圖示 | 更小，動畫表現需在客戶端確認 |
| `footer-icon` | Embed 頁尾小圖示 | 更小，動畫表現需在客戶端確認 |
| `v2-spoiler` | V2 並排縮圖加防雷 | 點擊揭露 GIF，文字仍可見 |
| `extras` | 上述額外四種各送一則 | 不重複 `all` 的基本四種 |

傳統 Embed 的 `author.icon_url`／`footer.icon_url` 是小圖示欄位，不適合當
主要 GIF 展示區；其動畫表現不作本工具的驗收保證。直接貼 GIF 或分享網址也可能
產生大張自動預覽，但排版由 Discord 決定。`rich` 是可自行組裝的 embed；
`gifv`／`video` 等自動預覽種類不能靠填 `type` 當作自訂卡片模板。

GIF 應提供可直接回傳媒體的 HTTPS URL，例如 `https://media.example.com/demo.gif`
（虛構範例，需替換），不要填 `https://giphy.com` 首頁或一般分享頁；
不是所有 GIF 網址都以 `.gif` 結尾，仍以實際回傳媒體為準。也可直接使用本機 GIF。
Discord 決定縮圖的顯示尺寸，設定 embed 的 `width`／`height` 無法強制縮小；
V2 Thumbnail 也沒有像素尺寸欄位。動畫仍受客戶端自動播放／輔助使用設定影響。
V2 訊息不混用傳統 `content`／`embeds`，文字必須用 Text Display。

在根目錄 PowerShell 執行（GIF 路徑／直連請自行替換）：

```powershell
$env:PYTHONUTF8='1'
# 只看四種 JSON，不讀 .env、不連線、不發送
.\.venv\Scripts\python.exe tools/send_preview_test.py --gif-file 'C:\Pictures\demo.gif' --layout all

# 實際送一則小縮圖，讀取已設定的通知頻道
.\.venv\Scripts\python.exe tools/send_preview_test.py --gif-file 'C:\Pictures\demo.gif' --layout thumbnail --send

# 實際送四則比較；可改用自己的媒體直連
.\.venv\Scripts\python.exe tools/send_preview_test.py --gif-url 'https://media.example.com/demo.gif' --layout all --send

# 自訂文字與連結（PowerShell 換行用 `n）
.\.venv\Scripts\python.exe tools/send_preview_test.py --gif-file 'C:\Pictures\demo.gif' --layout v2-thumbnail --title '有新人' --text "如題`n歡迎加入" --link 'https://example.com' --send
```

工具只使用 `DISCORD_BOT_TOKEN`、`DISCORD_NOTIFY_FORWARD_CHANNEL_ID`、
`DISCORD_NOTIFY_FORWARD_GUILD_ID`。各欄位依序從程序環境、根目錄 `.env`、
`discord_bot/.env` 取值；明確空字串視為停用／設定錯誤，不向後備援。
發送前取得頻道並核對 guild，兩個 ID 都必填。沒有硬編碼私人 ID 或 token，
不印設定、原始 API 錯誤或 HTTP traceback；dry run 會印出你提供的內文和 GIF URL，
請勿將包含私密內容或簽名網址的輸出分享出去。

需要 bot 的檢視頻道、傳送訊息、嵌入連結權限；本機 GIF 另需附加檔案權限。
本機檔案以固定名稱 `preview.gif` 上傳（工具上限 10 MiB，Discord 仍可能有更低限制）。
所有測試訊息禁用 mentions。成功會印訊息連結；遇錯立即停止，四則可能只送出部分。
不自動重送 POST，網路逾時後應先查看頻道避免重複發送。測試訊息可自行在 Discord 刪除。

使用 bot token 發送的名稱／頭像是現有 bot 身分。範例 JSON 的最外層 `username`／
`avatar_url` 是 webhook 發送選項，本工具不建立 webhook，也不修改 bot 身分；
`author.name` 則只是卡片內的作者欄位。

參考：[Discord Embed 與 Create Message](https://docs.discord.com/developers/resources/message)、
[Components V2／Thumbnail／Media Gallery](https://docs.discord.com/developers/components/reference)、
[Webhook 發送欄位](https://docs.discord.com/developers/resources/webhook)。
離線測試驗證 payload、設定優先序與發送邊界，實際動畫與版面需在 Discord 驗收。

### Webhook 警報版型獨立示範

`tools/send_patrol_preview_test.py` 可模擬「不看訊息警察」，各發一則 V2 有框並排縮圖與
傳統 Embed 縮圖。僅從通知設定取得頻道／guild，核對後重用或建立 bot 擁有的
`digest-patrol-layout-test` 測試 Webhook，不修改正式警報或使用其 Webhook。
範例與原始連結均標示模擬，不讀寫社群分享歷史或 DB。

```powershell
.\.venv\Scripts\python.exe tools/send_patrol_preview_test.py --gif-url 'https://media.example.com/demo.gif' --avatar-gif --send
```

省略 `--send` 只離線預覽。`--avatar-gif` 另外將 GIF URL 填到每則的 `avatar_url`，
測試頭像；API 接受不等於客戶端會播放動畫。卡片 GIF 與頭像是不同欄位。
`--layout v2 --no-heading` 可只送 V2 且移除卡片內重複標題，外層 Webhook 名稱仍保留。
`--layout author-icon` 可只送 Embed 作者列小 GIF 版，不附右側縮圖。
使用 `--avatar-url 'https://media.example.com/avatar.png'` 指定固定 PNG 頭像，
與卡片 GIF 分開；此選項和歷史測試用的 `--avatar-gif` 互斥。
`--channel-id` 僅覆寫此次發送目標，不修改 `.env` 檔案內容；仍核對設定的 guild。
跨伺服器測試須另帶 `--guild-id` 明確覆寫此次核對的 guild；同樣不寫入 `.env`。
卡片內的模擬原始連結預設為 `https://example.com`，不使用頻道資訊；
只有明確帶入 `--reference-url` 才改用指定網址。
主要測試目的地可放在 `tools/.patrol-preview.local.json`（Git ignore）中，使用
`channel_id` 與 `guild_id` 字串欄位。優先順序為 CLI、本機 JSON、既有通知環境設定；
本機 JSON 不含憑證，不納入 Git，也不修改 `.env`。
`--final-style --layout v2` 送出無測試註記的最終外觀，仍不觸發判重或寫 DB。
`--share-counts 2 3 4 5 6 --layout v2` 可各送一則指定次數的模擬警報，
沿用目前正式文案，並在測試註記標示次數；不改正式計數或 DB。
預設只允許標記 bot 自己，供驗證 mention 解析；`--mention-user` 可指定使用者 ID，
指定真人時禁用通知。
V2 Text Display 可以解析與通知 mentions，正式使用時應只允許明確對象；
傳統 Embed 中的 mention 與通知行為不等同 V2 文字元件。
Webhook V2 使用 `wait=true&with_components=true` 與 `flags=32768`，
不用 `SUPPRESS_EMBEDS` 隱藏卡片。送達回應只輸出去識別化布林檢查與訊息連結。

## 歷史紀錄備份

完整 PostgreSQL 單一資料庫備份使用 `tools/backup_postgres.py`，不轉成 SQLite、不限定資料表。它從既有 `.env` 載入 `DATABASE_URL`，不輸出連線資訊，也不將連線字串放進子程序命令列。需要安裝 `pg_dump` 與 `pg_restore`（建議與來源 PostgreSQL 相同 major 版本；pg_dump 不支援較新的來源 server）。

```powershell
.\.venv\Scripts\python.exe tools/backup_postgres.py
# 用戶端不在 PATH 時，以下安裝路徑僅為範例：
.\.venv\Scripts\python.exe tools/backup_postgres.py --pg-bin "C:\Program Files\PostgreSQL\18\bin"
```

預設輸出 `backups/postgres_<台北時間>.dump`，可用 `--output` 指定新檔；不覆蓋既有備份。Custom archive 包含可匯出的 schema、所有資料表資料、索引、約束、sequence、大型物件與 ownership/ACL，未來新增資料表也會納入。不包含 cluster 共用的角色／tablespace 定義、其他 DB、伺服器設定或 WAL；角色等全域物件需另用 `pg_dumpall --globals-only` 管理。本工具不自動執行此操作。

完成條件為 pg_dump 成功且無診斷、archive 非空、pg_restore 清單與完整解碼成功，之後才發布檔案並輸出 SHA256。失敗清理暫存；不顯示可能含主機資訊的原始錯誤。此驗證不代表實際還原成功。輸出與暫存屬敏感資料，已由 Git ignore 排除；儲存目錄請限制本機存取。

需還原時，先準備**獨立、空的 PostgreSQL 資料庫**與必要 extensions，再手動執行；以下僅為範例，工具不會執行還原：

```powershell
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --dbname local_restore_test backups/postgres_example.dump
```

此範例使用本機既有 libpq 認證設定，略過來源 ownership/ACL 以方便搬移；若要完整重現權限，需先建立對應角色再保留 ownership/ACL 還原。Extension 執行檔等外部依賴仍需另裝。勿指向正式 DB；本工具不提供自動覆寫或刪庫功能。官方說明：[pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html)。

`tools/sync_postgres_summaries_to_sqlite.py` 預設將 PostgreSQL **所有使用者資料表與欄位**轉存 SQLite，包含 guild 設定、社群分享、出警事件及未來新增的表。需要 `DATABASE_URL`，輸出由 `--output`、`LOCAL_BACKUP_SQLITE_PATH` 決定；都沒指定時使用 `postgres_backup_<台北時間>.db`。預設位置已存在時另加時間戳，不改舊檔；明確指定 `--output` 的既有檔則拒絕覆蓋。這個工具會連線資料庫；可先用 dry run 確認範圍：

```powershell
.\.venv\Scripts\python.exe tools/sync_postgres_summaries_to_sqlite.py --dry-run
.\.venv\Scripts\python.exe tools/sync_postgres_summaries_to_sqlite.py
```

| 選項 | 行為 |
| --- | --- |
| `--table all` | 預設，全使用者資料表的全量新快照，不依賴 id 或主鍵 |
| `--table history` | 原本兩張歷史表，保留增量同步模式 |
| `--table summaries`／`--table deepfaker_events` | 只同步指定表 |
| `--mode incremental` | 僅 history／單張歷史表適用且為其預設，各表按本機最大 id 取得新增資料；all 禁用 |
| `--mode upsert` | history／單表以 id 更新或加入；all 一律匯出全量新快照 |
| `--mode replace` | history／單表重建本機選定表，使用前另存舊檔；all 一律匯出全量新快照 |
| `--limit`、`--output` | 限制每表讀取數量、指定輸出檔 |

全表模式以單一唯讀 REPEATABLE READ transaction 取得一致資料，逐批寫入暫存 SQLite，完整性檢查成功後才發布；任一表失敗即不發布。權限不足或 RLS 會明確失敗，不靜默省略。`public` 保留原表名，其餘以 `schema.table` 命名；SQLite 大小寫不敏感的名稱碰撞會失敗。分割表透過根表讀取，不重複匯出子表。Foreign table 也會讀取，遠端來源的一致性仍受該來源限制。

若 DBeaver 需要 SSH 才能存取 DB，這個工具也可自動建立 Tunnel，不需開放 PostgreSQL 公網埠。以下為虛構 alias 與私鑰路徑，換成自己的設定：

```powershell
.\.venv\Scripts\python.exe tools/sync_postgres_summaries_to_sqlite.py --ssh-host example-db --ssh-key "$HOME\.ssh\example-db.pem"
```

`--ssh-host` 可用既有 SSH config alias；`--ssh-user`、`--ssh-port` 可另指定，預設沿用 config。`--ssh-db-host`／`--ssh-db-port` 是從 SSH 主機可連到的 DB，預設 `127.0.0.1:5432`；Docker DB 必須已發布在遠端 loopback，若只在 Docker network，請填 SSH 主機實際可達的位址。也可手動設定上述 `BACKUP_SSH_*` 到本機 `.env`，之後原指令直接執行即可；真實設定不寫回文件或範本。

私鑰由 OpenSSH 的 `-i` 使用，Python 不讀取／複製／輸出內容；無需把鑰匙放進專案。SSH 子程序不接收 DATABASE_URL。只綁本機 loopback，備份成功或失敗都會關閉 Tunnel；`DATABASE_URL` 原檔不變，TLS 模式與原 hostname 保留，只覆寫 libpq hostaddr／port。預設 `StrictHostKeyChecking=yes`，不自動接受未知主機；新主機需先由你核對指紋並建立 known_hosts。加密私鑰請先載入本機 ssh-agent；BatchMode 不會在背景提示密碼。原生 pg_dump 工具目前不使用此 Tunnel 選項。

JSON／陣列保存為 JSON text；Decimal、UUID、日期時間及超出 SQLite 64-bit 的整數保存為 text；bytea 保存 blob，NULL 保留。這是資料匯出，**不保存 PostgreSQL 原生型別定義、索引、約束、預設值、sequence 狀態、角色、view／materialized view、函式與大型物件**；完整原生備份請使用上面的 pg_dump 工具。`--limit` 是部分資料匯出，不可當成完整備份。

DeepFaker 的穩定識別用 `channel_id`；`channel_name` 是事件發生時的名稱快照。完整事件統計使用 `deepfaker_events`，不由相容用的 `summaries` 推算。

Cookie 單次診斷工具見[社群預覽指南](social-preview.md)；實際 `.env`、Cookie 保存檔、OAuth、DB 與 log 均為本機敏感資料，不納入 Git。
