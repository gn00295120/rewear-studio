# 網站模式與維運

網站模式已實作獨立帳號與衣櫃，公開環境尚未選定。本文件提供單機、單程序的部署方式；
不代表已公開上線。最新擴大 15 張固定測試為 14/15 嚴格端到端、14/14 可重建目標；
唯一只剩袖片的目標正確要求補拍。自動同款搜尋仍沒有核驗通過結果。
核心品質狀態請看 [產品驗收](PRODUCT_READINESS.md)。
目前最終 Linux arm64 映像已通過 140 項檢查，包括 loopback HTTPS、帳號隔離、重啟、備份還原、
三候選成本契約與免費購物入口；使用臨時受信任測試憑證和離線示範。公開憑證及其他 CPU 架構尚未驗證。

## 啟動與帳號

本機模式仍為 `rewear`，只監聽 loopback。測試網站模式要使用另一個資料目錄：

```sh
rewear --hosted --public-origin http://127.0.0.1:8791 \
  --allow-insecure-local --port 8791 --hosted-data-dir /absolute/private/rewear-test
```

公開網站需要 `--public-origin https://你的網域`，不開啟 HTTP 開發選項。
資料位於 `<hosted-data>/control/accounts.sqlite3` 及 `tenants/<帳號識別碼>/`。
使用者輸入的名稱不能決定檔案路徑；所有工作、圖片、匯出及修改都經過伺服器帳號驗證。
現有本機資料不會自動移入任何帳號，也不會透過網站模式公開。

註冊使用帳號名稱和至少 12 字元的密碼，不需要電子郵件。密碼以 Argon2id 儲存。
註冊時只顯示一次復原碼，使用者須保存後才進入衣櫃；復原成功會換發新碼並撤銷舊登入。
目前沒有郵件重設、社群登入或管理員代發密碼流程。遺失密碼和復原碼時無法自行恢復登入。
使用者可在帳號管理修改密碼、登出、查看容量及重試未完成的媒體清理。
登入有效期為 8 小時；HTTP-only cookie、CSRF 與精確來源檢查共同保護修改操作。
HTTPS 模式使用 `__Host-` Secure cookie；開發用 HTTP cookie 只適用 loopback。

網站上的「本機模型」是**伺服器的 Ollama**；照片仍會上傳到伺服器。
Meta 與可選 Google 圖片索引的資料傳送規則與本機版本相同，見 [README](../README.md)。

## 評審／Demo 網站（帳號由營運者建立）

給評審試用時，不開放註冊，只發預先建立的帳號，並為每個帳號設每日費用上限：

```sh
rewear account create --hosted-data-dir /absolute/private/rewear-site --username judge --display-name "Hackathon Judge"
rewear --hosted --public-origin https://你的網域 --hosted-data-dir /absolute/private/rewear-site \
  --accounts managed --daily-spend-limit-usd 5
```

- `rewear account create` 會產生 20 字元隨機密碼並**只顯示一次**；要指定密碼可先設 `REWEAR_ACCOUNT_PASSWORD`。
  同名帳號已存在時會拒絕，不會覆寫。容器部署改用
  `docker compose --env-file … -f deploy/compose.yaml exec app rewear account create --hosted-data-dir /var/lib/rewear/data --username judge`。
- `managed` 模式下 `/auth/register`、`/auth/password`、`/auth/recover` 都回 403；登入頁只剩「Sign In」並說明帳號由主辦方提供，
  帳號管理也不顯示改密碼。共用帳號因此不會被其中一位評審改掉密碼而鎖住其他人。
- 每日上限依既有的模型費用帳本計算該帳號最近 24 小時的估算費用。超過後，新的分析、重試、批次、完整預覽與
  AI 查找商品頁回 429，其他帳號不受影響；已開始的分析與其自動預覽會照常完成，所以實際花費可能略超過上限。
  這是應用程式內的保險，最終上限請另外在 Meta 開發者後台的 Billing 頁面設定。
- 帳號名稱與密碼只放在提交表單給評審的欄位，不要寫進公開 repo。

## 容量與工作限制

啟動參數可調整：

| 參數 | 預設 | 用途 |
| --- | --- | --- |
| `--max-accounts` | 1000 | 最多註冊帳號數 |
| `--max-active-tenants` | 16 | 同時載入的衣櫃；有工作或串流的衣櫃不會被逐出 |
| `--max-jobs-per-tenant` | 200 | 每個帳號保留的工作數 |
| `--max-media-mib-per-tenant` | 2048 | 每帳號媒體准入限制，MiB |
| `--accounts` | `open` | `managed`：不開放註冊、改密碼與自助復原，帳號由營運者建立（環境變數 `REWEAR_ACCOUNTS`） |
| `--daily-spend-limit-usd` | 無 | 每帳號最近 24 小時的模型估算費用上限，美元（環境變數 `REWEAR_DAILY_SPEND_LIMIT_USD`） |

所有帳號共用最多 64 筆排隊／執行任務；分析、重建、購買搜尋各一次執行一件。
新工作先保留空間，分析每件 32 MiB、重建 64 MiB、購買搜尋 24 MiB，另計原始檔案。
保留量直到所屬背景工作結束才釋放。這是應用程式准入限制，**不是檔案系統硬配額**；
實際生成資料可能超過預留，超過後拒絕新增，既有結果、匯出、取消及刪除仍可用。
計量範圍是各衣櫃的 `jobs/` 和 `.trash/` 常規檔案，不包含 SQLite 歷史、帳號／批次 metadata
與 multipart 上傳暫存；這些也需納入部署層的磁碟監控。
部署者應依磁碟與模型額度降低帳號數／媒體上限，並監看磁碟、記憶體及服務日誌。
容器配置限制記憶體及程序數，不能代替磁碟配額或雲端模型費用上限。

