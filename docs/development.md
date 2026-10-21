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

`social_duplicate_cog.py` 獨立接收 X／Twitter 原留言與管理出警設定；三平台則由既有預覽流程在完整送達後登記分享。判重與持久資料放在 `duplicate_service.py`／`social_duplicate_repository.py`，關閉出警不會停用三平台預覽。

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
```

部署腳本測試需要 Bash，透過 fake Docker 執行，不操作實際 Docker daemon。Windows 若誤選 WSL Bash，改用已安裝的 Git for Windows Bash；以此工作區的安裝路徑為例，僅在本次終端調整：

```powershell
$env:PATH='C:\Software\Git\bin;C:\Software\Git\usr\bin;' + $env:PATH
```

其他安裝位置需替換路徑。以最終 `OK`／`FAILED` 判讀測試；DB unavailable 測試刻意產生 traceback，缺少 Discord／aiohttp 可能造成 skip。不要把 skip 當成已驗證。離線測試不等於實際社群抓取或 Discord 播放驗收。

## 歷史紀錄備份

`tools/sync_postgres_summaries_to_sqlite.py` 預設同步 PostgreSQL 的 `summaries` 與 `deepfaker_events` 到本機 SQLite。需要 `DATABASE_URL`，輸出由 `LOCAL_BACKUP_SQLITE_PATH` 或 `--output` 決定。這個工具會連線資料庫；先用 dry run 確認範圍：

```powershell
.\.venv\Scripts\python.exe tools/sync_postgres_summaries_to_sqlite.py --dry-run
.\.venv\Scripts\python.exe tools/sync_postgres_summaries_to_sqlite.py
```

| 選項 | 行為 |
| --- | --- |
| `--table all` | 預設，兩個歷史表各自同步；不含社群分享歷史或 guild 設定 |
| `--table summaries`／`--table deepfaker_events` | 只同步指定表 |
| `--mode incremental` | 預設，各表按本機最大 `id` 取得新增資料，不修正舊列 |
| `--mode upsert` | 重新取得資料，以 id 更新或加入本機紀錄 |
| `--mode replace` | 重建選定的本機資料表，先備份原 SQLite |
| `--limit`、`--output` | 限制每表讀取數量、指定輸出檔 |

DeepFaker 的穩定識別用 `channel_id`；`channel_name` 是事件發生時的名稱快照。完整事件統計使用 `deepfaker_events`，不由相容用的 `summaries` 推算。

Cookie 單次診斷工具見[社群預覽指南](social-preview.md)；實際 `.env`、Cookie 保存檔、OAuth、DB 與 log 均為本機敏感資料，不納入 Git。
