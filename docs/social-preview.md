# 社群連結預覽與重複分享出警

Threads、Facebook、Instagram 共用 Components V2 卡片、Webhook 代發與刪除流程。所有設定名稱及預設值見[環境變數指南](configuration.md)，完整範本見 [`.env.example`](../.env.example)。

X／Twitter 僅提供[重複分享偵測](#xtwitter-僅偵測重複分享)，不接管其他 bot 的預覽。出警有獨立開關，見[同頻道重複分享出警](#同頻道重複分享出警)。

## 伺服器開關

三平台未設定 env 時皆預設關閉。管理者可用 `/社群預覽設定` 選平台與啟用／停用／預設；`/社群預覽狀態` 顯示目前生效值與來源。Guild override 優先於 env，即使 env 關閉仍可由管理者開啟。「預設」會清除 override，回到 env。

設定存入 `guild_social_preview_settings`，即時生效且需持久資料庫。DB 無法讀寫會明確提示，不把失敗當成正常全域預設。

## 卡片與媒體

Threads、Facebook、Instagram 使用 Components V2 卡片，圖片與影片依來源附件順序共用四個名額。例如「圖、圖、影片、圖、圖」會顯示前四項，第三格保持影片；多支影片也各占一格。圖片每張最多下載 10 MiB，單則圖片合計最多 20 MiB；前四項選定後，下載失敗的圖片略過，不補抓第五項。圖片以上傳附件顯示，影片使用官方 CDN 直連、不下載或上傳，也不把影片封面另算成圖片。只有 OG 資料時無法確認完整輪播順序，會保守使用可用的單一影片或圖片。

原貼文沒有正文時，卡片直接顯示作者、媒體與按鈕，不補「無文字內容」。原訊息的留言、防雷以及抓取失敗時的錯誤提示仍保留。

預覽優先使用 Webhook 顯示貼連結者的名稱與頭像，無法代發時由 bot 發送。原始訊息在發送後嘗試刪除，無權限時嘗試收合原本 embed。長正文會分頁；垃圾桶按鈕會嘗試刪除整組預覽。影片連結有時效，Discord 接受訊息不代表每個客戶端都能正常播放。

刪除按鈕逾時由 `SOCIAL_PREVIEW_DELETE_TIMEOUT_SECONDS` 控制；`0` 代表同一程序內不自動逾時，目前未跨 bot 重啟恢復互動按鈕。

## 異常訊息與 botlog

Threads／Facebook／Instagram 遇到最終存取限制、Cookie 失敗、無可用內容或未預期處理／發送錯誤時，公開提示統一為「平台名預覽失敗 爛meta」。卡片保留評論、防雷及原連結；無法送出卡片時以同樣的固定文字回覆，原訊息保留。

詳細原因只轉發既有 `DISCORD_NOTIFY_FORWARD_CHANNEL_ID`（botlog），可用 `DISCORD_NOTIFY_FORWARD_GUILD_ID` 指定其他伺服器。使用者與來源頻道優先顯示「名稱（ID）」；社群通知使用來源訊息已有的名稱，其他數字 ID 只查現成快取，名稱缺失時保留 ID，不額外呼叫 Discord API。通知另包含固定分類、時間、去除 tracking 的原連結，以及可用的錯誤類型／HTTP 狀態／Discord code／IG trace ID，不包含 Cookie、HTML、貼文正文、媒體簽名網址或原始例外字串；此通知不寄 Gmail、不寫歷史表。

通知未設定、送達失敗或等待超過 5 秒時，伺服器仍保留安全的原因紀錄，公開卡片不改顯示診斷資訊；通知逾時就停止等待，繼續公開處理。成功備援與可用的部分內容維持既有呈現；若卡片已送出但來源清理發生未預期例外，只通知 botlog，不再追加失敗回覆。

## 兩種防雷

- **手動防雷**：在 Discord 用 `||來源連結||` 包住 URL，三平台都遮正文與全部圖片／影片；作者／平台標籤、留言及按鈕保持可見，外層卡片不再遮一層。
- **Threads 原生防雷**：只遮來源標記的文字片段或特定媒體，其他正文與媒體公開。手動防雷優先覆蓋原生標記。

原生標記只從現有抓取資料解析，不另增請求；來源未提供標記時無法還原。已知文字防雷但片段不完整時，保守遮住對應正文。Instagram／Facebook 目前不辨識貼文本身的原生防雷。這些行為不另設 env 開關。

## Cookie 設定生效方式

Cookie 是登入憑證，三平台分別設定。以下流程中的「重啟」指本機 Python 程序；Docker 的 `env_file` 更新必須依[部署指南](../tools/deploy/README.md#修改環境設定)重建 Bot，單按 Restart 不會套用。

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

6. 本機重啟 bot；Docker env_file 依[部署指南](../tools/deploy/README.md#修改環境設定)重建 Bot。再貼一次原本失敗的 Threads 連結；既有 Discord 訊息不會自動重建。

若看不到 Request Headers 的 Cookie，先確認已登入，保持 DevTools 開啟且停用快取後重新整理，再選一次主文件。操作位置可參考 [Chrome DevTools 官方 Network 文件](https://developer.chrome.com/docs/devtools/network/reference#headers)。不要使用 `document.cookie` 當完整來源，它不包含 HttpOnly Cookie。

Cookie 相當於登入憑證，只保留在自己的環境設定與本機受保護的 Cookie 儲存檔；不要貼進聊天、log、截圖或 Git。登入備援不保證每篇都成功：過期、驗證挑戰、限流、需執行 JavaScript 或頁面格式變動仍會失敗。

觀看限制、登入驗證、限流與 Cookie 備援失敗時，公開卡片統一顯示「Threads預覽失敗 爛meta」；通知行為見上方「異常訊息與 botlog」。

管理員可依 botlog 的原因處理：

- `cookie_login_required`／「重新登入或驗證」：先在瀏覽器確認登入／驗證狀態，有需要時重新複製 Cookie、更新 `.env` 並套用設定。401／403 也可能是存取被拒，不能單憑狀態碼斷言 Cookie 已過期。
- `cookie_rate_limited`／「暫時限制請求」：稍後再試，程式不會自動重試或切換帳號。
- `cookie_no_content`／「仍無法取得」：沒有找到能對應這則貼文的資料，保留原連結供手動開啟。
- `cookie_restricted_content`／「登入備援仍收到限制觀看頁」：請用提供 Cookie 的同一帳號在瀏覽器確認；這個狀態不會被誤報為 Cookie 過期。平台限制頁不再當成正文，會正常觸發備援。

清空或移除 `THREADS_COOKIE` 並套用設定，就會恢復全程匿名抓取。

`cookie_no_content` 不等於 Cookie 過期。log 的 `reason=parse_budget_exhausted` 代表解析額度不足，`json_missing` 代表未找到 JSON 資料，`target_not_found` 代表沒有對應目標貼文的節點；`viewer=present` 僅是頁面包含登入狀態線索，不能單獨保證登入有效。

需要單次本機診斷時，可手動執行：

```powershell
.\.venv\Scripts\python.exe tools/diagnose_threads_cookie.py 'https://www.threads.com/@使用者/post/貼文代碼'
```

工具會使用環境或 bot 對應 `.env` 中的 `THREADS_COOKIE`，只請求一次，只輸出狀態、是否有正文與媒體數量等診斷資訊；不輸出 Cookie、正文、媒體網址或 HTML，不保存回應，也不更新設定。

三平台共用 Cookie 自動保存設定，行為見下方「Cookie 自動保存」。診斷工具刻意只使用環境初始值，不讀寫自動保存檔，因此其結果可能與已更新 Cookie 的 bot 不同。

## Facebook 影片與 Cookie 備援

Facebook 公開影片使用可用的 MP4 直連，從 Open Graph 或目標貼文的 JSON 資料取得，同一影片優先使用 HD。有明確附件順序時，圖片與影片一起取前四項，直接放入 V2 卡片；只有 OG 備援時使用第一支影片，避免把封面當成額外照片。不下載／重新上傳影片；沒有可用 MP4 時維持文字、封面與原連結。DASH／HLS manifest、分段網址、已知無聲影像、直播或需 DRM 的來源不會當成完整 MP4。

選填 `FACEBOOK_COOKIE` 可啟用獨立 Facebook 登入備援：匿名流程無可用內容，或影片頁只有封面卻沒有 MP4 時，最多對已解析的目標貼文增加一次請求。若匿名已取得影片，或一般圖片貼文已有內容，就不使用 Cookie。

照片分享可能先導向 `photo.php`，再導向登入頁。程式會保留轉址途中已確認的照片／貼文目標；只有登入頁提供 `next` 時，也只接受經驗證的 Facebook 內容 URL 作為目標提示。登入頁和提示本身不是公開證明，Cookie 回應仍須確認目標與公開範圍。所有匿名候選仍未取得目標時，不送出 Cookie，保留匿名流程的失敗分類；外部 redirect 仍直接拒絕。

取得方式與 Threads 相同：在已登入的 Facebook 目標頁開啟 Chrome DevTools → Network → 重新整理 → 選主文件 → Headers → Request Headers → Cookie，複製整段值，放入 bot 實際使用的 `.env`：

```env
FACEBOOK_COOKIE='在這裡貼上複製的整段 Cookie 值'
```

外層單引號保留，整段維持同一行；不要包含 `Cookie:` 字樣，也不要提供 `Set-Cookie`。填妥後套用設定，重新貼文測試。Cookie 只送到 `https://www.facebook.com`，不跟隨登入請求的轉址、不送至媒體 CDN、不使用 Threads Cookie，也不會寫進 log、Git 或 HTML 除錯檔。

登入回應只接受與目標貼文綁定、且 privacy 欄位能確認為公開的內容。私人、朋友、自訂或無法確認的範圍不轉發；有匿名封面時即使 Cookie 失敗也保留原本預覽。沒有匿名內容且登入也不可用時，沿用既有失敗卡片與原連結。

需手動診斷時，使用已解析的一般貼文、Reel 或影片網址（不要使用 `/share/...` 短連結）：

```powershell
.\.venv\Scripts\python.exe tools/diagnose_facebook_cookie.py 'https://www.facebook.com/reel/影片代碼/'
```

工具只請求一次，僅輸出狀態、計數與公開狀態；不顯示正文、圖片／影片網址或登入資料，不讀寫自動保存的 Cookie。CDN 直連可能失效，實際播放與聲音仍應在 Discord 驗收。

加上 `--check-config` 可只檢查 Cookie 格式與登入欄位是否存在，不發出網路請求。格式有效不代表登入仍有效；`cookie_request_rejected` 代表 Facebook 回覆 HTTP 400，不能直接視為 Cookie 過期。工具可額外回報固定的錯誤提示分類，不輸出原始錯誤頁。

## Instagram 擷取與 Cookie 備援

Instagram 優先自行抓取官方資料。先匿名讀取官方貼文頁；若只取得公開 OG 首圖或未取得完整媒體，且設定了 `INSTAGRAM_COOKIE`，優先追加一次同網域目標媒體 API 請求。匿名已取得完整輪播或 Reel 影片則不帶 Cookie。正文優先採用 `caption.text`，不把圖片替代描述當成貼文正文。

公開 Reel／TV 在官方頁面與已設定的 Cookie 都未取得影片時，才以 KKInstagram 作最後備援，最多一次無登入請求，僅接受可信 Meta CDN 的完整 MP4 直連；不向 Discord 貼第三方 embed 網址。已知私人、404、限流或沒有可用公開內容時不走第三方，也不猜測一般多圖貼文中缺失影片的位置。備援失敗仍保留公開文字與封面；完全沒有內容則顯示失敗卡片與原連結。留言、防雷、Webhook 代發、Bot 備援及整組刪除與其他平台一致。

取得 Instagram Cookie：

1. 在瀏覽器登入 `https://www.instagram.com`，開啟一則自己可正常觀看的公開貼文。
2. 按 F12 開啟開發人員工具，在 Network 面板重新整理，選取 `www.instagram.com` 的貼文 Document 或網站請求。
3. 在 Request Headers 找到 `Cookie`，只複製值，貼入本機／部署環境的 `INSTAGRAM_COOKIE='完整 Cookie 值'`。不要包含 `Cookie:` 前綴，也不要使用 Response Headers 的 `Set-Cookie`。
4. 本機重啟 bot；Docker env_file 需依部署指南重建。Instagram Cookie 與 Threads、Facebook 分開設定、分開保存，不互相代用。

Cookie 只傳給 HTTPS `www.instagram.com` 的目標媒體 API，不跟隨轉址、不傳媒體 CDN 或第三方。登入回應須綁定原貼文 shortcode，且作者 `is_private=false` 才能轉發。Cookie 不保證所有貼文可取得；私人內容、驗證挑戰、限流及網站結構變動仍可能失敗。Log 出現 `checkpoint_required` 代表 Instagram 要求額外驗證，須先在瀏覽器完成 Instagram 驗證，再更新 Cookie；不會自動重試登入挑戰。

### 社群預覽 log

進入預覽處理時，Threads、Facebook、Instagram 各記錄一行 INFO；X 在偵測到有效 status 連結時記錄。沿用 console 與 `bot.log`。以下為虛構格式範例，名稱與大括號內的帳號／貼文代碼均為占位符，不對應真實訊息或可使用的貼文網址：

```text
範例使用者 在 範例頻道 貼了 Threads url https://www.threads.com/@{example_author}/post/{example_post_id}
範例使用者 在 範例頻道 貼了 Facebook url https://www.facebook.com/share/p/{example_share_id}/
範例使用者 在 範例頻道 貼了 Instagram url https://www.instagram.com/p/{example_shortcode}/
範例使用者 在 範例頻道 貼了 X url https://x.com/{example_author}/status/{example_status_id}
```

每行由 logging 加上時間、等級與 logger 名稱。這是連結偵測紀錄，不是預覽送達或 DB 寫入證明；三平台在抓取前記錄，可能仍是 Facebook share 網址。一般活動行只有顯示名稱與頻道名稱，不含 guild／channel／user ID、來源或預覽訊息 ID，不能直接還原 `social_share_events`。回補流程見[開發指南](development.md#一次性社群分享歷史回補)，檔案持久性見[部署指南](../tools/deploy/README.md#log-保留與容器更新)。

Instagram 成功階段、完整成功摘要與 V2 發送成功的詳細診斷均為 DEBUG，預設不輸出；Cookie 例行保存成功也為 DEBUG。HTTP 失敗、驗證挑戰、KK 接手、Reel 仍無影片、媒體下載略過或錯誤卡片才輸出 WARNING，組裝／發送失敗輸出 ERROR。匿名內容不足但 Cookie 正常補回是預期備援；前四項限制省略後續媒體也不算異常。

詳細紀錄以 `instagram_diagnostic` 開頭，後接單行 JSON；同次預覽沿用 `trace_id`，`shortcode` 用來辨認貼文。診斷不記錄 Cookie、回應內容、正文或完整媒體網址。

| `event` | 觀察重點 |
| --- | --- |
| `fetch_stage` | `stage=anonymous/cookie/kkinstagram`、`http_status`、固定 `result`／`reason`、`elapsed_ms`、抓到的媒體數；可分辨 `checkpoint_required`、`rate_limited`、`timeout` 等原因。 |
| `fallback` | 切換到 Cookie 或 KK 的原因，例如 `missing_video`；切換 KK 時附上 `cookie_result`。 |
| `fetch_complete` | 每次抓取一筆摘要；正常為 DEBUG、異常為 WARNING。記錄最終 `source`、`cookie_result`、`kk_result`、`outcome` 與總耗時。Reel 的 `images_only` 代表仍只有封面，不能算影片補抓成功。 |
| `preview_prepared` | V2 準備結果、選入／保留／略過的媒體數、防雷及準備耗時（含抓取與圖片下載）；`outcome=error_card` 表示錯誤提示卡片。 |
| `preview_delivery` | Discord V2 `result=sent/failed`、`phase`、頁數與該階段耗時；失敗記例外類型及可取得的 HTTP status／Discord error code，不記原始錯誤內容。 |

篩選方式（在自己的專案目錄手動執行，不外貼完整 log）：

```powershell
rg 'instagram_diagnostic' bot.log
rg '貼了 (Threads|Facebook|Instagram|X) url' bot.log
rg '某筆紀錄的trace_id' bot.log
```

常態 log 可用貼連結行查看處理量，再用相同 `trace_id` 合併異常紀錄；WARNING 不是全量樣本，不可直接拿其筆數計算成功率。若維護時暫時將 logger 及對應 handler 調到 DEBUG，才可用完整 `fetch_complete` 統計來源成功率，避免把多階段 log 重複計數。`cookie_result=success` 只代表取得可用內容，是否有影片還須看 Cookie 階段的 `video_count`。`kk_result=not_attempted` 表示沒有呼叫 KK；`unavailable` 的詳細原因可用相同 `trace_id` 查看 KK 階段。`sent` 只代表 Discord 接受 V2 訊息，不能證明使用者端影片已播放成功。

可先檢查格式（不發請求），再明確執行一次登入擷取診斷：

```powershell
.\.venv\Scripts\python.exe tools/diagnose_instagram_cookie.py 'https://www.instagram.com/p/貼文代碼/' --check-config
.\.venv\Scripts\python.exe tools/diagnose_instagram_cookie.py 'https://www.instagram.com/p/貼文代碼/'
```

診斷只顯示固定結果及文字／媒體計數，不輸出 Cookie、正文或媒體網址，也不讀寫自動保存的 Cookie。格式有效不代表登入仍有效。請只將 Cookie 保存在自己的環境設定，不貼入聊天或提交到 Git。

## Cookie 自動保存

預設開啟，適用於 Threads、Facebook 與 Instagram。三個平台先匿名抓取，只有需要登入備援時才帶各自的 Cookie，不增加背景或定時請求。

- 首次備援以 `.env` 的 `THREADS_COOKIE` / `FACEBOOK_COOKIE` / `INSTAGRAM_COOKIE` 建立各自的保存檔；成功解析可轉發的目標貼文後，合併該回應的 `Set-Cookie`。後續備援與重啟沿用更新值，不改寫 `.env`。
- 處理網站指定的有效期、刪除、網域與路徑；登入失敗、限流、限制頁、私人內容或解析失敗不更新 Cookie。網站沒有回傳更新時不會自行產生新 Cookie。
- 手動換成新的環境 Cookie 並重啟，會取代該平台快取；清空該環境變數並重啟即停用登入備援，即使舊保存檔仍在也不會使用。`SOCIAL_PREVIEW_COOKIE_AUTO_SAVE=0` 則停用快取讀寫，回到固定使用環境值。
- 預設目錄為專案內的 `secrets/social-preview-cookies/`，平台檔名分別為 `threads.cookies`、`facebook.cookies`、`instagram.cookies`；已由 Git 與 Docker 排除。可用 `SOCIAL_PREVIEW_COOKIE_STORE_DIR` 指定持久目錄；容器必須掛載持久 volume 才能跨重建保留。
- Windows 使用 [DPAPI CurrentUser](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) 加密，須以同一個 Windows 帳號執行 bot。Linux / POSIX 使用 0600 檔案權限、新目錄 0700；檔案內容仍是敏感憑證，不應公開或加入版本控制。
- 檔案無法讀取、解密或寫入時會記錄不含憑證的警告，繼續嘗試環境初始值，不覆蓋無法讀取的檔案。若需重建，先停止 bot，再刪除該平台的 `.cookies` 檔，重啟後由環境初始值建立。每個目錄只供一個 bot 程序寫入。

自動保存只能接收網站已發出的 Cookie 更新，無法自動登入或解除 checkpoint／MFA。長期沒有觸發備援、登入被撤銷，或網站要求驗證時，仍需在瀏覽器完成登入後重新複製 Cookie。

## 同頻道重複分享出警

預設開啟，載入最近七天分享；管理者用 `/社群出警設定` 修改開關及正整數天數，用 `/社群出警狀態` 查看是否就緒。guild 設定優先於 `SOCIAL_DUPLICATE_ENABLED` / `SOCIAL_DUPLICATE_WINDOW_DAYS`。

Threads／Facebook／Instagram 只對已啟用預覽的平台、已確認的文章與完整送達的預覽記錄。不同 share 網址必須解析成同篇文章才會命中。重複時照常送預覽，另以固定名稱「不看訊息警察」的專用 webhook 標記本次分享者，附上同頻道最早的快取預覽跳轉連結，不重貼原正文。Webhook 不支援原生回覆；缺少管理 Webhook 權限或發送失敗時，改由 bot 原生回覆舊預覽，舊訊息已刪除時改一般提示。並發分享以先完成送達與登記者作為原始預覽。X／Twitter 的獨立偵測方式見下節。

警報採 Components V2：橘色色條 Container 內有粗體「不看訊息警察」、警報文字、原始連結，右側為 GIF 縮圖。頭像與 GIF 使用固定 CDN 資源，位置集中在 `duplicate_webhook.py`；頭像以每則 `avatar_url` 指定，既有專用 webhook 也會使用新資源。卡片不附測試註記，只允許通知本次分享者，無其他互動控制鈕；CDN 內容是否正常顯示仍受來源可用性及 Discord 客戶端影響。

Threads、Instagram、Facebook 與 X／Twitter 均經過共用出警流程，使用相同 V2 格式及 CDN 資源；各平台原有的觸發條件與開關維持不變。缺少 Webhook 權限或發送失敗時，仍保留 bot 文字回覆備援。

同連結在同頻道總共第 2 次出現（首次出警）用 CDN 狗狗 PNG 頭像，第 3 次起改用 CDN 警察 JPG；右側始終為同一個狗狗 GIF。不同連結各自計數，不因另一篇已升級就改掉自己的頭像。兩種頭像仍使用各自的專用 webhook，顯示名稱都維持「不看訊息警察」，不修改 webhook 全域頭像。計數隨快取從 DB 的載入期間恢復，運作中增量累計；重送相同來源事件不重複計數。調整天數／重啟時，只計重建範圍內的分享，不計整個 DB 的永久累積次數。

出警連結行顯示為 `⬆️ 查看原始連結`，只有文字可點擊，跳轉到先前的 Discord 分享訊息（三平台為預覽，X 為原留言）。第 2 次不加尾句；第 3～5 次在連結後分別加上「第 3 次了！三回啊三回！」、「第 4 次了！你是只看自己傳的訊息是不是！」、「第 5 次了！警察都認得這篇了，你還不認得！」。第 6 次分享起先生成吐槽，再發警報；固定由程式加上「第 N 次了！」，N 使用阿拉伯數字，模型只提供後面的原句，吐槽不包在連結內。

Gemini 吐槽為單段繁體中文約 20～100 字（不含程式附加次數），可按次數玩梗、諧音，
自然使用當下暱稱；輸入只有分享次數與最多 32 字的顯示名稱，沒有聊天內容、貼文、ID 或網址。
提示詞要求只回傳成品，不加標題、分析或說明；輸出驗證會排除長度不符、換行、
Markdown、網址與 mention。原暱稱僅作資料，不能改變系統指令。

沿用既有 cloud role Gemini，不受 ROLE_MODE 控制，缺少金鑰或 SDK、API 錯誤、
生成逾時或格式不符就從本機預設句隨機選一段；其中保留「這篇都快被你分享成每日簽到任務了」。
不額外重試生成。生成（含排隊）最多 6 秒、最多 2 個並行請求；生成完成後才啟動獨立的
5 秒發送時限，避免等 AI 耗盡原有發送時間。bot 備援訊息也保留同一段吐槽。
Gemini 本身的 503 備援模型仍受這次生成的總時限限制，429 直接用本機句，不換模型重試。

DB 保存完整事件歷史；記憶體只放文章識別與必要 ID。每次啟動從 DB 載入最近 N 天，期間增量更新，不逐次計算到期、不定時清理。修改設定時重建該 guild 快取；長期不重啟時判重範圍與快取隨資料增加。DB 故障會暫停出警並重試恢復，既有預覽照常運作。

判重 key 為 `(guild_id, channel_id, platform, post_key)`，不同伺服器、頻道或平台分開計算，Threads 與 X 即使文章 key 相同也不碰撞。這是程序內索引；同一 bot 不應同時啟動多個實例期待即時共享計數。關閉出警不記錄新分享，重開只載入 DB，不回掃停用期間的 Discord 歷史。

### 出警事件與名稱快照

`social_share_events` 保存符合條件的分享；`social_patrol_events` 另外保存實際觸發的
出警事件，Threads／Instagram／Facebook／X 共用。首次分享、停用期間、未命中重複及
獨立測試發送不產生出警事件。新表由既有社群 repository 初始化自動建立，支援 SQLite／PostgreSQL。

每筆出警保留觸發者的 `user_id`、`username`、`global_name`、`display_name`，以及
伺服器／頻道 ID 和名稱快照。名字取自事件當下，不當識別鍵；取得不到為 NULL，
不回查歷史暱稱、不用現在名字回填舊事件。亦保留社群、貼文 key、正規化網址、
當次 Discord 原訊息與預覽 ID、首次分享者／原訊息 ID、跳轉目標 ID 及當時 `share_count`。
`share_count` 包含首次分享，第 2 次分享才是首次出警。

`occurred_at` 是判定出警時間，`source_message_created_at` 是原 Discord 訊息建立時間，
`completed_at` 是發送處理完成時間，均以 UTC epoch 秒保存。不要以分享表中的處理時間
冒充出警送達時間。以下狀態區分判定與實際送達：

| `delivery_status` | 意義 |
| --- | --- |
| `pending` | 已記錄判定、尚未完成；也可能是程序中止或結果更新失敗 |
| `sent` | Discord 發送呼叫成功，另存 `patrol_message_id` 與發送方式 |
| `failed` | 發送最終失敗；`error_type` 只存類別，不含原始錯誤或 token |
| `unknown` | 逾時／取消，可能已送達，不能當成確定失敗 |

發送方式為 `webhook`、`bot_reply` 或 `bot_notice`。出警紀錄寫入失敗不阻止本次原有警報，
會留下安全警告及 DB health 通知；失敗資料不自動重播。統計出警判定可計全部事件，
統計已確認送達只計 `sent`，`pending`／`unknown` 應另列。既有歷史分享不會回填成出警事件。
現行 PostgreSQL → SQLite 備份工具尚未包含此新表，不能把舊備份當成完整出警統計來源。

`taunt_text` 保存實際附加的完整吐槽（含次數），`taunt_source` 為 `none`／`fixed`／
`gemini`／`fallback`。第 2 次為空文字且來源 `none`。SQLite／PostgreSQL 初始化會為既有
出警表補上兩個 nullable 欄位，舊紀錄留 NULL，不回填不存在的歷史台詞。

### X／Twitter 僅偵測重複分享

X 預覽由其他 bot 處理。本 bot 只讀取人類原留言中的 `x.com`／`twitter.com` status 連結，以貼文 ID 判重；網域別名、帳號差異、`?s=46` 等 query、fragment 與 `/photo/1`／`/video/1` 尾碼均不影響身份。不對 X 發請求，不確認貼文存在／公開，不展開 `t.co` 或第三方 preview 網址。

支援兩網域的 `www`／`mobile` 子網域，以及 `/{帳號}/status/{ID}`、`/i/status/{ID}`、`/i/web/status/{ID}` 路徑；`x.com` 和 `twitter.com` 的相同 status ID 視為同篇。僅帳號首頁或其他非 status 網址不登記。

X 使用獨立的出警 listener，共用 `/社群出警設定` 的開關與天數，以及狗／警察頭像、次數台詞、DB 歷史和啟動恢復；與三平台預覽開關無關。第一次只記錄，同頻道重貼才出警；跳轉目標為第一次分享的 Discord 原留言。每則訊息只記錄第一個有效 X status 連結，重送同一個來源事件或同訊息重複同篇不增加計數。

偵測到有效 X 連結時，沿用共用社群活動格式記錄一行 INFO，包含安全處理過的分享者／頻道名稱及正規化網址（移除 query／fragment）。首次與重複分享都會記錄；停用出警仍保留這行偵測 log，但不寫入分享歷史或出警。bot／webhook、DM 與非貼文網址不記錄。

這條路徑不建立或轉發預覽，不刪除、編輯或壓制原留言的 embed，不偵測其他 bot／webhook 的訊息，也不追蹤編輯事件。如果其他 bot 刪除了原留言，跳轉可能失效，本 bot 不介入尋找其替代預覽。原訊息若另含 Threads／Facebook／Instagram 連結，既有三平台行為仍由原預覽流程決定，X listener 不介入。
