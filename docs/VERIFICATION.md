# 衣序 Rewear — 實際驗證紀錄

日期：2026-11-03（Asia/Taipei）。目前支援本機模式與明確啟用的帳號隔離網站模式；公開環境尚未選定。

## 簡化介面與英文化回歸修復：2026-11-03

- 即時分析失敗的根因：Meta API 金鑰所屬團隊 billing 未設定，SAM／Spark 回 HTTP 402
  `billing_not_configured`，先前被誤報為「unprocessable response」。現在照實顯示並指向 dev.meta.ai 處理；
  帳號擁有者已於 15:24 處理，之後的真實模型端對端結果見下方。
- 瀏覽器端對端測試 `tests/test_e2e_browser.py`（10 項）：真實伺服器、SQLite 與媒體流程，由無頭 Chromium
  操作拍照、批量匯入、拖放、詳情面板、Shop similar 連結、服務錯誤與重試、範例結果、卡片增量更新及手機版
  （下滑式面板、浮動相機）；模型回應為腳本化 fixture，不連線 Meta。此測試抓到並修正一個問題：衣物辨識失敗時，
  卡片上的「Try again」因卡片未隨工作狀態重繪而不出現。
- 真實模型端對端（Meta SAM 3.1、Muse Spark 1.3、Muse Image 1.0，獨立資料目錄）：桌機拍照 floral-dress 17.4 秒
  找到 1 件（slip dress）；批量匯入 2 張找到 5 件（denim jacket、jeans、T-shirt、denim shorts、tank top）；
  手機拍照 blue-hoodie 找到 2 件（zip-up hoodie、T-shirt）；summer-full 31.4 秒找到 2 件、AI 預覽 72 秒內完成。
  每件皆顯示 Muse Image 三候選預覽與 Shop similar 連結，無 console error 或水平溢位。實跑時發現三候選預覽
  完成會跳出紅色錯誤提示，已改為一般完成提示並以真實模型重驗。
- 英文化造成的前後端協定回歸：前端改用英文比對後端仍為中文的預覽 notice、推測展示面及前襟比對原因，
  使所有 Muse Image AI 預覽被判為未就緒；已還原為與 `reconstruction_quality.py` 逐字一致。另修復
  重複衣物比對的中文別名資料、舊版品質警告去重、預覽候選預設選取及網站模式容量判斷。
- 介面只保留「Take a photo」與「Import photos」，選取後自動分析；新增進度卡、結果橫幅（找到件數或
  錯誤原因與 Try again）及卡片「Shop similar」。修正空目錄時結果欄塌縮、手機結果頁水平溢位與搜尋圖示重疊。
- 衣物詳情簡化為大圖、名稱、屬性與「Shop similar on Google」，進階功能收進預設收合的「More」；手機為下滑式面板、
  點外側關閉（有未存編輯時不關閉）。卡片改為依 id 增量更新：重繪前後 3/3 張卡片沿用同一 DOM 節點、不重播進場動畫；
  手機捲離拍照按鈕後才顯示浮動相機按鈕。以上行為皆經桌機與手機無頭瀏覽器實測。
- 手機實測改由 `rewear --lan` 明確啟用（只信任私有區網位址並印出無登入警告）；本機模式預設仍只監聽 loopback。
- 完整程式測試 **1,591 passed**；Ruff check／format 與三份 JavaScript 語法檢查通過。桌機 1440px、手機
  390px 在獨立資料目錄走完拍照、批量匯入、失敗、範例結果、詳情與處理紀錄，無水平溢位、console 或
  page error：[截圖](../out/verification/simple-flow-20261103/screenshots/)。

## 「有沒有真的找到該商品」核對：2026-11-03

- 免費「Shop similar」是依辨識屬性組成的關鍵字搜尋，不是同款辨識。原本只辨識到「jeans, black」，搜尋字詞過於籠統。
  Spark 的款式名稱加入「可見版型與細節、不含顏色品牌」的指示後，同一張照片改為 ripped skinny jeans、
  oversized denim jacket、graphic t-shirt，Bing 結果變成同款式的黑色破洞窄管牛仔褲。
- 「Meta AI 精準比對」實測：舊查詢「jeans black solid」找到的候選款式不符；改用具體款式名稱後，候選變成
  LOVEsick Black Ripped Skinny Jeans、Blue Oversized Denim Jacket 等同款式商品，但因售完、需登入或頁面無法讀取而
  仍為 0 件核驗通過。結論：目前能找到「同款式」的真實商品，尚不能保證「同一件」；無品牌標記的街拍單品要找
  同一件，需要以圖搜圖（例如可選的 Google Vision 原圖搜尋）。
- 示範影片第 4 步改為「Shop similar」，購物頁顯示「Same style, not guaranteed to be the exact item」。

## 30 張新街拍實測與示範影片：2026-11-03

- 照片：透過 Openverse 取得 33 張 CC BY／BY-SA／CC0／PDM 授權的街拍與生活照，人工排除 3 張不適合者（名人特寫、
  性感自拍、街友肖像），保留 30 張；來源、作者與授權見
  [`web-qa-20261103/manifest.json`](../out/verification/lifestyle/fixtures/web-qa-20261103/manifest.json)。
- 以「Import photos」一次匯入 30 張，真實 SAM 3.1／Muse Spark 1.3／Muse Image 1.0，逐張依序處理約 33 分鐘；
  實際用量 US$3.26（分析 1.04、AI 預覽 2.22；160 次 SAM、222 張生成圖）。
- 結果：25/30 張找到可購衣物，共 74 件（同照重複合併後 69 件），每件都有 AI 預覽；3 張無人照片（建築、
  兩張夜景）正確回報 0 件；另有 51 個無法辨識的碎片區域。人工逐張核對：主要衣物多數正確（毛背心、牛仔褲、
  西裝褲、雨衣、和服、紗麗旁的條紋襯衫、娃娃的紅大衣、平鋪的條紋上衣與西裝外套）；漏抓西裝外套（02）、毛領外套（21）、
  白上衣（14）；標籤錯誤為黑裙判成短褲（04）、紗麗判成頭巾（19）；背景路人的衣物也會被列入（22、24、32）。
  鞋、帽、包包依設計不在生活穿搭範圍。
- 依實測修正：同一張照片重複定位的同件衣物只顯示一次（5 件，皆經原圖核對為同件）；無法辨識的碎片收合為
  「N more areas couldn't be identified · Show」且不進入「Your finds」；後端 7 則中文警告改為英文；Bing 連結固定英文介面。
  端對端測試增至 10 項，完整程式測試 **1,591 passed**。逐張核對圖：[`qa-30-photos/`](../out/verification/simple-flow-20261103/qa-30-photos/)。
- 示範影片 `media/archive/demo-ui-flow.mp4`（46 秒，1920×1080）與直式 `media/archive/demo-ui-flow-phone.mp4`：iPhone 尺寸觸控瀏覽器實錄，
  拍照（按下後的全螢幕相機取景、對焦、快門與「Use Photo」為示意畫面，因無頭瀏覽器沒有相機）→ 掃描（等待以 4 倍速顯示）→ 3 件衣物 → AI 預覽（省略約 1 分鐘等待）→ 衣物詳情 → Bing Shopping 同款式真實商品（標明不保證為同一件）。
  自動化瀏覽器開 Google Shopping 會被要求機器人驗證，因此影片改點同一面板的 Bing Shopping；真人手機不受影響。
- 情境短片 `media/production/sources/rewear-pov-film-v2.mp4`（26.4 秒，1920×1080，原檔同目錄 `rewear-pov-film.mp4` 35.6 秒保留未動）：重剪導入，
  開場即上標題、拍照改為快門閃光＋快門聲＋推進手機畫面並溶接進 App 分析畫面，進入 App 由約 12.5 秒提前到 6.3 秒；
  剪掉原片 3 處閃現的放大錯格（14.17–14.27、27.33–28.13、30.20–30.30 秒）。逐格核對：未加字卡的畫面與原片同一時間點
  平均差 ≤1.6/255；各剪接點前後逐格檢查無錯格；快門聲位於 5.73 秒。字幕 `rewear-pov-film-v2.srt` 依新節奏重新對時。
- Chrome 擴充功能「Shop this look」（`extension/`）：無頭 Chromium 載入擴充功能的 2 項端對端測試（照片上有透明覆蓋層的動態牆、
  懸停→按鈕→Rewear 分頁開啟並找出每件衣物、第二張照片沿用同一分頁；Rewear 未啟動時顯示錯誤），完整回歸 **1,593 passed**。
  實錄：示範動態牆（Pexels 授權照片，網域 feed.example 對應本機）點朋友合照，真實 SAM 3.1／Muse Spark 1.3 約 100 秒找出
  **8 pieces**（三人的襯衫、T 恤、針織上衣、牛仔褲、長褲），8 張 Muse Image 預覽約再 100 秒完成。
- 成片 `submission/rewear-demo.mp4`（101.0 秒）：上述實錄、POV 重剪版與該次分析的真實遮罩／欄位／預覽合成；配樂為程式合成的原創音樂。
  英文旁白 19 句由 Kokoro-82M（Apache-2.0，本機）合成；以 faster-whisper base.en 轉寫**混音後成品**，與旁白原稿比對的
  字錯率為 **0.5%**（209 字中 1 處：“And one tap” 聽成 “In one tap”；數字與品牌名先正規化）。旁白比壓低後的配樂高約 12 dB，
  整體 −16.0 LUFS。字幕 25 段無重疊；燒字版 `rewear-demo-subtitled.mp4` 逐場景抽格確認字幕不遮住 App 主要畫面。
  搬到 `media/production/` 後重跑流程：SRT 逐位元組相同，抽樣畫面與成片平均差 0.3–1.2/255（壓縮誤差）。
- 評審試用網站設定（2026-11-03）：`--accounts managed` 時註冊、改密碼、自助復原皆回 403，`rewear account create` 建立的帳號可登入；
  `--daily-spend-limit-usd` 依費用帳本計算最近 24 小時，超過後新分析／重試／預覽／AI 查找回 429，只影響該帳號，24 小時前的花費不計。
  新增 17 項測試（網站模式 9、CLI 8），完整回歸 **1,610 passed**。另以無頭 Chromium 實際啟動 managed 網站：登入頁只剩 Sign In、
  顯示主辦方帳號說明，評審帳號登入後帳號管理不顯示改密碼，390px 手機無水平捲動；順手修正手機版登入頁標題少一個空格。

## 固定 100 張網路生活照測試：2026-10-23

從 Pexels 建立搜尋分層的 100 張生活照便利樣本；來源、SHA-256、每張固定目標及目標錨點均在推論前
凍結。固定目標失敗時不得換成同張照片中的其他衣物，也不得以更容易的來源替換。這是工程壓力測試，
不是隨機母體抽樣，也不支持一般生活照準確率宣稱。

- 第一輪固定目標辨識為 86/100；新版在定位未解決、不確定或達上限時，以原圖執行一次通用衣物 SAM
  回退，14 個原失敗案例恢復 5 件。自動錨點資格為 91/100；直接稽核剔除 1 個混合兩件衣物的遮罩後，
  **同一固定目標辨識為 90/100**。
- 3 件雖辨識到同目標，但來源只呈現不足以判定完整衣物的局部；因此可支持重建評分的來源為 **87/100**。
- 91 個自動候選先各生成三張，共 273 張；直接逐張對照原圖後，基線忠實完整衣物為 75/100。
  針對已凍結的失敗案例修改參考圖順序、參考圖角色說明及衣料／領口／敞開前襟／分層衣物約束，
  再做受控回歸；最後忠實完整衣物為 **85/100**。正確辨識目標中的保真率為 **85/90（94.4%）**，
  可重建來源中的保真率為 **85/87（97.7%）**；全部輸入的嚴格端到端率仍為 85%。
- 最後 15 個非通過案例包括：2 件生成失真、3 件來源不足、1 件混合遮罩及 9 件漏辨識。
  所有失敗及不確定結果保留在報告，**0 件非通過候選自動發布**。
- 對最後 9 件漏辨識各追加一次完整原圖、目標特定的 SAM 探測並逐張檢視疊圖：7 件取得遮罩，
  但其中 4 件的固定錨點位在衣物外，3 件仍被背包、坐姿或畫面邊界遮住關鍵結構；另 2 件沒有遮罩。
  因此嚴格結果恢復 0 件，沒有再花費生成費用，也沒有用框線跨過錨點冒充遮罩命中。
- 基準與受控修復共保存 334 張有用量紀錄的生成圖。已知核心費用 US$7.71033095，SAM 診斷另
  **US$0.0425**（其中最後九件探測 US$0.0225）；一次磁碟滿的中斷請求沒有可持久核對的用量，
  明確排除。依保存的逐案費用估計，現在程式
  同一批執行一次分析並讓 91 個辨識目標都生成三候選，約 **US$5.42844360／100 張**，即每個輸入
  US$0.05428444。這不是定價保證。
- 完整程式測試 **1,576 passed / 97.17 秒**；生產程式、測試與腳本另經 Ruff／format、JavaScript
  語法、sdist／wheel 及專案外 wheel 流程核對。

[100 張摘要](../out/verification/reconstruction/web100-final-manifests-20261023/FINAL_100_PHOTO_REPORT.md) ·
[100 張機器可讀逐件結果](../out/verification/reconstruction/web100-final-manifests-20261023/final-100-photo-report.json) ·
[來源及固定清單](../out/verification/lifestyle/fixtures/web-100-20261023/) ·
[直接目標稽核](../out/verification/lifestyle/fixtures/web-100-20261023/direct-target-audit.json) ·
[最後九件 SAM 探測與逐張檢視](../out/verification/lifestyle/fixtures/web-100-20261023/targeted-sam-diagnostic-20261023/direct-review.json) ·
[基線生成目視結果](../out/verification/reconstruction/web100-final-manifests-20261023/direct-visual-review.json) ·
[可重建最終報告產生器](../out/verification/reconstruction/web100-final-manifests-20261023/build_final_report.py) ·
[最終完整測試](../out/verification/web100-service-refresh-20261023/pytest-final.log) ·
[JUnit](../out/verification/web100-service-refresh-20261023/pytest-final.xml) ·
[wheel 專案外驗證](../out/verification/reconstruction/web100-final-manifests-20261023/wheel-smoke-final.json) ·
[Linux arm64 容器驗收](../out/verification/web100-service-refresh-20261023/container-smoke-final/report.json) ·
[服務、備份與交付總表](../out/verification/web100-service-refresh-20261023/delivery.json)。

## 歷史擴大 15 張網路生活照固定測試：2026-10-23

從 Pexels 新增 15 張不同的生活照，涵蓋多人、遠景、背面、側面、坐姿、分層、遮擋、裁切、素色、條紋、
圖案與外套。下載後先凍結每張的固定目標和通過條件，再執行 Meta 衣物定位、SAM 3.1 分割、欄位判讀與
三候選低成本重建；固定目標失敗時不得換成同張照片中較清楚的其他衣物。

- 自動工作檢查為 12/15。直接檢查發現九人合照中央 T 恤其實有足夠可見結構，但第一個 SAM 遮罩只覆蓋
  目標框 31%，舊流程過早接受。新版保留第一個遮罩作回退，低於 50% 時以較大原圖再分割並採用較佳真實遮罩；
  相同固定目標恢復為可辨識上衣。最終固定目標取得可用辨識區域為 14/15。
- 第一輪 13 件可生成目標為 11/13；固定失敗案例顯示反折袖被拉成長袖，以及手套／披肩材質被黏進上衣。
  生產提示新增袖口、外搭配件與閉合 V 領的可觀察規則後，以相同來源受控回歸，該 13 件改善為 13/13；
  後續低覆蓋分割修正再恢復第 14 件固定目標。
