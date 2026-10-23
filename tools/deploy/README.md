# GitHub main 自動部署

> 文件不記錄個人帳號或主機位址；`<owner>` 為占位符。實際映像來源由 `.github/workflows/deploy.yml` 與部署腳本的固定設定決定，執行前須核對，修改文件範例不會變更部署來源。

此設定對應本專案的既有 VPS Docker Compose。Portainer 僅查看容器，不接管 Stack。首次安裝、GitHub Secrets、GHCR 登入完成後，推送 main 會觸發部署，也可在 main 手動執行 workflow。本文描述部署流程，不代表已驗證目前 VPS 的掛載、log 保留或運作狀態。

## 憑證事件修補與離線驗收（2026-10-23）

依 [Python 官方公告](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576)，Dockerfile 先以公告所附 SHA256 的 pip 26.2.2 wheel 引導更新，再安裝應用依賴；certifi 固定 2026.10.19。aiohttp 更新至已發布的 3.14.5，參見[官方變更紀錄](https://docs.aiohttp.org/en/v3.14.5/changes.html)。

CI 使用 `docker build --pull --no-cache`：前者重新解析 base image，後者停用建置 layer 快取。`pip --no-cache-dir` 只控制套件快取，不能取代這兩個選項。Dockerfile 另更新系統 ca-certificates；不固定移除其他未受影響的 emSign roots。

本機驗收命令（PowerShell，不啟動 Gateway、不載入 env）：

```powershell
docker build --pull --no-cache --platform linux/amd64 -t discord-bot:trust-check .
docker run --rm --network none --entrypoint python --mount "type=bind,source=${PWD}/tools,target=/app/tools,readonly" discord-bot:trust-check tools/verify_image_trust.py
docker run --rm --network none --entrypoint python discord-bot:trust-check -m pip check
docker run --rm --network none --entrypoint python --mount "type=bind,source=${PWD}/tests,target=/app/tests,readonly" --mount "type=bind,source=${PWD}/tools,target=/app/tools,readonly" discord-bot:trust-check -m unittest discover -s tests -t .
```

驗收工具直接讀系統、應用 certifi、pip 內嵌 certifi 的 PEM，轉 DER 計算 SHA256，拒絕 emSign ECC Root CA - G3 指紋 `86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`（[事件資料](https://github.com/galadran/we-have-a-problem.com/blob/main/roots.json)）。空 bundle、缺檔、版本不足也會失敗，不依賴 SSLContext 預載或延遲載入的 CA 列表。CI 在發布前執行相同檢查及回歸測試。這些驗收不代表 VPS 已更新；main push 仍會觸發既有部署流程。

2026-10-23 本機實際驗收：以 `--pull --no-cache` 建置 `discord-bot:trust-check`，base manifest 為 `sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377`，輸出 image index 為 `sha256:ff955ef187e08dabcadb7e8fff35ffc2e2058dfda7fe10e7b8aa4cec72749107`。pip 26.2.2、certifi 2026.10.19、aiohttp 3.14.5，`pip check` 通過。系統 bundle 共 148 張、兩份 certifi 各 120 張，三者均未包含受影響指紋。系統 PEM SHA256 為 `e9c7b63ee2623386e9b0a3ee32f499d01a8ea23dc83b8383ddff9f8e749543b7`；兩份 certifi PEM 均為 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785`。映像內離線完整測試結果為 `Ran 432 tests`、`OK (skipped=1)`，另有真實 discord.py 子程序測試通過。此為本機快照，未發布映像、未部署 VPS。

## 1. 從 Windows 上傳部署工具

在 Windows PowerShell 的專案根目錄執行，輸入既有 SSH 位址及 Port：

```powershell
Set-Location (Read-Host '本機專案根目錄')
$VpsHost = Read-Host 'VPS IP 或 SSH 主機名稱'
$VpsPort = [int](Read-Host 'SSH Port，例如 22')
ssh -p $VpsPort "ubuntu@$VpsHost" 'mkdir -p ~/github-bot-deploy'
scp -P $VpsPort tools/deploy/discord-bot-deploy.sh tools/deploy/install-deploy.sh "ubuntu@${VpsHost}:github-bot-deploy/"
```

這裡使用你原本登入 VPS 的方式，不是剛建立的部署私鑰。

## 2. 在 Linux VPS 安裝受限入口

前置條件：/home/ubuntu/.ssh/github_discord_deploy.pub 已由使用者建立。

```bash
sudo bash /home/ubuntu/github-bot-deploy/install-deploy.sh
```

安裝器會安裝 root 擁有的 /usr/local/sbin/discord-bot-deploy 與限定此腳本的 sudoers，並將公鑰以 restrict + forced command 加入 ubuntu 的 authorized_keys。不覆蓋其他公鑰、不加入 docker 群組、不改 SSH daemon、不部署或重啟容器。重複安裝會保留同一受限公鑰；若已有不同形式的同一把公鑰則停止。

預期最後輸出：

```text
Deployment entry ready. Compose is valid and the existing bot is running. No deployment performed.
Installed restricted deployment entry. No containers were updated.
```

使用者 ubuntu 本身可能仍有原本的 sudo 權限；此處限制的是這一把部署 SSH 金鑰，而不是取消原管理帳號權限。

## 3. 檢查 SSH 公鑰指紋與實際受限登入

在 VPS 查看主機公鑰指紋（不是部署金鑰指紋）：

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

以部署 key 從 VPS 自己連回本機，將 Port 改成實際 SSH Port：

```bash
ssh -p 22 -i ~/.ssh/github_discord_deploy \
  -o IdentitiesOnly=yes -o HostKeyAlgorithms=ssh-ed25519 \
  ubuntu@127.0.0.1 check
```

首次連線只在顯示的指紋與上一步相同時接受。check 只驗證 Compose 與現有容器，不需要 GHCR 登入，也不會部署。若 SSH 沒有監聽 loopback，改用實際可達 SSH 位址。

## 4. 讓 VPS 可以拉取私有 GHCR image

GitHub 帳號 Settings → Developer settings → Personal access tokens → Tokens (classic)，建立有 read:packages 的 token，期限依需求設定。Token 的帳號必須可讀取此 Package。不要把 token 放進 repo 或對話。

在 Linux VPS 互動輸入，token 輸入不回顯：

```bash
read -rsp 'GHCR read:packages token: ' GHCR_READ_TOKEN
printf '\n'
read -rp '可讀取此 Package 的 GitHub 帳號: ' ghcr_user
printf '%s' "$GHCR_READ_TOKEN" | sudo docker login ghcr.io -u "$ghcr_user" --password-stdin
unset GHCR_READ_TOKEN
```

此處使用 sudo docker，因為部署腳本由 root 拉取 image。憑證存放在 VPS root 的 Docker 設定。Workflow 發布則使用 GitHub 自動提供的 GITHUB_TOKEN，不需要另外放一個可寫 Package 的 PAT。

## 5. GitHub production Environment Secrets

Repository → Settings → Environments → production → Environment secrets → Add environment secret：

部署 job 已指定 `environment: production`，因此可以讀取此環境內的 Secrets；不需要另外複製到 Repository secrets。建置 job 不引用此環境。若 production 設有必要審核者或分支限制，部署必須先滿足這些規則。

| 名稱 | 值 |
| --- | --- |
| VPS_HOST | GitHub runner 可直接連線的 VPS IP 或 SSH DNS 主機名稱，不含 https:// |
| VPS_PORT | 實際 SSH Port，例如 22 |
| VPS_USER | ubuntu |
| VPS_SSH_KEY | /home/ubuntu/.ssh/github_discord_deploy 的完整私鑰，含 BEGIN/END 行 |
| VPS_KNOWN_HOSTS | 與 VPS_HOST、VPS_PORT 對應的已驗證 SSH host key，見下方 |

部署私鑰直接由你複製至 GitHub Secrets，不要貼給 agent。可從 Windows 透過原本管理用 SSH 直接複製到剪貼簿，避免終端機顯示：

```powershell
ssh -p $VpsPort "ubuntu@$VpsHost" 'cat ~/.ssh/github_discord_deploy' | Set-Clipboard
```

VPS_KNOWN_HOSTS 從 VPS 既有主機公鑰產生。Linux VPS 執行以下命令，host 與 port 必須和 GitHub Secrets 完全相同：

```bash
read -rp 'VPS_HOST: ' deploy_host
read -rp 'VPS_PORT: ' deploy_port
if [ "$deploy_port" = 22 ]; then
  printf '%s ' "$deploy_host"
else
  printf '[%s]:%s ' "$deploy_host" "$deploy_port"
fi
sudo cut -d ' ' -f 1,2 /etc/ssh/ssh_host_ed25519_key.pub
```

把產生的一整行填入 VPS_KNOWN_HOSTS。這是主機公鑰，不是部署公鑰。Workflow 強制驗證 host key，沒有使用 StrictHostKeyChecking=no 或未驗證的 ssh-keyscan。

VPS_HOST 目前支援 IPv4 或 DNS 名稱。如果防火牆只放行你的 Windows IP，runner 會連線失敗；需另行規劃 runner 的連線方式，不要直接取消既有防火牆限制。

## 6. 推送 main 與驗收

先完成前述設定，再由使用者提交並推送 .github/workflows/deploy.yml、tools/deploy/、tests/test_deploy_script.py、.gitattributes。其他既有改動請另外審查。此流程不自行執行 git commit 或 git push。

在 GitHub Actions 開啟 Test, build and deploy Bot：

1. 建置 linux/amd64 image。
2. 使用此 image 的依賴執行完整 unittest，掛載測試與 tools，禁用測試容器網路；沒有正式憑證。
3. 成功才推送 ghcr.io/<owner>/discordaisummarybot:sha-<commit SHA>。
4. 執行受限 SSH 命令 deploy <commit SHA>。

確認 GitHub Repository 的組織政策允許 Actions 發布 Package。第一次 image 若已由其他流程建立而權限不同，需要在 Package Settings → Manage Actions access 授權此 repo。

在 VPS 驗收：

```bash
sudo docker ps --filter name=discord-summary-bot-bot-1 --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

再於 Discord 實測一個摘要／問答指令。容器健康檢查沿用 Compose，Flask /health 正常不代表 Discord Gateway 或 LLM 一定正常。腳本不會將 bot logs 或環境變數轉發到 GitHub。

## 更新與回退行為

- 先拉 image，失敗不重建 Bot。
- 原 Compose 不改；image 覆寫保存在 /var/lib/discord-bot-deploy/compose.image.yaml。
- 只對 bot 執行 up --no-deps --no-build --wait，不重建 db、不執行 down、不升級 DB 或 migration。
- 健康檢查失敗會嘗試用更新前 image 回退，workflow 仍標記失敗；回退也失敗時需人工處理。
- 保留上一版 image 名稱在 /var/lib/discord-bot-deploy/previous-image。不自動清除舊 image。
- 應用程式 image 回退不會還原 DB 寫入；未來 schema migration 需另外設計。
- 可用受限命令 deploy <舊 SHA> 部署仍存在於 GHCR 的已發布版本。
- CI 全流程 concurrency 不取消正在執行的部署，VPS 再用 flock 防止同時更新。
- 修改部署腳本後需重新上傳並安裝；日常應用程式 main push 不會修改 VPS 的 root 腳本。

日後手動操作 Compose 必須包含 image 覆寫，否則原 compose.yaml 仍指向舊 image：

```bash
sudo docker compose --project-name discord-summary-bot --profile bot \
  -f /opt/discord-summary-bot/compose.yaml \
  -f /var/lib/discord-bot-deploy/compose.image.yaml ps
```

第一次成功部署前覆寫檔可能還不存在，此時使用原本的 Compose。

## 修改環境設定

以下適用於已完成 GitHub 自動部署的 VPS。在同一個 Bash session 定義共用命令，固定使用目前的 image 覆寫；覆寫檔不存在時先確認部署階段，不要直接省略它重建既有服務。

```bash
bot_compose() {
  sudo docker compose --project-name discord-summary-bot --profile bot \
    -f /opt/discord-summary-bot/compose.yaml \
    -f /var/lib/discord-bot-deploy/compose.image.yaml "$@"
}
```

先確認主機 Compose 的 `env_file` 使用 `/opt/discord-summary-bot/secrets/bot.env`，再備份、編輯並驗證設定。不要把新的 `.env.example` 整份覆蓋到既有正式設定。

```bash
env_backup="/opt/discord-summary-bot/secrets/bot.env.before-edit-$(date +%Y%m%d-%H%M%S)"
sudo cp --preserve=mode,ownership /opt/discord-summary-bot/secrets/bot.env "$env_backup"
sudo chmod 0600 "$env_backup"
sudo nano /opt/discord-summary-bot/secrets/bot.env
bot_compose config --quiet
```

只有驗證成功後才執行下列命令；這一步會讓 Bot 短暫離線。`config --quiet` 不會驗證 API key、Cookie 或應用程式欄位值是否有效。

```bash
bot_compose up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 120 bot
bot_compose ps
curl --fail --max-time 10 http://127.0.0.1:8080/health
```

這會沿用目前 image 重建 Bot、不更新 DB。Docker 的 Restart 不會重新注入修改後的 `env_file`；本機直接執行 Python 則重啟程序即可。完成後另在 Discord 驗證修改涉及的功能。手動重建沒有部署腳本的自動回退；若失敗，先保留錯誤資訊，再於同一 session 還原剛才備份的 env：

```bash
sudo cp --preserve=mode,ownership "$env_backup" /opt/discord-summary-bot/secrets/bot.env
bot_compose config --quiet
```

還原驗證成功後，再執行上方相同的重建與健康檢查命令。備份含憑證，留在受限目錄；不要提交 Git。這個流程只處理 Bot 設定，不用來輪替 PostgreSQL 角色密碼。

### Cookie 持久保存

啟用三平台 Cookie 時，可設定 `SOCIAL_PREVIEW_COOKIE_STORE_DIR` 指向容器內的持久目錄。只有設定路徑不會建立 volume；主機 Compose 必須有對應掛載。image 使用 UID/GID `10001:10001`，掛載目錄須允許該身分寫入。

- Cookie 保存檔包含登入憑證，與 `bot.env` 一樣需要限制存取。
- 未掛載持久儲存時，容器重建會失去更新後的 Cookie，改以環境初始值重新建立。
- 修改掛載需編輯主機 Compose，再沿用上述驗證、重建流程；日常 GitHub image 部署不會替你修改 Compose 或 env。
- Bot 設定見[環境變數指南](../../docs/configuration.md)，Cookie 行為見[社群預覽指南](../../docs/social-preview.md)。

## Log 保留與容器更新

應用程式有兩條 log 路徑：console 交給 Docker logging driver，另由 Python `FileHandler` 以附加方式寫入工作目錄的 `bot.log`。目前 image 的工作目錄是 `/app`，所以未被掛載或其他啟動設定改變時，檔案位於 `/app/bot.log`；程式沒有內建檔案輪替，也沒有 log 路徑 env。

- 同一容器重新啟動會繼續使用原檔案；更新 image 而替換容器時，未持久掛載的檔案不會帶到新容器。
- `bot.log` 若掛載至主機檔案或 persistent volume，容器替換後可以保留，實際歷史長度仍受主機清理／輪替影響。只掛載 PostgreSQL 或 Cookie 目錄不代表 log 也持久保存。
- Docker console log 的保留由 logging driver、輪替設定與容器生命週期決定，不能因新容器沿用名稱就假設能查到舊容器歷史。Docker 的輪替設定不會自動輪替 Python 另寫的 `bot.log`。

先在 VPS 唯讀確認目前容器的工作目錄、掛載及 logging 設定；以下命令不輸出環境變數值：

```bash
sudo docker inspect --format '{{.Config.WorkingDir}}' discord-summary-bot-bot-1
sudo docker inspect --format '{{range .Mounts}}{{println .Type .Source "->" .Destination}}{{end}}' discord-summary-bot-bot-1
sudo docker inspect --format '{{json .HostConfig.LogConfig}}' discord-summary-bot-bot-1
sudo docker logs --since 24h --timestamps --tail 2000 discord-summary-bot-bot-1
```

最後一條只查看目前容器最近 24 小時、最多 2,000 行；無輸出不證明沒有更早的檔案紀錄，達到行數上限也不代表已收集完整一天。從實際掛載結果找到主機 log 後，再檢查檔案是否存在、大小、日期範圍及輪替檔，不把特定主機路徑當成 image 保證。若需跨更新保存，應先備份並確認持久掛載；不要為了讀 log 重建或移除舊容器。新掛載也無法自動恢復已遺失的歷史。

Log 可能包含私人分享網址、名稱與錯誤資訊，不外貼完整內容、不提交 Git。一般社群活動行不含完整事件 ID，也不證明預覽送達；若用於 DB 回補，依[回補指南](../../docs/development.md#一次性社群分享歷史回補)交叉核對。檔案存在、Docker healthy 或舊維運紀錄，都不能代替本次對 log 內容與範圍的確認。

## 官方參考

- https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images
- https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry
- https://docs.docker.com/reference/cli/docker/compose/up/
- https://man.openbsd.org/sshd.8 （authorized_keys restrict、command 與 SSH_ORIGINAL_COMMAND）
