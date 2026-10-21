# DiscordAISummaryBot

以 discord.py 建立的 Discord 機器人，提供 AI 摘要、對話問答、Threads／Facebook／Instagram 連結預覽，以及同頻道重複分享出警（含 X／Twitter）。

## 功能

- Gemini 摘要與對話問答；部分角色問答可切換本地 LLM。
- 三平台共用預覽卡片，依順序顯示前四項圖片／影片，保留留言與原連結。
- 支援手動防雷及 Threads 原生局部防雷，伺服器可各別設定平台開關。
- 「不看訊息警察」偵測同頻道重複分享，管理者可設定開關與載入天數；X／Twitter 只偵測出警，不製作預覽或刪改原留言。
- SQLite／PostgreSQL 紀錄、Discord／Gmail 通知、歷史備份及 Docker 部署。

## 本機啟動

使用 Python 3.11（與 Docker image 一致）。Windows PowerShell 在專案根目錄執行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

填入 `.env` 的 `DISCORD_BOT_TOKEN`；使用 AI 功能再填 `GOOGLE_GENAI_API_KEY`。Discord 應用程式須開啟 Message Content Intent，邀請 bot 時包含 `bot`、`applications.commands`。需要社群預覽時開啟對應 env，或在伺服器使用 `/社群預覽設定`。

```powershell
.\.venv\Scripts\python.exe bot.py
```

範本採本機 SQLite、cloud 角色問答，Gmail 與三平台預覽全域預設關閉；重複分享出警預設開啟、啟動時載入最近七天。既有安裝請只補必要欄位，不覆蓋原設定；詳細設定與權限見下方指南。

## 指令

| 用途 | Slash command |
| --- | --- |
| 24 小時／1 小時／7 天摘要 | `聊那麼多誰看的完`／`整理廢話的魔法`／`命運探知之魔眼` |
| 根據近期對話回答 | `你要不要聽聽看你現在在講什麼` |
| 一般／角色問答 | `解答之書`／`el_psy_kongroo` |
| 世界線測試／偽裝發言 | `測試d-mail`／`deepfaker` |
| 伺服器社群預覽管理 | `社群預覽設定`／`社群預覽狀態` |
| 伺服器重複分享出警管理 | `社群出警設定`／`社群出警狀態` |

摘要與對話問答的 `len_msg` 可填 1～20,000；自動社群預覽直接貼連結即可。

## 文件

- [環境變數與完整範本](docs/configuration.md)
- [社群預覽、重複分享出警、防雷與 Cookie](docs/social-preview.md)
- [開發、測試與備份工具](docs/development.md)
- [GitHub 自動部署與套用 env](tools/deploy/README.md)
- [文件索引](docs/README.md)