- 全 15 個固定目標的嚴格端到端重建為 **14/15（93.3%）**；可重建子集為 **14/14**；另外 1 件因
  原照片只剩袖片而正確要求補拍。安全流程結果為 15/15，但補拍沒有灌入重建分子。此輪達到固定便利測試
  的 90% 目標，仍不是一般生活照母體準確率或隱藏細節真值。
- 初始 39 張候選 US$0.39；提示與分割受控回歸共 12 張 US$0.12；本次生成驗證合計 US$0.51。
  出貨路徑仍是每件一次三候選、US$0.03。原 15 張分析未彙總費用；中央 T 恤受控重跑實際為 US$0.062224。
- 最終 1,574 項測試、全專案 Ruff／format、JavaScript 語法、sdist／wheel 及專案外 wheel 流程通過；
  8765、8780、18791、18792 四個服務已重新載入新版並通過健康／登入閘門檢查。

[15 張逐件目視報告與 SHA-256](../out/verification/reconstruction/web-blind-direct-review-v2-20261023.json) ·
[固定來源與三份輸入清單](../out/verification/lifestyle/fixtures/web-blind-20261022/) ·
[固定目標映射](../out/verification/lifestyle/direct-review-web-blind-20261022/target-selection.json) ·
[低覆蓋分割回歸](../out/verification/lifestyle/model-web-blind-recovery-studio-20261023/report.json) ·
[生成修正回歸](../out/verification/reconstruction/model-web-blind-recovery-studio-20261023/report.json) ·
[最終交付檢查](../out/verification/web-blind-delivery-20261023.json)。

## 低成本三候選、固定十件 9/10 與免費購物搜尋：2026-10-22

目前出貨路徑一次呼叫 Muse Image 並要求三張完整候選，使用者選圖後保存；不執行額外 Spark 規劃或
模型自評，三張共估算 US$0.03。提示要求保留可見結構，對真正不可見的細節產生保守替代，且至少一張
不新增帽子、口袋、扣件、Logo 或裝飾。畫面始終把候選標成「未逐項比對」，與原圖並排。

- 相同的兩份五件清單在生成前已凍結；每件一次請求、不重試，十件全部留在分母。
- 兩批各 5 次請求、15 張圖、US$0.15；合計 10 次、30 張、US$0.30。完整單件衣物 10/10。
- 根代理逐張看來源上下文、可見裁片及三候選，依事前規則為 9 pass／1 fail／0 uncertain，
  即該固定便利測試最佳三選一可見錨點 90%。唯一失敗的黑色棒球外套仍重寫胸前字樣。
- 這不是一般生活照的母體準確率、隱藏細節真值或同一實體商品證明；沒有用程式測試或模型自評計入分子。
- 零模型費購物區會依原圖可見款號／品牌、人工確認品牌及衣物屬性開啟 Google Shopping、Bing Shopping
  和一般搜尋。AI 品牌／商品頁查找為主動選用；外部連結仍標示未核對。
- 真實 API／SQLite 瀏覽器驗證已確認三候選、選圖保存、卡片更新、免費搜尋及 390px 無橫向溢位；
  模型只在固定十件批次呼叫，瀏覽器流程使用明確本機圖片 fixture。
- 最終原始碼 **1,574 項程式測試通過**；隔離 wheel 與部署 context 通過。以固定新 Python 基底建置的
  Linux arm64 映像完成 **140 項** HTTPS、登入隔離、容量、重啟、備份還原及本輪成本／介面契約檢查。
- 四個既有服務在確認沒有進行中工作後建立離線備份並更新；目前 8765、8780、18792 與登入預覽 18791
  均載入新版。重啟前後資料表內容及媒體雜湊一致，8765 的 GET-only 桌機／手機檢查亦未改動 6 工作、14 衣物。

[目視報告與雜湊](../out/verification/reconstruction/preview-three-choice-v2-direct-review-20261022.json) ·
[第一批](../out/verification/reconstruction/model-preview-three-choice-v2-batch-1-20261022/report.json) ·
[第二批](../out/verification/reconstruction/model-preview-three-choice-v2-batch-2-20261022/report.json) ·
[瀏覽器端到端報告](../out/verification/affordable-preview-browser-release-20261022/report.json) ·
[首次使用與費用顯示](../out/verification/onboarding-cost-browser-final-20261022/report.json) ·
[實際服務桌機／手機](../out/verification/production-release-browser-affordable-20261022/report.json) ·
[服務備份與重啟](../out/verification/affordable-release-service-refresh-20261022/report.json) ·
[最終容器](../out/verification/container-affordable-delivery-20261022/report.json)。

同款自動核驗的既有真實基準仍為 0/3；免費搜尋解決每次查找都花模型費的問題，沒有把未核對連結改算成成功。

## 費用調整：購買搜尋改為自行勾選，2026-10-22

生活穿搭匯入仍預設生成完整衣物預覽；購買搜尋改為預設關閉，可勾選整批查找或稍後逐件查找。
API 原本的 `discover=false` 省略值沒有變更。這只減少未主動要求的品牌／商品搜尋與核對呼叫，
沒有量測新的單件成本，也沒有更改生成品質判準。

- 相關測試 **21 passed**；預覽、原圖搜尋介面及購物流程共 **25 個 fixture 情境通過**。
- 本機 8765 在 1440／390px 實際瀏覽驗證：生成預設開啟、搜尋預設關閉；原有資料前後相同。
- JS 語法及修改過的驗證腳本 Ruff check／format 通過；本輪沒有付費模型呼叫。
- 原生套件與容器尚未重建；下節套件驗證屬於這次預設調整之前的版本。

[本次證據與報告](../out/verification/shopping-opt-in-20261022-165151/summary.json)。

## 主照前襟狀態與判定一致性：2026-10-22

當前程式證據在 `out/verification/primary-state-20261022/`。規劃另外保存主照的前襟展示狀態、
來源與觀察範圍，候選由同一次既有全圖核對獨立觀察；兩者明確不符時，由程式保留失敗及修正目標。
補照／refinement 不能改寫主照狀態；缺漏／未知不能判一致；選圖獨立比較前襟狀態避免新退步。
已有其他失敗時仍保留前襟差異理由。舊版雙欄位皆缺的報告保持原判準，沒有回寫歷史資料。

- 全部程式測試 **1,560 passed / 68.94 秒**；Ruff check／format（99 檔）、兩份 JS 語法檢查通過。
- 新前端契約與既有相關契約共 **132 項通過**；新增 **14 個**桌機／手機 fixture 情境通過，
  原預覽 **6 個**、多原照 **11 個**亦通過。這些均不計作模型品質成功。
- 獨立程式核覆 **107/107**；修正單邊欄位缺漏的 legacy 繞過及既有失敗掩蓋選圖退步兩個實際反例。
- 單次真實回歸複製已保存的三張襯衫原照、七筆辨識紀錄與人工確認群組，沒有重新分析或換圖。
  發出一次重建請求，只生成一張新候選；主代理與獨立目視皆判前襟仍未保留敞開程度，整件失敗。
  程序卻把來源與候選皆觀察成 `open`，因此新比較邏輯未觸發；`needs_review` 仍來自下擺不清楚。
  **本輪沒有證實還原成功率改善，也沒有抓到這個實圖差異；不能將邏輯測試通過當成模型核對正確。**
- 真實保存結果以臨時 Uvicorn 及 Chromium 在 1440／390px 讀取：兩張比較圖正常，無溢出／瀏覽器錯誤，
  GET-only 操作前後資料相同，臨時服務已關閉。新型別報告及所有媒體完成離線備份還原，資料列及雜湊一致。
- 新 wheel 在專案外流程通過，37 個套件檔與來源及 Linux arm64 容器一致；HTTPS 容器 **135/135** 檢查通過，
  使用測試 CA 及離線示範，沒有公開 DNS／ACME 或真實付費模型呼叫。
- 本機 8765／8780／18792 已更新，登入預覽改為 18791（原 8791 在備份空檔被另一工作占用，未干預該服務）。
  四組服務資料列與原圖逐項不變，所有更新前備份保留。

[當前交付證據](../out/verification/primary-state-20261022/delivery.json) ·
[實圖獨立審查](../out/verification/primary-state-20261022/live-run-01/independent-review.json) ·
[程式獨立審查](../out/verification/primary-state-20261022/review/final-review.json)。
90% 保真、自動核驗同款購買、公開部署仍未達成；沒有降低門檻或改寫既有十件評分。

## 額外兩件原照對照與實際失敗：2026-10-22

證據在 `out/verification/multiview-heldout-20261022/`。事前選定兩組 H&M 官方穿搭原照，
每件做一次單照與一次三照生成，共四次呼叫；品牌名稱、款號與商品描述沒有送入模型。
這是生成端隔離控制，沒有經辨識／規劃／品質檢查，不計入產品完整流程成功率。

- 碎花洋裝的兩個條件皆保留五項可見錨點，兩位目視一致；只證明這件沒有退步。
- 格紋襯衫的兩個條件皆沒有保留主照敞開前襟。多照版另把牛仔褲的兩條藍抽繩接到襯衫下方。
  兩位目視一致判失敗；所有候選都保留，沒有只挑成功圖。
- 之後用同三張襯衫原照跑一次真實產品流程：三次上傳得到七筆紀錄，人工確認同件、
  修正局部照分類並合併，再發出一次重建請求。生成圖沒有內搭、牛仔褲或抽繩，仍把前襟靠攏。
  兩位目視皆判整件失敗；下擺錨點的 pass／uncertain 分歧照實保留。
- 程序判定為 `needs_review`，沒有誤列嚴格通過；但 `closures` 判 pass、預覽差異警告為空，
  只因下擺 ROI 不清楚而需核對，確實漏報前襟狀態變化。此結果作為後續修正的失敗基準。
- 成果頁在 1440／390px 各十張圖解碼、兩個展開區及三份 JSON 讀取均正常；
  僅本機 allowlist 檔案可讀、無瀏覽器錯誤或橫向溢出，臨時伺服器已關閉。

[全部對照](../out/verification/multiview-heldout-20261022/index.html) ·
[獨立目視](../out/verification/multiview-heldout-20261022/independent-review.json) ·
[真實流程獨立目視](../out/verification/multiview-heldout-20261022/live-run-01/independent-review.json) ·
[彙整與雜湊](../out/verification/multiview-heldout-20261022/summary.json)。
兩件衣物、四個條件不能當成四件獨立樣本，也不與原固定十件或先前 Nike 單件對照混算。
目前證據沒有支持多原照普遍提高保真，更沒有證明 90% 或自動同款購買已達成。

## 同件衣物多原照與來源履歷：2026-10-22

最新程式證據在 `out/verification/multiview-20261022/`。確認同件群組後，可選最多兩張其他原照，
由目前主照固定展示面；型別化來源、圖片雜湊、群組變更與跨工作取消均已整合。
各張已採用圖、完整預覽及未採用候選保存自己的來源；刪除歷史來源後仍能備份還原完成圖，
現存工作缺檔、輸出缺檔或不合法來源則仍拒絕。舊單照請求與自動重建保持相容。

- 全部程式測試 **1,459 passed / 65.29 秒**：
  [pytest-final.log](../out/verification/multiview-20261022/pytest-final.log)。
  Ruff check／format（96 檔）及兩份前端 JS 語法檢查通過。
- 新多原照 UI **11/11**，預覽 **6/6**、網站帳號 **14/14**、生活照 **8/8**：
  [前端驗證](../out/verification/multiview-ui-20261022/verification.json)。
  39 個情境使用明確 fixture，涵蓋選取上限、鎖定、取消、過期選取、登入續作／換帳號清除、
  逐圖來源與刪除來源後不請求失效圖片；不代表模型準確率。
- 單件四條件事前控制：正面單照徽記失真，增加原始近照後保留雙橢圓／小勾；
  背面單照及多照均保留素面。兩位獨立目視一致：
  [協定](../out/verification/multiview-20261022/protocol.json)、
  [主代理](../out/verification/multiview-20261022/root-review.json)、
  [獨立目視](../out/verification/multiview-20261022/independent-review.json)。
  這是 **1 件衣服、2 個展示面、4 條件**，每條件一次，不是四件獨立樣本或重複成功率。
- 真實產品流程另上傳三張原照，實際分析產生五筆紀錄；人工確認三筆、修正局部照類別並合併，
  然後一次生成。正面五項適用錨點經兩位目視通過：
  [流程](../out/verification/multiview-20261022/live-run-01/report.json)、
  [獨立目視](../out/verification/multiview-20261022/live-run-01/independent-review.json)。
  API 流程使用隔離 TestClient 與真實 Meta 服務；不是全自動合併，也不是透過瀏覽器送出上傳。
  自動核對因下擺 ROI 未包含擺邊，仍為 `needs_review`；沒有放寬門檻或手改為已比對。
- 上述真實資料另由 Uvicorn 提供，桌機 1440px／手機 390px GET-only 瀏覽器通過：
  [report.json](../out/verification/multiview-20261022/live-run-01/browser-run-03/report.json)。
  原圖／生成圖與三張來源全部可解碼、無溢出、無頁面錯誤、沒有任何變更請求或新模型呼叫。
  前兩次測試 fixture 分別誤用群組 endpoint，以及比較每次不同的匯出時間戳；修正測試後重跑，
  失敗紀錄保留，產品資料沒有更改。現在只排除 `exported_at`，保留所有 items/groups 比較。
- 新 wheel 在專案外完成本機、網站登入／隔離、媒體、匯出及備份還原；多原照型別與前端包含於包內：
  [wheel-smoke.json](../out/verification/multiview-20261022/wheel-smoke.json)。
  wheel 與當前程式 37 檔逐一一致，Linux arm64 映像內 37 檔也與 wheel 一致：
  [package-parity.json](../out/verification/multiview-20261022/package-parity.json)。
- 新映像 `rewear-studio:multiview-20261022` 的實際 HTTPS、Secure cookie、帳號隔離、重啟、
  備份還原及非 root／唯讀根目錄檢查 **135/135** 通過：
  [container-smoke/report.json](../out/verification/multiview-20261022/container-smoke/report.json)。
  僅離線示範，沒有模型呼叫、公開 DNS／ACME 或一般生活照效果宣稱；測試資源已清理。
- 四組本機資料先停止服務、建立 0600 離線備份，才重啟新版。
  真實多原照資料 **27 檔**還原後全部資料列與媒體雜湊一致：
  [backups.json](../out/verification/multiview-20261022/local-restart/backups.json)。
  8765 原衣櫃、8780 舊流程、8791 帳號預覽、18792 多原照實測均正常，重啟前後所有資料表及原始檔案不變：
  [after.json](../out/verification/multiview-20261022/local-restart/after.json)。
  帳號預覽仍無使用者帳號，未代替使用者註冊或設定密碼。

這次可選的多原照改善有單例支持，**仍未證明 90% 保真或自動同款購買成功**。
原固定十件、未知隱藏細節與自動購買零核驗結果保持原記錄。
使用方法及 API 見 [MULTI_PHOTO_RECONSTRUCTION.md](MULTI_PHOTO_RECONSTRUCTION.md)。

## 網站模式、資料可靠性與發布準備：2026-10-22

本輪證據保存於 `out/verification/resumed-fidelity-20261022/`。
新功能包含帳號、一次性復原碼、密碼更換、登入過期後草稿保留、兩帳號資料隔離、
每帳號容量、全域工作上限、刪除媒體失敗重試、離線備份／還原及 HTTPS 容器配置。
這些工程驗收不代表 90% 保真與自動同款購買需求已達成。

