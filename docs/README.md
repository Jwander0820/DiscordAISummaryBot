# 文件索引

使用方式以這裡的指南為入口，完整環境設定只維護在 [`.env.example`](../.env.example)。

| 文件 | 內容 |
| --- | --- |
| [專案 README](../README.md) | 功能與本機快速啟動 |
| [環境變數](configuration.md) | 必填／選填、程式預設、範本選擇、設定生效方式 |
| [社群預覽](social-preview.md) | 三平台、防雷、Cookie、持久保存與診斷 |
| [開發與工具](development.md) | 架構、指令限制、測試、歷史備份 |
| [GitHub 部署](../tools/deploy/README.md) | main 自動部署、修改 env、Cookie volume 與回退 |

## 文件維護

- 修改 env 名稱或行為時，同步 `.env.example` 與環境變數指南；明確區分省略預設與範本值。
- 修改預覽呈現或來源策略時，更新社群預覽指南，不把詳細診斷堆回根 README。
- 維護部署操作只保留一份現行命令；其他文件連到部署指南，避免遺漏 image 覆寫。
- 測試以現行命令及結果為準，不在使用指南固定宣稱測試總數。

本機另有 `AGENTS.md`、`docs/specs/README.md`、`docs/operations/README.md` 及 `docs/research/`，分別保存維護規則、規格索引、私人維運與歷史研究。這些預設不納入 Git，公開指南不依賴它們才能完成操作。`.gitignore` 只開放本索引與上表的三份公開指南。