網站刪除工作會永久移除該工作來源與派生媒體。若清理失敗，檔案在該帳號的 `.trash/`
保留並繼續計入容量；重啟後仍可從帳號管理重試。清理成功不代表已清除先前離線備份，
資料庫的歷史稽核紀錄也仍保留。本機模式維持原本的回收目錄行為。
目前沒有使用者自行刪除整個帳號的流程，不應對外宣稱有這項功能。

## 容器與 HTTPS

`deploy/Dockerfile` 只接收已建置 wheel 與鎖定的相依套件。`.dockerignore` 排除原始照片、
資料庫、測試輸出、金鑰與其他專案檔案。應用程式以 UID/GID 10001 執行，根目錄唯讀，
持久資料單獨放在 volume 的 `/var/lib/rewear/data` 子目錄。Python 和 Caddy 基底以 digest 固定；系統 ffmpeg 套件安裝仍依
建置當時的套件庫，因此另保存實際 image ID，不能只依 tag 判定版本相同。

在選定的主機準備私密環境檔，依 `deploy/env.example` 填入已授權的模型設定及網域。
範例網域不可直接使用。環境檔不要放入公開目錄或版本庫。

```sh
uv sync --frozen --extra dev --extra image-search
uv build
docker compose --env-file /absolute/private/rewear.env -f deploy/compose.yaml config --quiet
docker compose --env-file /absolute/private/rewear.env -f deploy/compose.yaml build app
docker compose --env-file /absolute/private/rewear.env -f deploy/compose.yaml up -d
docker compose --env-file /absolute/private/rewear.env -f deploy/compose.yaml ps
```

Caddy 與應用程式共用網路 namespace。只有 80/443 映射至主機，應用程式只綁定容器 loopback
8765，並只信任 loopback 代理。設定檔讓 Caddy 處理 HTTPS 與最多 257 MiB 的請求；
應用程式仍逐檔與整批檢查大小。不要另外公開 8765 或盲目信任任意 `X-Forwarded-For`。
網域 DNS、80/443 可達與實際憑證需在選定環境驗證。操作依據：
[Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、
[請求大小](https://caddyserver.com/docs/caddyfile/directives/request_body)、
[Uvicorn 代理設定](https://uvicorn.dev/settings/)。

`/healthz` 只回傳服務是否就緒，不回帳號、照片或金鑰。模型可用性是另一個檢查，
健康回應不代表模型、Google 權限或商品搜尋成功。
所有帳號共用同一套模型服務設定，不支援把其他使用者的憑證放進前端。
這個部署包不支援多副本共用 SQLite。升級時重建整個 app/proxy 組合，保留資料 volume；
不要只替換 app 卻留下仍共用舊網路 namespace 的 proxy。

## 離線備份與還原

先停止服務；執行中的資料目錄會被鎖定而拒絕備份。指定新檔名，不覆寫既有備份：

```sh
rewear backup --mode hosted --data-dir /absolute/private/hosted-data \
  --output /absolute/private/backups/rewear-20261022.zip
rewear restore --archive /absolute/private/backups/rewear-20261022.zip \
  --destination /absolute/private/restored-data
```

本機資料使用 `--mode local`。備份包含有效帳號、登入／復原資訊、每個衣櫃 SQLite 及工作媒體，
以 SQLite backup API 處理 WAL。排除秘密環境檔、鎖、暫存與已刪除工作回收檔；
還原不會恢復已刪除的媒體。ZIP 權限為 0600，**沒有加密**，應保存在受保護位置。
檔案雜湊用來檢查損毀，不是數位簽章；只還原自己信任的備份。

還原先驗證清單、檔案大小與雜湊、路徑、SQLite 結構和媒體參照，再原子放入目標。
只接受不存在或空白目錄，拒絕覆蓋既有衣櫃。最大 50,000 檔、總計 20 GiB、單檔 2 GiB；
超出時拒絕，需另規劃停機檔案系統快照，不能略過驗證。

容器部署先 `docker compose … stop proxy app`。用同一個 image、同一資料 volume 啟動
一次性 `rewear backup --mode hosted --data-dir /var/lib/rewear/data --output /backups/新檔名.zip`，
另掛載只有操作人員可讀且 UID 10001 可寫的備份目錄至 `/backups`；
不要把備份留在資料 volume 裡。還原時使用**新的 volume** 掛載 `/var/lib/rewear`，
還原目的地指定尚不存在的 `/var/lib/rewear/data`；不要把掛載點本身當成原子還原目標。
先在 loopback 驗證帳號、
媒體、工作數與匯出，再將 app 指向新 volume。保留舊 volume 和舊 image 直到驗證完成。

升級前保存 image ID、wheel 雜湊及離線備份；升級後檢查健康、登入、衣櫃、媒體和匯出。
若升級後已有新資料，不可直接用舊備份覆蓋；先保存當前資料，再於新目錄還原舊版供比對。
原始照片屬於使用者資料，備份保留期間與刪除處理需由部署者明確設定。

開發環境可重跑隔離驗證（需要 Docker、OpenSSL 與已建置映像）：

```sh
uv run python scripts/deploy_context_smoke.py --output /absolute/new-context-report.json
uv run python scripts/container_smoke.py --image rewear-studio:local --output /absolute/new-smoke-directory
```

測試只建立自己標籤的容器與 volume，使用離線示範和臨時憑證，不呼叫模型、不進行公開部署。