- 全部程式測試 **1,331 passed / 103.29 秒**：[pytest-final.log](../out/verification/resumed-fidelity-20261022/pytest-final.log)。
- 網站 UI **14/14**：[report.json](../out/verification/hosted-ui-20261022/storage-browser-frozen/report.json)；
  受影響本機 UI **39/39**：[verification.json](../out/verification/hosted-ui-20261022/local-fixtures/verification.json)。
- 真實 Uvicorn／Argon2／SQLite／瀏覽器 **7/7**：
  [report.json](../out/verification/resumed-fidelity-20261022/hosted-backend-browser-quota/report.json)。
  模型使用 fixture；帳號、上傳、圖片、權限、改密碼、復原與下載都使用真實後端。
  第一輪抓到上傳缺少 `X-Rewear-Request`，已修正並納入 fixture 約束；失敗紀錄保留。
- 實際兩帳號資料 **9 檔**離線還原：
  [report.json](../out/verification/resumed-fidelity-20261022/hosted-backup-roundtrip/report.json)。
  還原前逐表一致，還原後兩帳號可登入／讀圖／匯出，跨帳號讀取拒絕；備份權限 0600。
- 專案目錄外的 wheel 本機與網站流程、備份還原、真實密碼登入通過：
  [wheel-smoke-final.json](../out/verification/resumed-fidelity-20261022/wheel-smoke-final.json)。
  新測試最初誤期待註冊回 201；實際 API 契約為 200，已修測試，未變更 API。
- 原資料逐表不變、桌機／手機 14 張圖片可解碼、無溢出與頁面錯誤：
  [重啟比較](../out/verification/resumed-fidelity-20261022/final-local-restart/after.json)、
  [瀏覽器驗證](../out/verification/resumed-fidelity-20261022/production-browser-final/report.json)。
- 獨立發布審查修復兩個實際問題：建置白名單過寬，以及中文網域的 healthcheck Host 未正規化。
  真 BuildKit fixture 證明只送入指定三檔，15 個假私密檔案均排除；ASCII／IDN／預設 443 健康檢查通過：
  [context-after.json](../out/verification/resumed-fidelity-20261022/deployment-review/context-after.json)、
  [healthcheck-after.json](../out/verification/resumed-fidelity-20261022/deployment-review/healthcheck-after.json)。
- 實際 Linux arm64 容器、受信任測試 CA 的 HTTPS、Secure cookie、CSRF、兩帳號媒體／匯出隔離、
  重啟保存及映像內備份／還原通過：
  [container-smoke/run-03/report.json](../out/verification/resumed-fidelity-20261022/container-smoke/run-03/report.json)。
  非 root UID 10001、唯讀根目錄、只有 loopback 隨機連接埠，Caddy 共用 app 網路 namespace。
  只匯入離線示範，模型呼叫 0；還原 11 檔、3,392,076 bytes 後兩帳號可登入且原圖雜湊一致。
  135 個檢查點不是 135 個獨立準確率案例；未測公開 DNS、ACME 或另一 CPU 架構。
  前兩輪 fixture 因漏 Caddy capability 與內部 bridge 無 published port 停止，原始失敗紀錄保留。
  成功輪使用獨立一般 bridge，未宣稱 OS 層封鎖外連；沒有真憑證，模型端點固定至 loopback 9。
  自建測試容器、volume、network 均清理並確認無殘留。
- 帳號版本機預覽已在 `http://127.0.0.1:8791/login` 啟動，使用新的 `data-hosted-preview/`；
  [桌機／手機登入與註冊頁](../out/verification/resumed-fidelity-20261022/hosted-preview/report.json)
  僅做 GET 與分頁操作，未建立使用者帳號。這是 loopback HTTP 開發預覽，並非公開 HTTPS 上線。

沿用原固定十件原圖的一次生成對照，完整圖為 10/10。主代理逐件目視為 **6 pass／3 fail／1 uncertain**，
獨立代理為 **7 pass／1 fail／2 uncertain**。兩者皆未達 9/10；不採用、不混算、不選擇較高數字宣稱普遍成功率。
原圖、請求與判讀證據：[凍結協定](../out/verification/resumed-fidelity-20261022/source-only-ten/protocol.json)、
[主代理判讀](../out/verification/resumed-fidelity-20261022/source-only-ten/root-review.json)、
[獨立判讀](../out/verification/resumed-fidelity-20261022/source-only-ten/independent-review.json)。
自動同款核驗仍沒有成功結果；Google 原圖索引尚缺可用授權設定，沒有重試已知無權限專案。

## 原圖商品索引接入口與保真控制：2026-10-22

本輪程式及試驗證據位於 `out/verification/fidelity-repair-20261022/`；**90% 保真及自動同款購買仍未達成**。

已接入可選的 Google Vision Web Detection：預設關閉，須明確設定專案／憑證後才傳送原始衣物裁圖。
每件最多一次原圖索引及兩次文字搜尋；索引只提供候選網頁，仍須通過既有商品頁、主圖、地區核對。
任一搜尋來源失敗時保留另一來源已取得的結果，取消可停止後續請求，錯誤不包含憑證或上傳內容。
前端分開列出圖片與文字搜尋，並在上傳、單件查找、服務設定三處說明 Google 的圖片傳送。

完整程式回歸 **1,148 項通過（70.97 秒）**，包含 84 項新 adapter 測試及 11 項整合情境；
皆為有界模擬傳輸，不代表真實召回成功。桌機／手機 8 個情境通過；獨立審查另測實際 adapter
取消、ADC token 交換取消及私密例外處理，未發現可重現的產品缺陷。

- [完整回歸](../out/verification/fidelity-repair-20261022/pytest-final.log)、
  [獨立審查](../out/verification/fidelity-repair-20261022/integration-review.json)、
  [桌機／手機驗證](../out/verification/image-search-ui-20261022/verification.json)。

真實 Google Web Detection 先前回覆 `USER_PROJECT_DENIED`，本輪沒有重送該圖片請求。
僅以既有憑證唯讀列出可存取專案：目前 ADC 配額專案不在清單內，沒有自選其他專案或改動 API／計費。
另一公開 CLIP 圖片索引健康檢查在 20.25 秒連線逾時，未上傳圖片；此結果不能推論全球服務失效。
新接入口尚需可用且獲授權的設定，才能測實際商品召回；Google Web Detection 也不等同 Lens。

生成控制沿用凍結原圖及可見特徵，由主代理與另一代理直接目視，沒有以模型自評代替：

| 對照 | 實際結果 | 採用 |
| --- | --- | --- |
| 黑外套：既有修正請求、補差異描述 | 兩張皆仍錯放袖章／胸字形 | 否 |
| 黑外套：原生圖文交錯、以實際生成 handle 續編 | 胸前可見字形及填色改善，兩張袖章仍在相反側 | 否 |
| 黑外套：相同交錯請求僅改單次內部生成模式 | 袖章仍錯側，另混入內搭與項鍊 | 否 |
| 專用 TryOffDiff：全景洋裝 | 粉色碎花短袖變綠色無袖 | 否 |
| 專用 TryOffDiff：相同設定改既有衣物上下文裁圖 | 短袖與粉色恢復，但印花變密、底色比例改變 | 否 |

五張黑外套控制與兩張洋裝控制分開記錄，均未達全部可見保真條件，不改寫原固定十件 **6/10**。
TryOffDiff 僅測作者公開研究 demo，沒有納入正式依賴或下載模型；兩張輸入構圖不同，不能稱單一模型比較。
未觀察到的全字樣、領口、衣長和隱藏構造維持未知。

購物自適應控制實際執行 2 次搜尋、22 筆命中、0 個合格商品。模型提出的第二輪款號沒有首輪來源支持，
執行器卻仍送出，已明列為[試驗協定偏離](../out/verification/fidelity-repair-20261022/adaptive-shopping-control/protocol-deviation.json)；
不算有效的有依據自適應檢索驗證，也未採用到正式流程。

## 搜尋召回與單張原照對照：2026-10-22

本輪證據獨立保存在 `out/verification/quality-retrieval-20261022/`；完整產品目標仍未完成。

### 修正複合衣物類別漏選

已保存的實際搜尋結果中，有兩筆 Adidas 外套商品標題只寫 `jacket`，原照辨識則為
`hooded jacket`。原本補選規則要求整段文字相符，因而把它們丟掉。現在以複合描述最後的
衣物類別比對，讓這類實際檢索到的商品進入後續核驗；`jacket cover` 不當成外套，
不同品牌、錯誤衣物種類、外國指定地區及非商品網址仍有原本限制。

兩筆保存結果的傳輸重播已重現修正前失敗及修正後恢復候選；這不是新搜尋，也不表示
這兩件商品與照片同款。商品頁、主圖、地區及庫存仍須各自核對。
七次保存的跑者搜尋合計 70 筆原始結果均未包含人工 Lens 找到的 HG5861，
所以這次程式修正尚不能宣稱解決同款召回。

- [診斷、實際命中及前後結果](../out/verification/quality-retrieval-20261022/search-research/findings.json)。
- [修正前測試](../out/verification/quality-retrieval-20261022/search-research/before-tests.txt)、
  [修正後 157 項相關測試](../out/verification/quality-retrieval-20261022/search-research/after-tests.txt)。

### 原照提出檢索假說，仍未找到合格商品

另在呼叫前凍結輸入與次數：只給原始裁圖、上下文及可見 Adidas 品牌，允許模型提出最多兩個
未確認的系列／款式搜尋線索。沒有提供人工 Lens 的商品名稱、款號、商家或商品圖，
原始 `product_code` 始終為 null。一次規劃只得到兩個外觀描述，沒有提出系列名稱。
依輸出順序執行兩次搜尋，實際各只有一次 web search；22 筆結果取得四個可追溯候選。

最多三次商品頁讀取及一次原圖／商品主圖比較後，核驗商品仍為 **0**：KICKS CREW 的 GT7084
實頁轉入台灣語系且可取得主圖，但實際是黑紅球隊外套，與原圖的大面積藍黑拼片不同；
主代理和另一代理直接目視都支持排除。Adidas 與 PicClick 限制存取後立即停止，
第二筆同站 Adidas 未再讀取。此方法未接入正式搜尋，不與先前自動三件 0/3 混算。

控制也重現 `/om/en/` 阿曼網址原先被視為地區未知，浪費一次核驗額度。
已補入共用地區判斷；路徑、語系與網域上的明確 Oman 地區在台灣搜尋預先排除，全球模式保留。
離線重播相同 22 筆結果，只有兩個阿曼網址由 unknown 改為 out_of_scope，
25 個新回歸含選中候選與原始結果補選兩條路徑；331 項相關測試通過，沒有追加網路或模型請求。
這修正地區過濾，並沒有找到缺少的同款。

另獨立檢查一筆被丟棄的 Lyst 結果：搜尋索引仍呈現 GM4430 單品，當下實際 HTTP 網址已轉向
49 件商品的分類頁。第一次商品讀取遭正確排除；第二次僅保存實際重新導向與 JSON-LD 診斷，
沒有新的搜尋、模型或圖片比較。確認不是商品選擇器誤判，因此未放寬單品證據規則。
這是控制結束後的兩次獨立網頁讀取，不隱藏在原三次商品頁額度內。

- [事前凍結試驗](../out/verification/quality-retrieval-20261022/search-research/hypothesis-control/protocol.json)、
  [所有實際呼叫與結果](../out/verification/quality-retrieval-20261022/search-research/hypothesis-control/report.json)、
  [獨立判讀](../out/verification/quality-retrieval-20261022/search-research/hypothesis-control/independent-result.json)。
- [阿曼地區修正及離線重播](../out/verification/quality-retrieval-20261022/search-research/locale-fix/report.json)。
- [丟棄結果首次核對](../out/verification/quality-retrieval-20261022/search-research/discarded-hit-audit/result.json)、
  [實際轉址與分類頁結構](../out/verification/quality-retrieval-20261022/search-research/discarded-hit-audit/schema-diagnosis.json)。

### 單張原照生成仍改變袖口

針對碎花洋裝，固定同一原始上下文照片，改為只給一張原照及簡短通用提示，
移除分割裁片、先前候選及款式文字規劃；一次生成用時 17.34 秒，沒有重試或模型自評。
雖然得到完整洋裝，主代理與另一代理獨立目視都確認：較平直的可見袖口變成收緊皺褶及荷葉邊。
可見款式為 **fail**，此方法未採用，沒有追加同一分支的生成。
領口、完整衣長與下襬原本無法觀察，仍列為未知，不把補畫當成真實細節。

此控制同時改變參考圖組合與提示，不能證明單一根因，也不能證明任何模型普遍無法完成；
它只否定此配置在這個已知失敗案例已修好的說法，不與原十件基準混算。

- [凍結輸入與次數](../out/verification/quality-retrieval-20261022/generation-research/manifest.json)、
  [實際候選](../out/verification/quality-retrieval-20261022/generation-research/candidate.png)。
- [主代理直接目視](../out/verification/quality-retrieval-20261022/generation-research/root-review.json)、
  [獨立原圖先行核對](../out/verification/quality-retrieval-20261022/visual-review/candidate-review.json)。

### 已採用修正的回歸與資料保存

包含阿曼地區修正的完整程式回歸 **1,053 項通過**，37.62 秒；Ruff 與格式檢查通過。
較早的 1,028 項回歸日誌另行保留，沒有覆寫。
正式 8765 與保存真實流程的 8780 都先檢查沒有執行中工作、完成 SQLite 備份，再載入修正。
六張資料表逐列與重啟前相同，API 內容亦相同：正式資料仍為 6 工作、14 衣物、15 稽核，
隔離真實流程仍為 1 工作、2 衣物、1 批次、16 稽核。重啟沒有觸發模型或搜尋請求。

- [完整 pytest](../out/verification/quality-retrieval-20261022/pytest-final.log)、
  [JUnit](../out/verification/quality-retrieval-20261022/pytest-final.xml)。
- [重啟前備份紀錄](../out/verification/quality-retrieval-20261022/runtime-pre-release.json)、
  [含兩項修正的最終重啟逐列核對](../out/verification/quality-retrieval-20261022/runtime-final.json)。

最終正式服務再通過桌機 1440px／手機 390px 唯讀檢查：14 張圖片可解碼、原圖／上下文／分割切換
正常，沒有橫向溢出、JavaScript／console 錯誤、外部請求或寫入；主代理直接檢視畫面。
wheel 於工作目錄以外載入，離線防呆、範例、媒體、API 與匯出皆通過，沒有模型呼叫。
交付包再次核對 28 個執行檔與已驗證 wheel、工作目錄完全相同；文件納入 source distribution。

- [最終桌機／手機報告](../out/verification/quality-retrieval-20261022/production-browser-final/report.json)。
- [獨立 wheel 驗證](../out/verification/quality-retrieval-20261022/wheel-smoke.json)、
  [交付包雜湊與來源比對](../out/verification/quality-retrieval-20261022/package-final.json)。

本輪交付紀錄為 [delivery.json](../out/verification/quality-retrieval-20261022/delivery.json)。
這輪完成兩項實際缺陷修正並排除無效的生成／檢索方法；90% 保真及自動同款購買仍未達成，
不將程式測試通過或人工檢索候選改算成完整產品驗收成功。

## 可用產品驗收續作：2026-10-22

目標仍為完整可用產品，包含至少 90% 的款式保留與有依據的購買結果；本輪沒有縮減需求。
驗收清單在 [PRODUCT_READINESS.md](PRODUCT_READINESS.md)。以下試驗與前一輪十件基準分開保存。

### 可用模型及失敗重試

實際環境的本機 Ollama 未連線，但 Meta 已設定。介面現在阻止將工作送給不可用的本機模型，
保留原選擇，讓使用者明確切換 Meta。重新檢查連線會要求新健康狀態，舊回應不能覆蓋新選擇。
後端在保存工作與開始付費分割／生成之前同樣檢查，失敗不建立工作或媒體目錄。
已受理的同一批次仍可冪等重送；已保存的生成圖可用本機模型重試，不必再次提供 Meta 金鑰。
健康檢查的五秒期限涵蓋鎖等待；重試等待健康狀態時若原工作已刪除，回 404，不重建已刪資料。

- [真實離線服務驗證](../out/verification/product-ready-20261022/readiness-real-offline.json)。
- [17 項新就緒測試與既有流程回歸，共 83 項通過](../out/verification/product-ready-20261022/readiness-race-tests.log)。
- [首次使用的桌機／手機 10 項檢查](../out/verification/product-ready-20261022/onboarding/release/report.json)。

### 從瀏覽器開始的真實完整流程

事前固定一張公開授權的藍色帽衫生活照，只提交一次，使用隔離資料庫及真實 SAM／Spark／Muse Image。
20.2 秒完成兩件可見衣物分析，296.0 秒後生成與購買查找全部到達終止狀態。
桌機、390px 手機、原圖切換與 JSON／CSV 下載皆通過，來源 SHA-256 前後一致。
這是一項流程控制，不是新準確率基準；僅有一張照片，不能用來估計成功率。

兩件都有完整預覽，但外套嚴格核對為 rejected，內搭為 needs_review。
直接目視確認外套袖口從原圖花紗藍色變為深藏青素色；白內搭的可見白色、素面與前領口保留，
雙袖、下擺與完整衣長仍屬推測。自動報告也有錯誤：它聲稱外套抽繩消失，但選中的候選實際有兩條抽繩。
因此不能以模型警告或模型通過替代原圖對照。
兩件各執行兩次真實商品搜尋，合計四次；核驗商品 **0**、確認同款 **0**。

- [事前固定素材、參數與程式版本](../out/verification/product-ready-20261022/flow-manifest.json)。
- [唯一上傳、逐步狀態與瀏覽器證據](../out/verification/product-ready-20261022/flow-browser/report.json)。
- [逐件直接目視紀錄](../out/verification/product-ready-20261022/flow-browser/root-visual-review.json)。

此真實流程也找到介面缺口：分析完成後，歷史頁原本沒有顯示仍在生成或搜尋的工作。
修正後，分析狀態保留原意，另外顯示各件的排隊／執行／完成／失敗與先前保留結果，
並可直接查看該次衣物與生成進度。使用此次保存的執行中及終態 API 進行固定重播，
桌機／手機七項情境及八項契約測試通過，沒有重新提交圖片、生成或搜尋。
根代理直接檢視桌機執行中畫面與手機終態畫面，確認「含推測」及「尚無通過核對商品」可見。
新證據在 [flow-after-ui/verification.json](../out/verification/product-ready-20261022/flow-after-ui/verification.json)，
原版真實流程報告與當時的前端檔案保持不變。

### 圖片及搜尋方法的額外對照

另外將三張固定失敗原照作為主要生成參考，移除自由文字款式規劃，保留原圖及真實局部裁片。
三次生成都返回圖片，但兩個代理各自目視結果均為 **0 pass、1 fail、2 uncertain**：
碎花洋裝仍新增抽褶袖口，格紋大衣與黑外套的細節不能可靠確認。這個方法未採用到正式程式。
官方 Muse Image 文件亦不支援 `mask` 或 `input_fidelity`，不能靠加入未支援欄位宣稱能保留原像素。

購買搜尋另做三次短查詢／原圖加文字搜尋診斷，36 筆原始搜尋結果全部缺少摘要，
沒有明確台灣網址提示；未產生由模型選出且網址可追溯的合格商品。
此診斷沒有執行正式流程的原始搜尋結果補選，不能把它當成完整 production 重跑的零候選數。
原圖加文字搜尋也不是反向圖片索引，沒有證據支持替換正式策略。

獨立人工核對的一件 ZARA 台灣白 T 實際只有幼童尺寸，已排除，沒有算成成人近似款。
其 HTTP 頁面是狀態 200 的 Akamai 驗證頁；修正後在保存的相同 HTML 上回傳 blocked，
不執行腳本、不跟隨驗證重新導向，既有流程會跳過同站後續網址。
只在真實 refresh 的 `bm-verify` 查詢鍵及精確 interstitial iframe 同時存在時觸發新增規則，
一般分析腳本不誤擋；149 項頁面證據測試與兩項同站停止回歸通過。

Google Cloud Vision 原圖索引只以既有 ADC 執行一次 WEB_DETECTION，實際回
`403 / PERMISSION_DENIED / USER_PROJECT_DENIED`。憑證更新成功，但配額專案沒有此請求權限；
未改帳號、專案、API 或帳單設定。此服務的召回效果尚未驗證。

- [生成試驗及主代理判定](../out/verification/product-ready-20261022/reconstruction/root-review-and-decision.json)、
  [第二代理獨立目視](../out/verification/product-ready-20261022/reconstruction/independent-review.json)。
- [生成 API 官方能力核對](../out/verification/product-ready-20261022/reconstruction/api-research.json)。
- [搜尋實驗、幼童反例與權限失敗](../out/verification/product-ready-20261022/shopping/conclusions.json)。

### 官方 Google Lens 的單次人工原圖檢索

在既有真實 Chrome 中，透過官方上傳介面送入一次公開授權跑者原照的 SAM 裁圖，
沒有使用 AI 重建圖、改查詢文字或呼叫未記錄的內部 API。這次得到具體的
**adidas Premium Designed for Gameday HG5861** 候選，明顯比先前文字搜尋更相關。
款號來自搜尋結果及商品頁，原照標籤仍不可讀，不能當成已確認同款。

最多檢查三個來源：台灣蝦皮先顯示黑藍 HG5861、S–2XL 及新北出貨，但隨後要求驗證／登入，
立即停止，沒有確認目前庫存；[JapanSport 越南商品頁](https://giaynhatchinhhang.vn/adidas-premium-designed-for-gameday-hg5861)
顯示 JP-XL、1,080,000 VND 及少量庫存宣告，未確認台灣配送。
下載的實際主圖與原照有相同藍黑拼片、袖部標誌、藍色袖線及拉片／淺色小標位置，
但藍色色調不同且原照部分遮擋，兩位代理僅判為高度相近的候選。
第三個官方美國 IC8046 頁面是不同配色／飾線且已售完，排除。

這是單張人工檢索的可行性證據，不是正式自動搜尋成功，也不改寫原三件 0/3 的結果。
官方 Web Detection 是另一項 API，尚無證據能重現 Lens 的結果；官方 Product Search
則需要自行建立商品集，不能當成開放網路商品索引。

- [完整檢索紀錄、三個實際來源網址與官方文件](../out/verification/product-ready-20261022/shopping/lens-control/report.json)。
- [主代理目視核對](../out/verification/product-ready-20261022/shopping/lens-control/root-visual-review.json)。

依此結果，介面加入明確的手動後備入口：沒有合格商品時可下載原始辨識區域／上下文，再另開
官方 Google Lens；沒有自動上傳、沒有用 AI 圖作為來源。下載僅允許同工作且符合資格的原圖，
拒絕生成來源、跨工作／外部／編碼路徑、購買商品圖及 AI 預覽。
兩個實際下載分別為 91,157 bytes 與 136,219 bytes，均與來源檔案 SHA-256 相同。
另將歷史摘要區分「已知差異」及「待核對」，使用與顯示圖片綁定的驗證報告。
此輪 31 項契約測試、六項手動入口及七項進度瀏覽器情境通過；桌機與手機皆目視，沒有模型請求或外部傳圖。
證據：[shopping-image-handoff/verification.json](../out/verification/product-ready-20261022/shopping-image-handoff/verification.json)。

獨立覆核另重現「衣物載入成功、來源工作清單讀取失敗」時，生成圖可能被誤供為原圖的缺陷。
已修正為必須找到同一來源工作，且明確為圖片／影片，才提供入口；恢復載入後同一詳情頁即時更新。
新增缺來源及錯來源正反控制，33 項契約與八項瀏覽器情境通過；一筆刻意模擬的 503 單列為預期錯誤。
第二代理以原失敗 payload 重測已修復，47 項相關測試通過。
證據：[來源防護驗證](../out/verification/product-ready-20261022/shopping-image-handoff/job-source-guard/verification.json)、
[獨立覆核](../out/verification/product-ready-20261022/shopping-image-handoff/independent-code-review-final.json)。

### 未採用的局部核對與另一生成工具

帽衫自動報告的「抽繩缺失」已追溯到同一候選的局部判讀，不是未選候選的報告混入。
局部裁片沒有涵蓋向兩側攤平的抽繩，模型卻據此宣告全件缺件；實際選中圖有兩條抽繩。
對既有圖片進行兩次有界控制，沒有重新生成：完整原圖＋完整候選的控制執行兩次 Spark，
約 48.92 秒；固定相同裁片、只加完整候選供定位的控制執行一次，約 25.70 秒。
兩次都找回抽繩，但都把真正袖口色差放行，因此未採用，既有嚴格判定與報告保持不變。
後一次原始回答甚至將色差歸因於沒有證據的曝光差異，不能當成已修好或品質通過。

另使用內建 imagegen 工具，以相同五張參考圖及相同提示，對碎花洋裝只生成一次。
結果再次新增荷葉袖口，並畫出原圖無法支持的下襬開口，目視失敗，未接入正式應用。
工具沒有公開模型識別碼，不能把技能中的 CLI 預設模型名稱套用到此工具；
不同工具的參數也不相同，這不是隔離單一變因的模型比較，更不是 90% 成功率證據。

- [完整上下文核對](../out/verification/product-ready-20261022/reconstruction/regional-context-control/independent-review.json)。
- [固定裁片、候選定位控制](../out/verification/product-ready-20261022/reconstruction/regional-context-control/candidate-only-control/independent-review.json)。
- [內建工具單次生成、原圖與提示雜湊](../out/verification/product-ready-20261022/reconstruction/builtin-image-control/report.json)，
  [生成圖](../out/verification/product-ready-20261022/reconstruction/builtin-image-control/candidate.png)、
  [完整提示](../out/verification/product-ready-20261022/reconstruction/builtin-image-control/prompt.txt)。

### 本輪最終程式回歸

所有已採用修改的完整回歸 **1014 項通過**，用時 38.03 秒；Ruff、格式與 JavaScript 語法檢查通過。
989 項的較早日誌保留，未用最後結果覆寫中間驗證。模型實驗失敗、人工 Lens 檢索與程式測試各自記錄。
證據：[完整 pytest](../out/verification/product-ready-20261022/pytest-final.log)、
[JUnit](../out/verification/product-ready-20261022/pytest-final.xml)。

最終 wheel 與 source distribution 建置成功；wheel 解到獨立暫存目錄，確認匯入的不是工作目錄程式。
已檢查套件內的新介面、重新讀取模型健康狀態、離線上傳回 503 且不建立資料、範例／媒體、
取消、JSON／CSV 匯出及 OpenAPI；沒有模型呼叫。證據在
[build-final.log](../out/verification/product-ready-20261022/build-final.log) 與
[wheel-final.json](../out/verification/product-ready-20261022/wheel-final.json)。

正式服務 8765 在確認沒有執行中工作並完成 SQLite 備份後重新啟動。
原始資料六個資料表逐列與開工前一致，仍為 6 工作、14 衣物、15 稽核。
真實桌機 1440px 與手機 390px 的唯讀瀏覽器檢查均通過，14 張衣物圖均能解碼，
原圖／上下文／分割切換與穿搭模式預設生成、搜尋、TW 地區正確；沒有橫向溢出、
JavaScript／console 錯誤、外部請求或資料寫入。
見 [正式服務瀏覽器報告](../out/verification/product-ready-20261022/production-ui-final/report.json)。

保存真實雙衣物流程的 8780 亦在備份後更新到相同版本，1 工作、2 衣物、1 批次及 16 稽核逐列不變，
沒有重做上傳或模型呼叫。更新版本及媒體雜湊在
[flow-runtime-release.json](../out/verification/product-ready-20261022/flow-runtime-release.json)。
更新後再直接操作真實服務的兩件結果，桌機／手機四個情境通過：完整預覽與原照可切換、
已知色差仍明示、原圖搜尋入口可見，手機下載的兩張來源圖逐位元與服務檔案相同；
沒有外部導覽、API 寫入或資料變更。根代理直接檢視正式首頁、手機原圖／預覽對照及購買面板。
報告：[flow-ui-release/report.json](../out/verification/product-ready-20261022/flow-ui-release/report.json)。
先前 `flow-ui-final/` 的測試錯把所有預覽說明都限定為「推測」字樣，遇到已知差異說明而失敗；
原報告保留，測試改核對共同的 AI 補畫／不可確認同款說明，並另外要求外套顯示已知差異。
這次只修正驗證腳本，沒有更改正式程式或遮蔽已知失敗。

Lens 之後另以候選款號做六次文字查詢，未找到新增可核實台灣現貨的頁面。
馬來西亞 Sport Planet 的 HG5861 商品頁標示 Sold Out，排除；相同編號的咖啡密封罐亦排除。
此為人工補查，不改寫先前限定三個商品頁的 Lens 實驗，也不計入應用自動搜尋成功率。
記錄：[款號補查](../out/verification/product-ready-20261022/shopping/hg5861-followup.json)。

最後只因驗證文件更新重建 `dist-delivery/` 發行包；所有 `wardrobe/` 套件檔案與先前通過的
wheel、目前原始碼及實際服務一致。當前成果、剩餘缺口、測試與檔案雜湊集中在
[delivery.json](../out/verification/product-ready-20261022/delivery.json)。

## 完整預覽與購買查找：2026-10-22

此輪針對側拍、紮衣及一般遮擋不出圖，以及上傳後沒有自動查找商品，補上完整預覽與自動搜尋。
「完整預覽」和「可見款式忠實度」各自驗證。預覽允許補足未知的衣長及遮擋部位，但必須列明推測；
不明確的隱藏部位仍不能成為完整同款核對通過的證據。原照片、欄位、分割及人工確認不改寫。

### 固定十件的第一輪結果

兩批均透過隔離的正式重建 API 呼叫真實模型，沿用事前固定的原始觀察與 SHA-256。
完整預覽 **10/10**；模型嚴格核對 **0/10 accepted**（9 needs_review、1 rejected）。
代理逐張看原圖及候選後，可見款式目視 **6 pass、3 fail、1 uncertain**。
因此尚未達使用者期望的 90% 款式還原率。這些開發案例是便利取樣，也不能代表一般生活照準確率。

三件確定失真為：正面跑者的淺色側標變成帶白字的藍標、黑外套衣身下襬的兩條白線消失，
以及商務大衣的翻領與格紋改動。碎花洋裝的花朵尺度／密度尚不能確認一致。
其餘六件的目視通過只涵蓋可見特徵，不是對未知領口、下襬或背面結構的保證。
黑外套與跑者的差異沒有被本輪模型差異警告捕捉，這項漏檢也保留在紀錄中。

- [事前固定十件與條件](../out/verification/preview-shopping-20261022/preview-ten-manifest.json)。
- [第一批實際結果](../out/verification/preview-shopping-20261022/model-batch-1/report.json)、
  [第二批實際結果](../out/verification/preview-shopping-20261022/model-batch-2/report.json)。
- [獨立目視逐件判定](../out/verification/preview-shopping-20261022/independent-visual-review.json)：
  檢查者也是實作代理，並非外部盲測，已披露限制。
- [原圖／生成／購買結果對照頁](../out/verification/preview-shopping-20261022/index.html)，
  本機網址 `http://127.0.0.1:8777/`；實測衣物室 `http://127.0.0.1:8776/`。

### 原圖細節補強後的四件重測

首輪三件失敗及一件未確定全部追加重測，沿用相同原始觀察；沒有挑選新的容易照片。
生成前增加原圖上、中、下的重疊局部參考，細節複核可補入兩處漏掉的區域，最終最多五處。
四件都能產生完整預覽，但獨立目視只有 **1/4 符合**：跑者的淺色小標與藍色拉片恢復為兩個部件。
商務大衣恢復翻領，但格紋仍變成裝飾性大花紋；黑外套恢復下襬條紋，但胸前字樣與袖章位置改變；
碎花洋裝反而新增收縮荷葉袖口。後三件均保留為失敗。

這是針對失敗案例的追加測試，不能與首輪混算成單次成功率，也沒有證明最終版本達到 90%。
模型細節判讀有誤讀袖口及局部座標指錯位置的情況；「完整預覽通過」仍不是可見款式忠實的證明。

- [四件事前重測清單](../out/verification/preview-shopping-20261022/detail-repair-manifest.json)。
- [重測第一批](../out/verification/preview-shopping-20261022/detail-repair-1/report.json)、
  [重測第二批](../out/verification/preview-shopping-20261022/detail-repair-2/report.json)。
- [獨立目視結果](../out/verification/preview-shopping-20261022/detail-independent-review.json)、
  [來源判讀與局部座標根因](../out/verification/preview-shopping-20261022/detail-root-cause.json)。

### 較強判讀模型的隔離試驗

另外以 Muse Wonder 1.0 檢查同一件碎花洋裝與黑外套，沒有改動正式模型或生成新圖。
原本陣列格式會出現重複特徵，較高推理設定的兩案均達 120 秒時限；medium 初步規劃仍有一案格式失敗。
隔離試驗改用固定特徵欄位的傳輸格式，驗證後轉回現有資料格式，兩案都在首次請求完成，
分別花 45.76 秒及 64.90 秒。這是格式可靠性改善，並非還原準確率。

後續獨立原圖細節複核中，黑外套的袖章區域定位改善；碎花洋裝第一次回應耗盡 6144 tokens，
另以 8192 token 上限重試後有完整回應，卻再次將普通袖口描述成明顯抽褶荷葉邊。
因此未採用模型切換，也沒有將這些來源判讀當作新的生成成功案例。
這些分階段診斷使用保存的初步規劃，耗時逐段記錄，並非正式端到端效能測試。

- [初始診斷](../out/verification/preview-shopping-20261022/wonder-plan-medium/)、
  [固定欄位規劃](../out/verification/preview-shopping-20261022/wonder-plan-fixed/)。
- [原圖細節複核](../out/verification/preview-shopping-20261022/wonder-source-fixed/)、
  [洋裝較高輸出上限重試](../out/verification/preview-shopping-20261022/wonder-source-fixed-retry/)。
- [隔離格式驗證](../out/verification/preview-shopping-20261022/wonder-fixed-features-offline.json)。

### 實際購買搜尋

生活穿搭上傳預設勾選查找購買選項，可選台灣或不限地區；API 的省略值維持關閉以相容既有呼叫。
單件完成後獨立排隊，以原圖尋找品牌線索並執行最多兩次搜尋；AI 重建圖不作商品身分證據。
未核對的候選、查詢詞及原因在無合格商品時直接展開，不把空結果藏成「已完成」的成功購買結果。

三張公開原照（側拍白 T、藍黑 adidas 外套、Nike 白 T）的兩次查詢均真實執行，共六次網頁搜尋。
通過商品核對 **0/3**、同款 **0**：候選有存取限制、缺主圖、缺地區證據或非單一商品頁等問題。
另三次官方網域查詢實驗也沒有改善，沒有採用；早期不支援 `tool_choice=required` 的 HTTP 400
診斷與原報告保留，正式請求使用服務支援的 `auto`。

人工另外在 Chrome 開啟 UNIQLO 台灣商品頁並選擇白色，核對到產品 483456、頁面價 NT$390、
顏色 00 WHITE 顯示庫存充足（未選特定尺寸）。這是白色素面圓領短袖近似款，版型／袖長有差異，
原照無法確認品牌，**不是同款或自動搜尋成功**。瀏覽器核對獨立於先前只取得 SPA 空殼的 HTTP 測試。

- [三件搜尋與限制總紀錄](../out/verification/shopping-improvement-20261022/summary.json)。
- [兩次查詢完成紀錄](../out/verification/shopping-improvement-20261022/complete-broadening/report.json)。
- [人工商品頁核對](../out/verification/preview-shopping-20261022/manual-uniqlo-review/report.json)、
  [UNIQLO 官方近似款](https://www.uniqlo.com/tw/zh_TW/product-detail.html?productCode=u0000000052833)。
- [13 筆展示資料來源](../out/verification/preview-shopping-20261022/review-data-provenance.json)：
  十件重建與三件搜尋保留不同原始觀察 ID，沒有把其他裁圖的搜尋證據貼到重建資料上。
- [最終前端的真實介面檢查](../out/verification/preview-shopping-20261022/live-ui-current/report.json)：
  26/26 桌機／390px 手機情境，僅讀取，衣物資料 SHA-256 前後一致；測試數不是圖像準確率。

搜尋後續修正保留不同商家的候選，最多查看五筆但只讀取三個商品頁；同商家已限制存取時，
跳過其餘網址並繼續後面的候選。白 T 的五筆既有結果離線重播證明能到達原先漏掉的第五筆，
此項不算新搜尋或新商品成功。Shopline 市場判斷增加目前網址、商品、商家與台幣資料的綁定核對，
實際重新讀取 JimmySelect 頁面及主圖後通過商家地區判斷，卻因全黑水洗款不同於原照藍黑拼接款，
被實際圖像比較排除；人工目視同意，沒有當成相似款或同款。

另修正拼色衣物查詢遺失次要顏色：首個查詢保留最多三個受限色名，較短的第二個查詢不變。
相同跑者原照追加 **2 次真搜尋、3 個真商品頁讀取**，仍是 **0 個通過核對**；
候選分別因地區、缺主圖或已達讀取上限未通過。這次追加測試與首次三件分開，不重新計算原先 0/3。

- [候選排序、五筆離線重播與一次真實商品圖比較](../out/verification/shopping-improvement-20261022/bounded-verification-after/report.json)。
- [保留藍黑雙色後的實際重跑](../out/verification/shopping-improvement-20261022/runner-palette-rerun/report.json)。

### 程式交付驗證

最終程式回歸 **952 項通過**，Ruff 檢查、格式檢查與 JavaScript 語法檢查通過。
這些驗證包含自動搜尋排隊、取消／重試、來源檔案保護、預覽報告綁定、購物排除理由與匯出。
另修正生成指令污染：生成只接收失敗檢查／特徵／清楚原圖區域的識別碼，模型自由文字修正
仍保留在報告中，但不再直接當作編輯指令。已測到的「補內搭／項鍊」不會進入生成請求。
本頁真實圖片基準早於這個最後的傳輸修正，沒有以程式測試數或該修正宣稱新的還原成功率。

- [完整 pytest 結果](../out/verification/preview-shopping-20261022/pytest-release.log)。
- [桌機／手機最終對照頁檢查](../out/verification/preview-shopping-20261022/gallery-ui-release/report.json)：
  十件首輪、四件細節重測、四個購買區塊與 37 張圖片都正常載入，私人資料路徑回傳 404。
- [正式應用的唯讀檢查](../out/verification/preview-shopping-20261022/production-ui-release/report.json)：
  1440px 與 390px 畫面各載入 14 張原有圖片；生活穿搭的重建／購買搜尋預設勾選，
  原圖上下文、完整原圖與辨識區域可切換，沒有瀏覽器錯誤或水平溢出。
  未提交任何修改或模型請求，完整工作／衣物 API 資料前後一致。
- [原始資料最後核對](../out/verification/preview-shopping-20261022/final-original.json)：
  6 筆工作、14 件衣物、15 筆稽核及空白批次／分組資料，均與工作前備份逐列相同。
- [獨立 wheel 驗證](../out/verification/preview-shopping-20261022/wheel-release.json)：
  從來源目錄以外載入套件，介面、離線範例、媒體與匯出通過，沒有模型呼叫。
- [最終服務及套件雜湊](../out/verification/preview-shopping-20261022/final-services-and-hashes.json)：
  正式服務 8765、實測服務 8776 與對照頁 8777 均可讀；30 個套件輸入與通過測試的版本一致。

此輪完成流程及交付驗證；**90% 款式忠實度與確認同款購買仍未達成**。
目前購買功能是原圖品牌判讀加文字搜尋、商品頁及圖片核對，尚未整合以原圖檢索的商品圖片索引。
僅靠原照缺少型號的品牌／顏色線索，無法把找到的近似商品確認為同款。

以下保留 2026-10-21 及更早的實驗與當時驗證數字。

## 提高成功率：分割已有改善，完整款式忠實度仍有限

本輪固定舊照片、觀察資料與驗收條件，另加入兩張事前選定的 Pexels 生活照，實際呼叫模型。
結果分成衣物找回、來源不確定性、生成圖可見細節及技術可靠性；不能把測試通過數當作圖片準確率。
新照片是便利取樣，舊照片用於開發回歸，這些小樣本不支持一般準確率估計。

### 分割與分類

| 檢查 | 修正前 | 修正後 |
| --- | --- | --- |
| 同五張照片的預定衣物數量及類別 | 4/5 | **5/5** |
| 同五張照片的所有流程條件 | 3/5 | **4/5** |
| 兩張新照片的所有流程條件 | 2/2 | **2/2** |

最終五張共 14 件：背面跑者 2、雨天大衣 3、夜間雙人 4、正面跑者 2、商務框選 3。
夜景仍有低光下的不確定衣物警告，工作保持 partial，沒有為增加通過數取消警告。
新照片分別為白 T 恤／牛仔褲 2 件，以及全黑外套／內搭／長褲 3 件。
另一次固定雨天場景的三次重測均找回三件並正確分類；它們是同一照片的重複測量，不能算三張新照片。

改善來自分割失敗時一次較寬原圖及通用上衣名稱的重試，以及分類不足時一次獨立原圖上下文複核。
只有真實遮罩才能成為衣物；放大遮罩映回原座標，人物框選仍限制可讀範圍。
每張最多三件可進行上下文複核，恢復結果仍需人工確認。單獨帽兜等部件不再當成一件衣物。
最初結果、擴大範圍結果、取消及錯誤均有保留；複核失敗不會丟失第一次判讀。

- [相同五照的最終正式 API 結果](../out/verification/lifestyle/model-success-component-guard-regression-20261021/report.json)。
- [兩張新照的最終結果](../out/verification/lifestyle/model-success-component-guard-holdout-20261021/report.json)、
  [凍結舊版對同兩張新照的結果](../out/verification/lifestyle/model-success-holdout-baseline-20261021/report.json)。
- [雨天重複測量](../out/verification/lifestyle/model-success-final-rain-repeats-20261021/report.json)：
  另兩次與同輪五照中的雨天一次合計 3/3；此輪早於最後的帽兜部件提示修正。

### 重建與錯誤保留

重建增加原始解析度的細節參考、生成前獨立原圖複核及一次針對差異的圖片修正。
第二張只在各項核對無退步、至少一項改善時採用；保留候選的核對報告必須對應該張圖片。
總時限 240 秒、每個模型請求 120 秒、最多兩張生成。原圖規劃與細節複核的格式錯誤各最多重試一次，
階段診斷不保存請求憑證或原始例外內容。修正也涵蓋實際 SDK 串流結束時的資源清理問題。

相同七件的第一輪全新生成產出五張候選：大衣及正面跑者曾自動接受，碎花洋裝拒絕，
格紋襯衫及商務大衣待核對；咖啡廳來源不足而停止，背面跑者在生成前失敗。
獨立目視沒有將兩張自動接受計為完整款式忠實通過。五個前後都有圖片的案例，固定可見特徵
為 **15/17 → 16/17**，確認洋裝袖口改善，但花紋仍太細密；含無輸出的全部七件則是
**18/21 → 16/21**。不能只挑有圖案例宣稱整體重建成功率提升。

兩張新照中，白 T 恤因側面及紮入褲腰的限制而未生成；黑外套首次在生成前失敗。
另一次黑外套重試產生兩張，但胸前字樣左右及形狀仍錯；第二張局部核對回應不完整，保留第一張拒絕候選。
背面跑者在最終程式的獨立重試已產生一張完整背面上衣，仍因被遮住的領口與袖口待核對。
這兩次重試另列，沒有取代首輪失敗數。兩個早期失敗均未在診斷呼叫重現，不能宣稱已確認其根因。

模型重選細節區域時曾遺漏原本不清楚的結構。本輪保留初次來源判讀及未解區域，
局部變清楚不能解除整個被遮擋部位的核對義務；確定的細節失真仍優先保留為拒絕。
另修正「非關鍵、但會畫在生成圖中」的未知部位被忽略：來源規劃明確區分展示部位、
不展示的另一面及尚不清楚。只有具備來源證據且位於相反一面的未知部分，才不阻擋完整核對。
被手遮住的正面袖口不屬於不展示的另一面。舊報告缺少這項證據則降為待核對，候選仍保留；
後端與瀏覽器主圖判定同步，沒有用關鍵字猜測舊資料的意思。
既有大衣、正面跑者候選另做來源規劃與核對，這些呼叫沒有產生新圖，報告及原圖 SHA-256 不變。
自動判讀仍會漏掉來源限制，因此目視結果與模型判定分開呈現，不宣稱可穩定還原同款完整衣物。

- [本輪原圖／舊版／新版對照頁](../out/verification/success-rate-20261021/index.html)，
  本機可開啟 `http://127.0.0.1:8775/success-rate-20261021/index.html`。
- [事前固定評分條件](../out/verification/success-rate-20261021/evaluation-plan.json)、
  [七件首輪完整結果](../out/verification/success-rate-20261021/final-reconstruction-seven/report.json)、
  [新兩件完整結果](../out/verification/success-rate-20261021/final-reconstruction-holdout/report.json)。
- [黑外套另一次重試](../out/verification/success-rate-20261021/black-jacket-retry/pipeline/report.json)、
  [背面跑者另一次重試](../out/verification/success-rate-20261021/runner-back-final-retry/report.json)、
  [同圖後續模型核對](../out/verification/success-rate-20261021/final-fidelity-recheck.json)。

最後一次[正面跑者的真實來源規劃及同圖覆核](../out/verification/success-rate-20261021/final-fidelity-scope-check.json)
明確把胸下拼接／口袋下緣、袖口與側標列為會出現在生成圖中的未知部位，另把不展示的背面單獨保留。
兩個規劃階段均第一次回應有效；候選判定為 needs_review，沒有生成新圖或改寫原圖。
之前的 accepted 報告仍保留。另有[舊報告離線重驗](../out/verification/success-rate-20261021/final-fidelity-scope-deterministic.json)，
只套用新驗證規則，沒有新增或修改模型的觀察文字，不能算作新的模型成功案例。

### 程式驗證與本機更新

最終完整 pytest **813 passed**，其中包括 18 項瀏覽器 JavaScript／後端核對規則對照，
使用自含式真實舊報告 fixture 驗證誤通過被攔下。Ruff 檢查與格式、JavaScript 語法、
sdist／wheel 建置及來源目錄外的安裝包 API／媒體／匯出測試均通過。
這些數字驗證程式行為，不代表圖片準確率。

正式服務 `http://127.0.0.1:8765` 已重啟新版，原目錄 **6 個工作、14 筆衣物、15 筆 audit**
與本輪前備份逐列一致。新實驗結果另放在 `http://127.0.0.1:8774`，共九件，
依相同候選 SHA-256 附上後續核對或獨立重試，原始實驗報告不修改。
九件真實結果的桌機／390px 手機檢查 **18/18 通過**：主圖來源、候選並排、未採用原因、
圖片解碼與無水平溢出；沒有頁面錯誤、外部請求或資料修改。另唯讀檢查正式目錄兩件、兩種尺寸，共 4 項通過。

- [813 項回歸結果](../out/verification/success-rate-20261021/pytest-verified.xml)、
  [安裝包測試](../out/verification/success-rate-20261021/wheel-verified.json)。
- [原目錄重啟後比對](../out/verification/success-rate-20261021/release-after-restart.json)、
  [正式目錄桌機／手機檢查](../out/verification/success-rate-20261021/production-browser-final/report.json)。
- [九件展示資料的可追溯來源](../out/verification/success-rate-20261021/review-data-provenance.json)。
- [九件真實結果的桌機／手機檢查](../out/verification/success-rate-20261021/review-browser-final/report.json)。

以下保留上一輪及更早的完整紀錄，數字代表各自當時的程式與實驗，並非本輪最新結果。

## 品質修正：實際圖像仍未達全自動忠實重建

收到「效果很差」後，重新逐張檢查原來的 10 張生活照、22 個裁切區域及 9 張生成圖。
舊版確實有上下身混成一件、把背面改生成正面，以及袖口和花紋被改寫的問題。
先前的「生成完成」「PNG 可讀」只證明流程運作，不能作為款式品質驗收。
以下是最新修正與驗證；後面的舊紀錄保留原始結果，不代表目前品質已達標。

生活穿搭現在先列出可見衣物，再逐件以具體名稱呼叫 SAM 分割；沒有真實有效遮罩的目標
會回報漏檢，不能直接把視覺模型畫的框當成分割結果。原圖框選人物、座標映射、遮罩去重、
自訂概念與取消流程均保留。重建先判讀原圖可見面與細節，保留正／背面，生成後再逐項比較，
另對最多三處領口、袖口、拼接或花紋等區域作局部核對。關鍵資訊不足可停止生成；
明確不符會拒絕採用，仍有不確定處則保留候選供比較。最多一次有依據的修正重試。

### 同照片分割重測

正式 API 以五張原照重新呼叫真實 SAM／Spark，產出 13 件紀錄，未使用替身。
事前固定數量／類別／工作狀態條件，完整符合為 **3/5 案**，數量與類別符合為 **4/5 案**；
這是小樣本檢查，不是一般辨識準確率。

| 原照 | 正式 API 重測結果 |
| --- | --- |
| 遠距背面跑者 | 原本整身混件，現在上衣、長褲分開；2 件 |
| 夜間雙人 | 兩人的外套、長褲各自分開；4 件，另有不確定衣物警告，因此工作為 partial |
| 正面跑者 | 外套與短褲分開；2 件 |
| 多人商務框選 | 只分析選中人物的大衣、襯衫、裙子；3 件 |
| 雨天大衣 | 大衣與長褲分開；黑色內搭漏檢，只有 2/3 件，工作為 partial |

雨天照片的初次獨立實驗曾取得三件，但正式 API 重測及加上一次通用名稱 fallback 的重測
仍漏掉內搭。進一步的 `top` 查詢沒有遮罩，`garment` 查詢得到混入腰帶／長褲的區域，
不能視為乾淨的上衣，因此沒有採用該方法掩蓋失敗。各次報告、遮罩與原始回應均保留。

### 重建與忠實度重測

六件舊觀察資料先作全新生成，另將新分割的背面跑者上衣送入最終正式流程，合計 **7 件**：
6 張生成候選、1 件因來源不足停止。六件第一輪結果另以最終局部核對程式重新檢查；
這是對既有候選的真實模型覆核，不冒充重新生成。

| 原照 | 目視與最終核對結果 |
| --- | --- |
| 背面跑者 | 已有完整、單件、保留背面的上衣；手套及毛帽遮住袖口和後領，仍需核對 |
| 背包格紋 | 已保留背面，不再生成正面鈕扣與胸袋；背包後方的接縫／打褶未知，仍需核對 |
| 雨天大衣 | 完整大衣候選；動態飄動無法確定靜止下襬與寬度，仍需核對 |
| 正面跑者 | 胸前分片有手部遮擋，不能確認全部幾何；仍需核對 |
| 多人商務大衣 | 手機與手遮住前襟扣位；仍需核對 |
| 坐姿碎花 | 生成圖將直筒袖口改成收縮褶邊，花朵更小、更密；**拒絕採用** |
| 咖啡廳毛衣 | 領口與下襬皆不可見；**沒有生成** |

最終為 **5 張待核對候選、1 張拒絕候選、1 件未生成，0 件自動接受**。
第一輪整張比較曾誤接受碎花裙，現在局部比較能指出袖口與花紋差異；最初誤判報告仍保留。
兩張舊圖作負向控制：錯生成正面的格紋襯衫被拒絕；原跑者圖因來源細節不明標記待核對。
沒有為了取得通過數而放寬標準，這也表示現階段不能宣稱生活照可穩定還原同款完整衣物。

介面主圖只使用已通過比對的生成結果，其他情況保留原圖；候選與原圖並排，列出不採用原因。
舊版無核對紀錄的生成圖另標為未核對。最新嘗試、上次通過圖、保留候選各自保存自己的
核對報告，失敗重試不覆寫先前已通過圖片；JSON／CSV 同樣保留正確的圖與報告關聯。

證據與可重現入口：

- [全部原圖、舊結果、新結果與限制](../out/verification/quality-repair-20261021/index.html)。
  本機對照頁：`http://127.0.0.1:8773/quality-repair-20261021/index.html`。
- [事前驗收條件](../out/verification/quality-repair-20261021/criteria.json)、
  [正式分割重測](../out/verification/lifestyle/model-quality-repair-20261021/report.json)。
- [第一輪新生成](../out/verification/quality-repair-20261021/reconstruction-v2/report.json)、
  [最終局部覆核及負向控制](../out/verification/quality-repair-20261021/final-validation.json)、
  [最終流程的背面上衣生成](../out/verification/quality-repair-20261021/runner-back-reconstruction/report.json)。
- [隔離展示資料的來源關聯](../out/verification/quality-repair-20261021/review-data-provenance.json)：
  候選網址與 SHA-256 核對後才附上評估；原實驗資料不改寫。
- 新版實際結果可在 `http://127.0.0.1:8772` 查看。正式目錄 `http://127.0.0.1:8765` 已更新程式；
  原有 **6 個工作、14 筆衣物、15 筆 audit** 與備份逐列一致，沒有重新分析或改寫原有衣物。

完整 pytest **717 passed**，涵蓋真實遮罩／座標、非接受候選、缺少核對資料、取消清理、
舊圖保留及匯出關聯等回歸；這個數字不是圖片品質成績。
真實結果的桌機／390px 手機唯讀介面 **18/18 通過**，包含主圖、候選並排、停止原因、
圖片解碼與無水平溢出，無頁面錯誤、外部請求或資料修改。
相關結果為 [pytest](../out/verification/quality-repair-20261021/pytest-verified.xml)、
[真實介面檢查](../out/verification/quality-repair-20261021/real-browser/report.json)、
[原資料比對](../out/verification/quality-repair-20261021/production-after.json)。
另有 22 組明標介面替身資料、**46 項瀏覽器檢查**，驗證接受／拒絕／舊圖／區域核對及通知狀態，
不能代替真實模型品質證據。Ruff、JavaScript 語法、sdist／wheel 建置及來源目錄外的
[wheel API／媒體／匯出檢查](../out/verification/quality-repair-20261021/wheel-report-final.json)通過。

## 追加生活照：10 張全新場景

依「多找一些網路上的生活照範例」新增 10 張 Pexels 照片，未重用先前樣本。
來源包含雨天、運動中、咖啡廳坐姿、雙人風衣、夜間合照、遠距背面、坐姿碎花洋裝、
背包遮擋、多人商務穿搭框選，以及局部騎車照。下載等比例 1600px 寬版本，逐張保存作者、
原始頁面、下載網址、授權、尺寸與 SHA-256；這是多樣性便利取樣，不是統計隨機抽樣。

先目視原圖並固定可見衣物清單，再以兩批各 5 張、隔離 API／SQLite 呼叫真實 SAM／Spark，
未使用替身，也沒有修改模型提示或品質門檻。10 個分析工作全部完成，輸出 **22 個區域**：
18 件穿搭待確認、1 件待人工核對、3 個需補拍區域。原始數量／類別檢查 **5/10 符合**；
這個比例不是衣物辨識準確率。

| 場景 | 目視結果 |
| --- | --- |
| 雨天大衣 | 大衣分離；黑色上衣與褲子混成一區，正確標記需補拍 |
| 正面跑者 | 運動外套與短褲分離 |
| 咖啡廳 | 橘色毛衣與深色圍巾分離 |
| 雙人風衣 | 兩件風衣分開，另取得一條圍巾 |
| 夜間雙人 | 左側外套、褲子分離；右側整身混件而需補拍，顏色與材質不能直接採信 |
| 遠距背面跑者 | 上衣與褲子沒有分開，整體區域需補拍；沒有強制重建 |
| 坐姿碎花 | 洋裝與針織外套分離；cardigan 被歸 top，與事前 outerwear 預設不同，並非漏檢 |
| 背包格紋 | 襯衫與短褲分離；背包遮住的部位仍不可見，深綠灰短褲被標 black |
| 多人框選 | 中央人物大衣、淺藍襯衫與黑裙分離，未混入旁人衣物 |
| 騎車局部 | 針織外套與長褲分離，漏掉少量可見的藍色內搭；cardigan 分類與預設不同 |

另外對 9 件衣物實際呼叫 Muse Image，9 張完整展示圖均生成並可讀，原始欄位、品質、
確認狀態及來源檔案 hash 不變。8 件依事前目標類別選取；騎車 cardigan 因分類差異另作補充重建，
原有失敗紀錄未改寫。每張來源最多重建一件；遠距背面跑者不符合種類條件，未強制生成。
兩批分割／判讀分別耗時 96.66 與 72.35 秒；兩批重建分別耗時 99.56 與 96.38 秒，批次平行執行。

已目視全部 10 張原照、22 張可見裁圖與 9 張重建圖。重建輪廓完整不代表忠實還原：
運動外套的袖帶及胸前分色接縫有變化；只見背面的格紋襯衫，其正面鈕扣與胸前口袋均是推測；
低光外套的色澤、未入鏡的衣長、碎花裙展開形狀與遮擋的領口也不能當作可見事實。
這輪未評估品牌、購物搜尋、跨照片同件衣物或配件完整檢出率。

新資料保存在獨立範例衣物室 `http://127.0.0.1:8771`；原有目錄與先前範例不變。
可檢閱[完整原圖、裁圖、重建與目視對照](../out/verification/lifestyle/web-expanded-20261021/index.html)，
[照片來源清單](../out/verification/lifestyle/fixtures/web-expanded-20261021/sources.json)與
[事前案例矩陣](../out/verification/lifestyle/fixtures/web-expanded-20261021/matrix-all.json)。
原始分析報告在 `out/verification/lifestyle/model-web-expanded-{1,2}-20261021/report.json`，
重建報告在 `out/verification/reconstruction/model-web-expanded-{1,2}-20261021/report.json`。
彙整、目視註記、唯讀桌機／手機檢查及正式資料比對保存在
`out/verification/lifestyle/web-expanded-20261021/`。本輪未修改應用程式碼，未重跑不相關的全套測試。

## 本次交付：完整衣物展示與實際商品核驗

本機版已整合批次生活照、逐張選人、衣物判讀、人工確認、重複衣物合併、完整衣物重建、
原圖品牌線索、購物搜尋、實際商品頁／主圖核驗，以及取消、重試、重啟恢復與匯出。
購買結果改為 v2：最多核對三個實際商品頁，以原圖比較種類、主色、圖案、袖長、領口與細節。
台灣模式排除外國或地區不明的頁面；已知缺貨、不符款式及無法讀取的商品不會當成合格候選。
舊版未經商品核驗的結果保留供匯出，但介面要求重新查找，CSV 也不輸出其未核驗購買網址。

**自動搜尋的正向驗收尚未通過。** 下列兩張原圖在台灣、繁中查詢及不限地區模式均沒有合格候選。
另外測試 `search_context_size=high`，仍為零候選，因此正式版保留原本的 `low` 設定。
所有失敗報告保留，未把「搜尋完成但沒有結果」改稱找到商品，也未把獨立控制案例注入自動搜尋。
本次證據支持開發流程與商品比對功能，不能宣稱自動找同款的命中率已達可用標準。

### 真實模型與目視檢查

兩張照片重新上傳，實際執行 SAM 分割、Spark 判讀及 Muse Image 重建，沒有模型替身。
Nike 官方穿著照輸出白 T 恤與黑短褲；Pexels 三人生活照框選中間人物後輸出紅 T 恤與卡其長褲。
四張新生成圖片均已逐張目視，輪廓完整且不含原人物；未入鏡的褲長、遮擋處與背面仍是 AI 推測。
原圖、欄位與媒體 SHA-256 均保持不變；生成圖片不作品牌或商品比對依據。
白 T 恤讀到可見 Nike／NIKE，沒有推測不可見款碼；無標誌的紅 T 恤保留未知品牌。

| 驗證 | 實際結果 |
| --- | --- |
| 新上傳 → 分割／判讀 → 完整展示圖 | 2 張照片、4 件衣物完成；目視檢查通過 |
| 自動品牌／購物搜尋 | 原圖品牌與可見特徵檢查通過；各輪合格商品均為 0，正向驗收未通過 |
| 獨立實際商品控制 | 3/3：相近磚紅圓領 T 恤接受；不同領口與缺貨 Nike 款排除 |
| 真實結果的桌機／390px 介面 | 12/12：圖片切換、無結果原因、生成來源限制、無水平溢出 |

獨立商品控制的網址在呼叫前另外搜尋並固定，使用正式 `verify_shopping`、實際商品下載與 Spark。
它驗證圖片比對能力，**不代表自動搜尋召回率**。接受的商品頁沒有可解析的庫存證據，因此保持庫存未知。
測試過程最初有一個 `merchant=null` 的控制資料格式錯誤，於模型呼叫前失敗；修正為實際網址主機後重跑，
初始報告也保留。商品主圖不是 AI 生成圖，來源頁和當時判斷均可檢閱。

- [原圖／重建／獨立商品控制對照頁](../out/verification/completion-20261021/review.html)。
- [第一次完整流程與預先設定的檢查](../out/verification/discovery/model-fullflow-20261021/report.json)。
- [繁中查詢重試](../out/verification/discovery/model-fullflow-localized-20261021/report.json)、
  [不限地區重試](../out/verification/discovery/model-fullflow-global-20261021/report.json)、
  [擴大搜尋資料量的最後一次實驗](../out/verification/discovery/model-fullflow-high-context-20261021/report.json)。
  後三輪使用已保存原圖觀察，重新呼叫品牌與搜尋，沒有再做分割或重建。
- [獨立商品控制原始結果](../out/verification/completion-20261021/product-controls/report.json)、
  [四件重建圖目視註記](../out/verification/completion-20261021/visual-review.json)、
  [真實目錄唯讀瀏覽器檢查](../out/verification/completion-20261021/real-browser/report.json)。

### 回歸、安裝包與資料保留

完整 pytest **612 passed**。新增回歸涵蓋公開 HTTPS／DNS 位址綁定與重新導向檢查、下載與影像上限、
商品頁與精確變體解析、庫存與市場判斷、六項外觀比對、原圖來源限制、取消／刪除／重啟清理、
舊結果保留與 JSON／CSV。商品圖片只從已核驗的本機媒體路徑顯示，不載入零售商追蹤圖片。
修正 `/media` 經由 symlink 越出資料目錄，以及搜尋失敗時重新混入未確認品牌或次要顏色的問題。

五組隔離瀏覽器測試共 **48 項情境通過**：基本 14、擴充 8、生活照 8、重建 7、品牌／購物 11。
這些流程使用明標模型替身，不能當成真實模型品質評估。最終購物介面另重跑 11 項通過。
12 項真實目錄檢查沒有頁面錯誤、外部連線或修改請求；完整頁截圖前逐張捲動並解碼延遲載入的圖片。
Ruff、JavaScript 語法、sdist／wheel 建置及來源目錄外的 wheel API／媒體／匯出檢查均通過。

正式服務為 `http://127.0.0.1:8765`；四件真實模型結果的隔離目錄為 `http://127.0.0.1:8770`。
正式資料與更新前備份逐列一致：6 個工作、14 筆衣物、15 筆 audit；批次與分組表仍為空。
沒有重新分析、修改或刪除原有目錄。

證據集中於 `out/verification/completion-20261021/`：
`pytest-final.xml`、五組 `*-browser/report.json`、`discovery-browser-final/report.json`、
`review.json`、`production-after.json`、`wheel-report-final.json` 與 `delivery.json`。
各真實模型報告保存當次程式雜湊；之後的防禦性解析與提示文字修正由最終回歸驗證，
不將較早模型回應宣稱為最後程式位元組的全新推論。

## 先前版本：原圖品牌與搜尋候選（尚未核驗商品圖）

衣物詳情新增手動「查找品牌與購買管道」，可選台灣／不限地區。先從原始可見像素與
上下文讀取品牌證據，再以文字搜尋購買候選。查找狀態、品牌證據與商品來源獨立保存；
不覆寫衣物欄位、品質或人工確認。AI 重建圖不作為品牌證據，AI 生成來源不能啟動查找。
支援排隊、取消、重試、搜尋失敗保留品牌、重新查找保留舊結果、重啟恢復、刪除與 JSON／CSV 匯出。

在呼叫模型前固定兩個案例，使用隔離 API／SQLite 與真實 Meta SAM／Spark：

| 原圖案例 | 品牌結果 | 購買結果 |
| --- | --- | --- |
| Nike 官網的白色 T 恤穿著照，胸前可讀 NIKE | observed / Nike，可見文字 NIKE，未推測看不到的型號 | 2 個有實際搜尋來源的相似候選 |
| 上輪 Pexels 生活照中的素面紅色 T 恤，未見 Logo／標籤 | unknown，品牌與型號 null，信心 0 | 3 個有實際搜尋來源的相似候選，不當成衣物品牌 |

Nike 案例重新上傳、分割與判讀；紅 T 恤重用已保存的原圖觀察，未重跑分割。兩件均重新
呼叫品牌判讀與購物搜尋，沒有推論替身或快取答案。最終一輪 38.85 秒；兩件各 10 項檢查
通過，包括品牌預期、無虛構型號、來源支持、原始記錄與媒體 SHA-256 不變。
這是兩案例 smoke test，不是品牌準確率或商品推薦品質評估。

初版真實搜尋有原始商品結果，但 strict JSON 沒有引用標記，因此錯誤地全部被濾掉。
保留初版報告後修正：候選 URL 必須精確匹配已完成搜尋的原始結果；標題取自搜尋來源，
商店名稱顯示網址主機。來源只保留實際選中或引用的結果；不能因缺引用而捏造或擴充網址。
型號候選另需原圖可讀款碼及來源標題／摘要中的完整款碼，說明用固定文字，避免庫存或官方店等
無依據話術。重播測試另涵蓋單個錯誤／私有網址不拖累其他有效結果。

搜尋品質仍有限：白色 Nike 上衣的候選含黑色款與菲律賓頁；紅色短袖的候選含印花、長袖款。
它們均標為相似候選，沒有證據是原圖同款。另行唯讀檢查的 Nike 頁為不同款且顯示無供貨，
Zara／Farfetch 頁未能透過閱讀工具存取；應用程式沒有據此宣稱庫存、配送、正品或原購買店家。
這些限制與實際結果均保留，不把搜尋成功當成精確找到同款。

證據：

- [事前矩陣與修正前結果](../out/verification/discovery/model-20261021/report.json)。
- [相同原圖重新呼叫的最終結果](../out/verification/discovery/model-source-results-20261021/report.json)。
- [原圖目視與候選品質限制](../out/verification/discovery/model-source-results-20261021/visual-review.json)。
- [最終程式的保存回應重播](../out/verification/discovery/provider-final-replay-20261021.json)：無新網路請求，
  品牌結果完全相同，購物結果除查詢時間外完全相同，包含最終程式雜湊。最終版另補了無效候選過濾，
  此離線重播不冒充新模型推論。

完整 pytest **440 passed**；新增 discovery API／provider **128 項**，覆蓋單併發／64 筆上限、
原圖與 symlink 限制、品牌文字與款碼證據、未知品牌、無來源／不安全 URL、CSV 公式、取消／重試、
重啟、同時編輯與重建、跨 manager 刪除競態及 SQL 失敗回滾。獨立審查無已確認待修缺陷。
原有四組瀏覽器回歸 **37/37**，新增 discovery **8/8**，共 **45 項通過**。
新增情境含 TW／全球、observed／possible／unknown、原圖依據、草稿保留、舊結果市場、失敗恢復、
取消、partial、生成來源拒絕、危險 URL／HTML、390px。瀏覽器替身測試不代表真實模型準確率。
報告在 `out/verification/discovery/{legacy-browser,extended-browser,lifestyle-browser,reconstruction-browser}/`
及 [`discovery-browser/final`](../out/verification/discovery-browser/final/report.json)。

真實結果的桌機／390px 唯讀檢查 **4/4**，原有清單的原圖允許／生成來源拒絕 **4/4**；
皆無頁面錯誤、外部連線或修改請求。正式服務在 `http://127.0.0.1:8765`，新品牌實測範例目錄在
`http://127.0.0.1:8770`。逐列比對更新前 SQLite 備份完全一致：6 個工作、14 筆衣物、15 筆 audit，
批次與分組表仍空。Ruff、JavaScript 語法、sdist／wheel 建置與隔離 wheel API 檢查通過。
證據在 `out/verification/discovery/{real-browser,production-browser,production-after.json,wheel-report.json}`。

## 完整衣物重建

依「應該重新生成完整衣物，而非只從照片挖出來」加入參考圖重建。生活穿搭預設在
辨識後自動排隊；也能關閉自動、手動生成／重試／取消。卡片與詳情顯示 AI 標記，
原圖、上下文與可見像素裁圖仍可切換。衣物欄位、品質、信心、人工確認與相似比對
始終使用原始觀察；生成完不會自動成為已確認衣物。

真實模型先做一件牛仔外套 provider probe（24.02 秒），確認 Meta `/images/edits`
確實接收兩張參考圖、沒有文字生成 fallback。再使用上一輪網路照片保存的 SAM／Spark
觀察，透過隔離 API 發出三次全新的 Muse Image 請求，66.72 秒全部完成。
這輪沒有重跑分割，也沒有使用模型替身或重用舊生成圖。

| 案例 | 目視結果與保留限制 |
| --- | --- |
| 牛仔外套 | 完整領口、雙袖、袖口、下襬；保留藍色牛仔與車線。前片、口袋配置和鈕扣間距含推測。 |
| 紅色 T 恤 | 手部遮擋缺口補齊，呈現完整紅色圓領短袖，無人體或旁邊衣物。隱藏衣身與版型仍是推測。 |
| 碎花洋裝 | 完整細肩帶、碎花與裙襬，保留棕／粉／綠色系及褶皺。原圖裙襬未入鏡，生成裙長不能當真實長度。 |

三件均通過 API 接受、生成完成、實際 PNG 可讀、合成標記、觀察資料不變及來源
逐檔 SHA-256 不變六項檢查。已逐張目視原圖上下文與生成圖。這是三案例 smoke test，
不是普遍準確率或款式忠實度測量。
[原圖／裁圖／生成圖對照頁](../out/verification/reconstruction/model-20261021/index.html)、
[原始 API 紀錄](../out/verification/reconstruction/model-20261021/report.json)、
[目視註記](../out/verification/reconstruction/model-20261021/visual-review.json)。

完整 pytest **312 passed**。新重建整合 48 項覆蓋去重、單併發／64 筆上限、取消清理、
失敗保留舊圖、無金鑰、未知碎片、選項預設與批次衝突、重啟、JSON／CSV 及 stale attempt。
獨立審查發現的三項問題均修復並回歸：同 job symlink 誤取合成參考圖、重啟殘留半成圖、
刪除期間重送生成競態；刪除 SQL 失敗時也驗證媒體復原與解除阻擋。

新增瀏覽器 **7/7**：自動生成與實際 PNG、保留編輯草稿、原圖切換、手動／鍵盤、
失敗重試與取消保留舊圖、手機 390px、模型不可用。原有三組回歸 **14/14、8/8、8/8**，
共 **37 項通過**，無 page errors。這些瀏覽器流程使用明標模型替身與隔離資料庫，
不代替上述真實生成品質驗證。報告分別在 `out/verification/reconstruction-browser/final/`
及 `out/verification/reconstruction/{legacy-browser,extended-browser,lifestyle-browser}/`。

最後以三張實際模型產出的圖片驗證桌機／390px 手機卡片、每件的展示圖與原圖／裁圖切換，
以及正式服務舊紀錄的重建控制，共 5 項唯讀檢查通過，無頁面錯誤或修改請求。
證據在 [`real-browser/report.json`](../out/verification/reconstruction/real-browser/report.json)。
正式服務已更新於 `http://127.0.0.1:8765`；三張生成範例的獨立目錄在
`http://127.0.0.1:8767`。正式資料逐列比對更新前 SQLite 備份完全一致：
6 個工作、14 筆衣物、15 筆 audit，批次與分組表仍為空。
[`production-after.json`](../out/verification/reconstruction/production-after.json) 保存健康狀態與比對結果。
Ruff、JavaScript 語法、sdist／wheel 建置與隔離 wheel API 檢查皆通過。

## 新網路照片實測

依「網路上找一些隨機圖來自動辨識」取得 5 張未使用過的 Pexels 照片，涵蓋街頭疊穿、
花紋洋裝、室內暗光、合照框選一人與整張多人街拍。這是搜尋後的多樣性便利取樣，
不是統計隨機樣本。照片來源、作者、授權與事前衣物清單在
`out/verification/lifestyle/fixtures/web-sample-20261021/`。

實際呼叫 Meta SAM／Spark，未使用模型替身。原圖批次 3 張完成、2 張因超過 2,400 萬
像素被拒絕；另下載同照片等比例 1600px 寬版本，兩張重跑完成。原始失敗沒有覆寫。
兩批流程合計 189.81 秒，5 張共輸出 16 個區域：**15 件待人工確認、1 個碎片需補拍**。

| 情境 | 目視核對結果 |
| --- | --- |
| 牛仔外套街拍 | 外套、圖案 T 恤、破膝牛仔褲，共 3 件 |
| 花紋洋裝 | 1 件；裙擺在畫面外，裁圖仍有缺口 |
| 室內暗光 | 條紋襯衫、牛仔褲，共 2 件；顏色受暖光影響 |
| 合照框選中間人物 | 上衣、牛仔褲，共 2 件；白內搭只剩碎片被拒絕 |
| 三人裁切街拍整張分析 | 7 件主衣物的可見區域均有分離 |

模型將鏤空針織上衣標為 `t-shirt`，暗光牛仔褲標為 `black`；部分顏色、材質與破洞
是否屬瑕疵仍需人工確認。帽子、鞋子、背包等配件未完整檢出。15 件可辨主衣物不等於
15 件完整裁圖或屬性全正確，也不能換算成普遍準確率。本批沒有已知同件衣物正例，
不評估跨照片重複辨識召回率。

已逐一檢視 5 張原圖與全部 16 張裁圖。正式 jobs／items／audit 及新增表的原始列
測試前後一致，原有 14 筆資料保留；兩個暫存清單已清除。
可離線檢閱的[原圖與裁圖結果頁](../out/verification/lifestyle/web-sample-20261021/index.html)，
以及[彙整紀錄](../out/verification/lifestyle/web-sample-20261021/results.json)保留原始模型輸出
與人工觀察的區別。兩批原始報告分別在 `model-web-sample-20261021/` 與
`model-web-sample-resized-20261021/`，位於同一驗證目錄。

## 生活照、批次與重複衣物

依「規劃後全部補齊」完成 [`LIFESTYLE_PLAN.md`](LIFESTYLE_PLAN.md) 的功能範圍：
32 檔批次、逐檔錯誤、持久進度、整批取消／失敗重試、逐張選人、穿搭待確認、
相似候選、選擇主紀錄的人工合併／取消合併、全部來源匯出。每檔 50 MB、整批 256 MB，
最多 64 筆執行中或排隊工作；生活穿搭僅接受照片。

新增 API 情境 **29 項通過**：混合有效／無效／空檔、同識別重送及 bytes／框選衝突、
大小／檔數／容量、無金鑰、人物選區與原圖往返、取消保留完成項目、只重試未完成項目、
缺失與越界 symlink 來源拒絕、重啟及刪除後摘要、合併／跨群組合併／取消／主紀錄刪除、
品質不被合併改寫、JSON／CSV 及無副作用的候選查詢。
另有穿搭媒體／品質與相似候選的單元測試，包含 EXIF、片段拒絕、有限候選數及屬性衝突。
`tests/conftest.py` 會在測試收集前隔離模組層級 app 的資料目錄；測試不開啟正式清單。

獨立交易／併發驗證通過：SQL 中途失敗回滾群組、來源與 audit；相同請求同時送達只留下
一個 batch/job；識別衝突回 409、容量競爭回 429 且無殘留目錄。另修正單筆重試可能複製
job 外 symlink 來源的問題；修正後拒絕且不建立新工作。完整方法與界線在
[`review.md`](../out/verification/lifestyle/review.md)。

瀏覽器新增 **8/8** 情境通過：桌機 pointer、鍵盤、390px 真實觸控事件框選，EXIF 6 的
選區與後端像素座標一致；混合 JPG／TXT／空檔的合法照片照常完成；32 檔上限、逐檔移除、
批次重試／取消、人工確認、候選比較與主紀錄、各來源切換、合併還原，以及伺服器接受上傳
後刻意遺失回應再重送不重複。使用真實暫存 API／SQLite，僅模型為明標替身，無 page errors。
批次總數包含拒絕檔案，刪除來源後保留原總數並顯示已移除數量；列表與對話框均驗證一致。
最終報告：[`browser-final-counts/report.json`](../out/verification/lifestyle/browser-final-counts/report.json)。
原有 **14/14** 與擴充 **8/8** 瀏覽器回歸亦通過；正式清單的 **5/5** 唯讀檢查通過。
證據在 `out/verification/lifestyle/{legacy-browser-verified,extended-browser,production-browser}/`。

真人照片以預先寫好的矩陣，執行全新 SAM 3.1 + Spark API batch，無模型替身、無快取推論：

| 情境 | 實際結果 | 約耗時 |
| --- | --- | ---: |
| 全身夏日穿搭 | 白色上衣、淺藍短褲，兩筆待確認 | 29 秒 |
| 半身穿搭 | 藍色拉鍊帽衫、白色內搭，兩筆待確認 | 40 秒 |
| 多人照框選左側人物 | 正確排除鄰人，辨識兩件上衣；**漏掉黑長褲** | 26 秒 |
| 第一張照片的較緊框選變體 | 同上衣及短褲，兩筆待確認 | 25 秒 |

4 個工作均完成，共 8 筆待確認、0 筆自動可整理，整批 120.3 秒。
事前的內容檢查 **3/4 通過、1/4 失敗**，失敗就是合照預期的下身未被找到；
不能將完成工作數當成完整辨識率。鞋子、皮帶亦未被廣泛 `garment` 提示分別找出。
每次來源、完整原圖、選區、上下文、遮罩裁圖與原始品質評估均保存並目視檢查。
保留模型偶爾不通順的中文理由、暗色照片的材質／款式不確定性。

另做唯一一次獨立 `pants` 提示補找：13.25 秒取得選定人物的黑長褲，仍列待確認；
沒有覆寫或把原來的漏檢改記成通過。來源與授權在 `fixtures/sources.json`。
兩份可直接檢閱的圖文證據：
[`原始四情境`](../out/verification/lifestyle/model-real-20261021/visual-review.html)、
[`指定褲子補找`](../out/verification/lifestyle/model-targeted-pants-20261021/visual-review.html)。

這批相似比對素材是**同一照片的選區變體，並非不同時間／角度的照片配對**。
基準結果找回短褲、漏配白背心，沒有出現其他錯候選；這個小樣本不代表普遍準確率。
檢查發現白衣／白背景的輪廓描述退化為 0–1 個有效格點。修正後將缺失輪廓視為
缺少證據，須有更強的明暗、色彩、比例與款式相符才提出較低分候選；不把缺失輪廓加分。
新增 12 項白衣、不同白色物品、空白影像及衝突反例，全數通過。
對原 8 筆資料離線重算，原短褲候選 0.8023 保留，新增白背心候選 0.6871，無其他新增配對。
原真實模型報告與特徵未改寫，未再次呼叫模型；證據在
[`duplicate-after.json`](../out/verification/lifestyle/duplicate-after.json)。
相似候選永遠需要人工確認，不還原遮住的衣物，也不推斷人物身份。

程式已重新啟動在 `http://127.0.0.1:8765`，新批次／分組／候選端點與模型設定健康檢查正常。
正式 jobs／items／audit 原始列的 SHA-256 更新前後一致：
`6c6262a469f2f8e149a1751011bc21897b72057d0320056a6fa3a9d9db1d97e7`，仍為
6 個工作、14 筆分析、15 筆稽核；新增三張表均未混入測試資料。
證據：`out/verification/lifestyle/production-before.json`、`production-after.json`。
更新前的 SQLite 與程式備份同在該目錄。

Ruff、格式檢查、JavaScript 語法檢查及 `uv build` 通過。wheel 從工作區以外的暫存目錄
匯入，驗證生活照靜態介面、批次 API、離線範例、可取消分組、全部來源匯出及候選端點，
無模型呼叫，結束後清除暫存資料。證據：`out/verification/lifestyle/wheel-report.json`。

## 擴充情境測試

依「多測試幾種情況」補測，單元／整合測試由 81 增至 **137 項，全數通過**。
新增涵蓋混合成功／需補拍／模型失敗、完成一件後取消、實際限制辨識呼叫數、
零偵測、失敗重試、人工確認撤回與匯出一致性；另以真實影像與影片編碼器測試
EXIF 八種方向的像素位置、透明白衣／半透明邊緣、WebP／palette PNG、
同追蹤 ID 去重與正確影格、30.0 秒接受／30.2 秒拒絕、偽 MP4 拒絕。

真實 Glimmer／Spark 對照找到兩項缺陷：本機模型將茶杯的完整外形判為可整理，
兩個模型也會在單件模式的雙衣物照片中自行挑選其中一件。已增加必要的 `target`
判定，與完整度分開保存；非衣物、多件或不明目標均不會自動列入可整理清單。
介面分別提示換成衣物照片或改用多件衣物模式。原始模型資料仍保留。

預期結果在呼叫模型前寫入 `out/verification/extended/fixtures/matrix.json`。
使用相同素材重測的 **14 項明確判定通過**：

| 情境 | Glimmer | Spark |
| --- | --- | --- |
| 完整牛仔褲 | 單件、完整、下身 | 單件、完整、下身 |
| EXIF 旋轉的同張牛仔褲 | 正確轉正並收錄下身 | 正確轉正並收錄下身 |
| 茶杯照片 | 非衣物，不收錄 | 非衣物，不收錄 |
| 純白空白圖 | 非衣物，不收錄 | 非衣物，不收錄 |
| 缺褲頭與褲腳的裁切 | 局部，不收錄 | 局部，不收錄 |
| 嚴重模糊圖 | 不明目標，不收錄 | 不明目標，不收錄 |
| 單件模式的兩件衣物 | 多件，不收錄 | 多件，不收錄 |

另有 **2 次觀察性測試**：博物館的印花禮儀圍巾照片，Spark 判為完整配件；
Glimmer 將圍巾與桌布視為多個目標。這個差異仍保留為模型限制，不列為通過案例。
特殊外觀、背景布料仍可能需要人工核對；本測試不是代表性準確率評估。

保留修正前 `model-before.json`（11 通過／3 失敗／2 觀察）與修正後
`model-after.json`（14 通過／0 失敗／2 觀察），包含提示／schema／素材雜湊及實際輸出。
照片來源、作者與授權在 `fixtures/sources.json`：牛仔褲與茶杯是公開授權照片；
模糊／裁切／旋轉圖是測試衍生素材；雙衣物是牛仔褲與先前 AI 生成上衣的合成。

使用新資料庫、新 Meta 呼叫完成 **3 項 API 端到端驗證**：

- 單件模式雙衣物：0 件可整理、1 筆多件提示，4.41 秒。
- 同張照片多件模式：SAM 分出 2 個目標，正確保存上衣及下身，13.06 秒。
- 同素材製作的 3 秒靜態測試影片：2 個獨立追蹤 ID、2 件可整理衣物，17.43 秒。

以上也檢查 JSON／CSV 與資料庫結果一致、媒體可讀，完成後移除暫存資料。
影片是明確標示的合成測試素材，不能據此推論移動衣架或遮擋影片的辨識效果。
證據：`out/verification/extended/live-flows.json`。

新增瀏覽器測試 **8/8 通過**：320px／768px、200% 實際字體尺寸、102 字中文與 emoji、
空顏色、篩選中儲存／覆核、斷線重試、MP4 實際播放、多件與非衣物提示。
使用真實暫存 API／SQLite 和明標的模型替身，不把它當真實模型測試。
報告：`out/verification/extended-browser/report.json`，包含實際播放時間與零 JavaScript 錯誤。
既有 14 項瀏覽器回歸也全數通過（`extended-regression/report.json`），重啟服務後
正式清單的 5 項唯讀檢查通過（`extended-live-browser/report.json`）。

正式資料的 jobs／items／audit 原始列資料雜湊前後相同：仍為 6 個任務、14 筆結果、
15 筆稽核紀錄。測試沒有增加或改寫原衣物清單。
證據：`out/verification/extended/production-before.json`、`production-after.json`。

## 截圖回報後的品質修正

使用者指出衣物卡片顯示狹長碎片卻被分成圍巾、褲子。比對原圖、遮罩與裁切後，
9/9 遮罩 raster 尺寸等於其邊界尺寸，逐像素放置一致；5/9 的可見像素不到邊界面積
的 30%，9/9 靠近原圖邊緣。原圖是密集重疊、衣襬被截斷的工作服，問題是把可見
片段當成完整衣物分類。下方較早的端到端成功紀錄只證明流程，不能作為品質合格證據。

新版同時分析遮罩裁圖與含原場景的上下文，獨立判定完整度；不完整結果移到需補拍區，
猜測欄位不再顯示或當成 CSV 衣物屬性。原模型欄位和來源仍保留，可人工核對。

真實模型對照（`out/verification/quality-provider-probes.json`）：

| 素材 | Glimmer | Spark |
| --- | --- | --- |
| 遮擋的局部布料（兩個獨立區域） | fragment／other，14.61 秒 | fragment／other，5.22 秒 |
| 完整藍色 T-shirt | complete／top，11.45 秒 | complete／top，3.97 秒 |

加入一般衣物類別定義後，完整帽衫也由 `sleepwear` 改為 `top / pullover hoodie`；
以本機模型實測一次 15.22 秒。證據：`out/verification/quality-hoodie-probe.json`。

現有 14 筆結果已重新判讀：**2 件可整理、12 個區域需補拍**。最初 14 筆 `model_record`
均與修正前 SQLite 備份一致，人工確認仍為 0；原圖、舊裁圖未替換。
證據：`quality-reassessment.json`、`quality-final-state.json`（皆位於 `out/verification/`）。
原始猜測只作為模型證據保存，不代表正確答案；模型完整度判斷也不等同經標註資料驗證的準確率。

新版瀏覽器驗證：既有流程 **14 項通過**、隔離品質 fixture **15 項通過**，
真實清單另通過分組、保留上下文、中性名稱、三視角切換、CSV 與 390px 無溢出檢查。
報告分別在 `quality-browser-regression/`、`quality-ui-fixtures/`、`quality-live-browser/`。
真實清單檢查是唯讀；測試 fixture 不當成模型結果。

修正後另以隔離資料庫執行全新上傳（並非重用舊推論）：完整帽衫經 Glimmer
14.17 秒得到 1 件可整理上衣；衣架照片經新 SAM 分割與 Spark 判讀，79.80 秒得到
9 個需補拍區域、0 件可整理衣物。所有媒體端點可讀，新 job 亦保存完整 mask records。
證據：`out/verification/quality-new-upload.json`。隔離資料庫已清除，未增加正式清單筆數。

## 自動測試

`uv run python -m pytest -q`：**240 passed**，包含批次、選人、相似候選、分組、API、模型、影像、完整度與舊資料重評測試。
本次完整執行的 JUnit 證據：`out/verification/lifestyle/pytest.xml`（0 failures、0 errors）。

涵蓋圖片上傳與資料保存、人工編輯／確認、查詢篩選、CSV 公式防護、歷史示範的
冪等匯入、刪除回復資料、路徑穿越與跨來源請求限制、大小限制（含分塊上傳）、
並行排隊上限、取消競態、重試保留已生成圖片、服務重啟後恢復部分成果、
SAM 影格與物件 ID、裁切與遮罩邊界、影片實際轉碼／影格／預覽、逾時與部分串流。
新版另涵蓋完整度與信心分離、完整窄形衣物不被誤擋、猜測欄位抑制、上下文圖片、
人工覆核／撤回／刪除後的數量與摘要、備份、保留人工編輯、重評失敗與中斷後安全重試。

模型在單元測試中由可控制的替身提供，不把替身結果當成真實推論。

以下檢查通過：

```sh
uv run ruff check wardrobe tests scripts
uv run ruff format --check wardrobe tests scripts
node --check wardrobe/static/app.js
uv build
```

打包的 wheel 已從獨立暫存目錄載入，確認程式不是從原工作區匯入；
介面、靜態資源、三筆示範資料與離線匯出均成功。
證據：`out/verification/wheel-smoke.json`。

## 真實模型及端到端流程

測試使用現有設定的 Meta 金鑰與本機 Ollama；金鑰未記錄在證據中。

| 流程 | 實際結果 | 證據 |
| --- | --- | --- |
| 單件圖片 → Glimmer → 資料庫 | 完成，1 筆衣物紀錄 | `out/verification/e2e-local.json` |
| 衣架圖片 → SAM → Spark → 資料庫 | 完成，9 個候選物件、9 筆衣物紀錄及遮罩預覽；67.67 秒 | `out/verification/e2e-rack-garment.json` |
| 8 秒影片 → SAM → Glimmer → 資料庫 | 完成，3 個追蹤物件、3 筆衣物紀錄及追蹤 MP4；135.31 秒（含等待前一筆任務） | `out/verification/e2e-video-garment.json` |
| 從瀏覽器點擊生成 → Meta Muse Image → Glimmer → 清單 | 完成，綠色帽衫影像與 1 筆衣物紀錄；沒有 JavaScript 錯誤 | `out/verification/e2e-generated-ui.json` |

SAM 影片正規化為 640×360、5 fps；已測的 8 秒素材有 40 個影格。同一追蹤 ID
只保留一筆衣物，選取較大的可見畫面。個別 SAM 呼叫另有 66.14 秒的成功紀錄。

各提供者的獨立成功紀錄在 `out/verification/provider-probes-fixed-20261021.json`；
首次圖片生成成功紀錄在 `provider-probes-20261021.json`，其餘當時失敗的呼叫也保留，
沒有把修正前的失敗覆蓋成成功。

## 瀏覽器操作

`uv run python scripts/browser_smoke.py`：**14 項操作檢查通過**。

- Chromium，桌面 1440×1000、手機 390×844。
- 空白狀態、服務狀態、鍵盤快捷鍵、歷史範例標示。
- 編輯與確認、重新載入後保留資料、搜尋／類別／確認狀態組合篩選。
- 實際下載並解析 CSV 與 JSON。
- 歷史處理與媒體預覽、手機版無水平溢出、手機編輯與刪除確認。
- 使用者文字中的 HTML 被當成文字顯示。
- 不支援的檔案提示、真實壞圖失敗、重試建立新任務。

JavaScript 錯誤、console errors 及非預期 HTTP 錯誤均為 **0**。
測試使用獨立暫存資料目錄，完成後關閉服務；以 `webapp-testing` 的網路政策限制
瀏覽器僅連線到該本機服務，阻擋 service workers。沒有覆寫實際衣物清單。

報告：`out/verification/browser/report.json`。
已檢視的畫面包括 `desktop-catalogue.png`、`mobile-editor.png`；截圖停用過場動畫，
避免把動畫開始時的透明畫面當成完成版。真實推論清單截圖在
`out/verification/live-catalogue.png`。

真實追蹤影片另外在介面完成切換預覽、載入 8 秒 metadata、跳轉及播放。
媒體端點支援 `Range`，實測回傳 HTTP 206 與指定的 1,024 bytes；播放無 JavaScript
錯誤。證據：`out/verification/video-playback.json` 與 `live-video-playback.png`。

## 已知模型限制

- 同一份衣架圖片與影片，提示 `garment` 能取得上述結果；`shirt` 在獨立及完整流程
  呼叫中都回傳空結果。證據：`out/verification/shirt-diagnostic.json`、
  `e2e-rack.json`、`e2e-video.json`。應用程式保留提示原文並顯示未偵測到衣物，
  不會自行替換提示或拿歷史預測補成新結果。
- 本次完整流程的信心值約 0.15–0.60；品牌／尺寸常無法讀取，帽衫也有分類不準的
  例子。這些紀錄均保留人工確認狀態。物件數量是模型產生的候選數，不是標註真值，
  沒有宣稱辨識準確率或完整召回率。
- 遮擋、相似衣物與模型呼叫速度仍會影響結果。影片／多件模式需要 Meta；
  單件圖片可用本機模型獨立運作。

## 執行環境

Python 3.12.13、FastAPI 0.156.2、OpenAI Python SDK 3.35.1、
meta-sam-parser 0.1.0、Playwright 1.62.0、pytest 9.1.1、Ruff 0.17.1。
套件版本固定在 `uv.lock`。原始 `out/t*.json` 保持不變。
