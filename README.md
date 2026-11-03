# Rewear Studio — See It. Find It. Buy It.

**English** · [中文](#中文說明)

Rewear Studio turns any outfit photo — a street-style snap, a friend's post, a short video — into a shoppable
breakdown of every piece, powered by **Meta SAM 3.1, Muse Spark 1.3 and Muse Image 1.0**. It keeps what the AI saw
apart from what it guessed: unclear regions are flagged instead of guessed, AI previews are always labeled, and
shopping links say "similar", never "the same item".

- **Live demo:** <https://rewear.soraing.com> (the judge login is in the submission form)
- **Demo video** (1:41, English narration and subtitles): [`submission/`](submission/README.md)
- **Chrome extension "Shop this look":** [`extension/`](extension/README.md)
- Built for the **Meta Global AI Developer Hackathon 2026**

## How it works

1. Take a photo, import up to 32 photos, or click **Shop this look** on any photo while you browse.
2. Muse Spark 1.3 lists the visible garments. For each one, SAM 3.1 segments it from a text prompt such as
   "blue denim jacket" and returns a pixel mask.
3. Muse Spark 1.3 reads every cut-out piece — category, specific style name, colors, pattern, fabric — with a
   confidence score. Pieces it can't read reliably are flagged and kept out of the results.
4. Muse Image 1.0 previews the complete garment (three candidates, about US$0.03 per piece), always shown next to
   the original photo and labeled as AI.
5. Every piece links to Google Shopping, Bing Shopping and web search for similar items at no model cost. An
   optional AI search looks for the brand's product page and lists only results that pass verification.

SAM 3.1 is called through the OpenAI-compatible Meta Model API (`wardrobe/providers.py`):

```python
client = AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key=MODEL_API_KEY)
stream = await client.responses.create(
    model="sam-3.1",
    stream=True,
    input=[{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "blue denim jacket"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,..."},
    ]}],
    metadata={"mask_encoding": "one_bit"},
)
# meta-sam-parser turns the stream into one box and one 1-bit mask per object
```

## Built during the hackathon (Oct 26 – Nov 4, 2026)

**Before the event (Oct 21–23): preparation.** An early prototype of the segmentation → garment-inspection →
preview pipeline and of the hosted mode, plus internal quality benchmarks. The 100-photo benchmark figures quoted
in the demo video (90/100 identified, 85/100 faithful previews) come from that preparation run on Oct 23.

**During the event.** The user-facing experience shown in the demo was built or rebuilt in the hackathon window:

- The two-step flow: Take a photo / Import photos → automatic Meta AI analysis → "N pieces found" → Shop similar,
  with a slide-up item sheet on phones and plain-language error states (including Meta API billing).
- Result quality from a 30-photo street-style QA run: one entry per garment, unreadable fragments kept out of
  results, and specific style names that drive better shopping queries.
- **Rewear for Chrome ("Shop this look")**: a new browser extension that sends one clicked photo from any feed
  to Rewear (`extension/`).
- Live verification with SAM 3.1, Muse Spark 1.3 and Muse Image 1.0, and a 12-test real-browser end-to-end suite
  (2 of the tests drive the extension).
- Judge-demo hosting controls: organizer-managed accounts and a per-account daily spend cap.
- The narrated demo film and its reproducible production pipeline (`media/production/`).

In numbers: 22 of 33 source files under `wardrobe/` and 30 of 48 test files were changed or added during the
event, and `extension/` is entirely new.

## Quick start

Requires Python 3.12+ (and `ffmpeg` for video).

```sh
uv sync --extra dev
export MODEL_API_KEY=...   # or save it in ~/.config/meta/model_api_key
uv run rewear              # http://127.0.0.1:8765
uv run rewear --lan        # also serve the Wi-Fi address, to test on a phone
```

The Meta key stays on the server; the browser never sees it.

**Chrome extension:** open `chrome://extensions`, turn on Developer mode, choose **Load unpacked** and select
`extension/`. Hover any outfit photo and click **Shop this look** — only that photo is sent to your Rewear, and
nothing runs in the background.

## Hosted mode and the judge site

`rewear --hosted` adds accounts (Argon2id passwords), CSRF and origin checks, and per-account storage and job
limits. For judging, the site runs with organizer-managed accounts (no sign-up or password changes) and a
per-account daily spend cap:

```sh
rewear account create --hosted-data-dir <dir> --username judge
rewear --hosted --public-origin https://<domain> --hosted-data-dir <dir> --accounts managed --daily-spend-limit-usd 5
```

The live demo runs this way on a Google Cloud VM behind a Cloudflare Tunnel. Operations, Docker/HTTPS deployment and backups:
[`docs/HOSTED_OPERATIONS.md`](docs/HOSTED_OPERATIONS.md).

## Quality and honesty

- Frozen 100-photo lifestyle benchmark (Oct 23, preparation): **90/100** target garments identified and **85/100**
  faithful full-garment previews; no failed or uncertain candidate is published automatically.
- 30 real street-style photos were re-tested during the event; the fixes are listed above.
- Automatic same-item verification still confirms 0 matches on the fixed set, so the UI only ever says "Shop similar".
- **1,610 automated tests**, including 12 real-browser end-to-end runs (2 of them drive the extension). Evidence:
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md); the raw `out/` evidence (about 2 GB) is kept locally.

## Repository layout

| Path | Contents |
| --- | --- |
| `wardrobe/` | FastAPI app, analysis pipeline, Meta model providers, web UI |
| `extension/` | Rewear for Chrome ("Shop this look") |
| `tests/` | Unit, API and real-browser end-to-end tests |
| `deploy/` | Docker + Caddy HTTPS deployment |
| `docs/` | Design, operations and verification notes (mostly in Chinese) |
| `media/production/` | Reproducible demo-film pipeline: scripts, recordings, narration |
| `submission/` | Demo video, subtitles, extension zip and submission checklist |

## License

MIT — see [`LICENSE`](LICENSE). Third-party photos keep their own licenses (Pexels License).

---

## 中文說明

衣序 Rewear Studio 把任何穿搭照片（街拍、朋友的貼文、短片）變成可以購物的單品清單，使用
**Meta SAM 3.1、Muse Spark 1.3 與 Muse Image 1.0**。AI 看到的和推測的分開呈現：看不清楚的區域會標示出來、
AI 預覽一律標示，購物連結只說「相似款」，不宣稱是同一件。

- **線上試用：** <https://rewear.soraing.com>（評審帳號寫在提交表單）
- **Demo 影片與提交檔案：** [`submission/`](submission/README.md)（1:41，英文旁白與字幕）
- **Chrome 擴充功能「Shop this look」：** [`extension/`](extension/README.md)

預設為本機模式，另可明確啟用帳號隔離的網站模式。實測數字、截圖與誠實限制整理在
[Hackathon 交付稿](docs/HACKATHON_HANDOFF.md)。`docs/` 引用的驗證證據在 `out/`（約 2 GB 的測試輸出與第三方
測試照片），只保留在本機，沒有放進這個 repo；結果摘要見 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)。
影片製作流程與原始素材在 [`media/production/`](media/production/README.md)。

### 活動期間完成的工作

**活動前（10/21–10/23）為前置準備：** 分割 → 衣物判讀 → 預覽流程與網站模式的早期原型，以及內部品質基準。
影片引用的 100 張基準數字（辨識 90/100、忠實預覽 85/100）來自 10/23 的前置準備。

**活動期間（10/26–11/4）完成：** 影片中使用者看到的產品體驗，都是活動期間新做或重做的：

- 兩步驟流程：Take a photo／Import photos → Meta AI 自動分析 → 「N pieces found」→ Shop similar；手機由下滑出的
  衣物詳情；照實說明的錯誤狀態（含 Meta API billing）。
- 依 30 張街拍實測改善結果品質：同件只顯示一次、無法辨識的碎片不進結果、具體款式名稱帶出更準的購物搜尋。
- **Rewear for Chrome（Shop this look）**：全新的瀏覽器擴充功能（`extension/`）。
- 以真實 SAM 3.1、Muse Spark 1.3、Muse Image 1.0 實測，以及 12 項真實瀏覽器端對端測試（其中 2 項操作擴充功能）。
- 評審試用網站的防濫用設定：主辦方發放的帳號、每帳號每日費用上限。
- 有旁白的 Demo 影片與可重建的製作流程（`media/production/`）。

量化：`wardrobe/` 33 個原始檔中 22 個、測試 48 個檔中 30 個在活動期間修改或新增，`extension/` 全部是新的。

### 啟動

需要 Python 3.12+；影片功能另需 `ffmpeg` / `ffprobe`。

```sh
uv sync --extra dev
uv run rewear
```

已有專案虛擬環境時也可以：

```sh
.venv/bin/python -m wardrobe
```

開啟 <http://127.0.0.1:8765>。預設只監聽本機，不會公開部署。
連接埠可以用 `--port 8766` 調整；開發時使用 `--reload`。

用手機實測（電腦與手機在同一個 Wi-Fi）：

```sh
uv run rewear --lan
```

終端機會印出 `http://<區網 IP>:8765`；用手機開啟後按「Take a photo」即可直接拍照分析，
「Import photos」可一次匯入多張。本機模式沒有登入，同網段任何人都能檢視與修改這個衣物室，
只在信任的網路使用。
請使用單一服務程序；同一資料目錄同時啟動第二個服務會被拒絕。

邊滑社群邊找同款（Chrome 擴充功能）：在 `chrome://extensions` 開啟開發人員模式，「載入未封裝項目」
選擇 `extension/`。之後在任何網頁（例如朋友的貼文）把滑鼠移到穿搭照片上按 **Shop this look**，
Rewear 會開啟並自動分析這一張照片。只有按下時才傳送該張照片，不會背景掃描；細節見
[`extension/README.md`](extension/README.md)。

網站模式包含註冊、登入、一次性復原碼、密碼變更、私人衣櫃、容量管理及媒體清理。
正式 HTTPS、容器安裝、容量參數、離線備份與還原方法見
[網站模式與維運](docs/HOSTED_OPERATIONS.md)。評審試用網站已以 `--accounts managed` 部署在 <https://rewear.soraing.com>（GCP VM ＋ Cloudflare Tunnel）；既有本機資料不會自動公開或移入帳號。

目前預設的低成本重建會以一次 Muse Image 請求產生三張完整候選，標準生成費率估算約
**US$0.03／件**；使用者選圖後會持久保存，但候選仍標示為 AI 推測。最新工程基準先從 Pexels
凍結 100 張生活照及每張固定目標，再執行推論，不以其他衣物或較容易的照片替換失敗案例。
人工稽核後，同一固定目標辨識為 **90/100**；其中 87 件有足夠來源證據可評估完整重建，最終
**85/100** 忠實產生完整單件衣物。正確辨識目標中的保真率為 **85/90（94.4%）**，可重建來源中的
保真率為 **85/87（97.7%）**；全部輸入的嚴格端到端率仍是 85%，未達 90%。其餘為 9 件漏辨識、
1 件混合遮罩、3 件來源資訊不足及 2 件生成失真；沒有失敗或不確定候選被自動發布。
最後 9 件另以每件一次、共 US$0.0225 的目標特定 SAM 探測逐張核對；沒有可在固定規則下恢復的案例，
所以沒有繼續付費生成。這也確認正式產品應先拒絕無衣物像素或關鍵結構被遮住的輸入。
這是搜尋分層的固定便利樣本，只作工程回歸，不能外推為一般生活照母體準確率。
逐件結果、固定清單、候選雜湊、失敗案例與費用在
[100 張最終報告](out/verification/reconstruction/web100-final-manifests-20261023/FINAL_100_PHOTO_REPORT.md)。

購買入口現在先提供不呼叫模型的 Google Shopping、Bing Shopping 與一般網頁搜尋；只有主動按下
「AI 查找品牌與商品頁」才會產生 Spark／搜尋費用。產品內仍只把通過原圖、商品頁、主圖與地區核對的結果
列為購買候選；目前固定實測的自動同款核驗仍為 0，因此畫面不會把外部搜尋連結宣稱成已確認同款。

最終交付已通過 1,610 項程式回歸（含 12 項瀏覽器端對端，其中 2 項載入 Chrome 擴充功能；2026-11-03 重新驗證）、隔離 wheel 安裝、實際服務重啟與資料雜湊比對，以及 Linux arm64
唯讀容器／HTTPS／登入隔離／備份還原 140 項檢查。桌機與 390px 手機瀏覽器均驗證三候選選圖及三個
免費搜尋入口；完整證據索引見[最新驗證紀錄](docs/VERIFICATION.md)。

### 模型設定

| 功能 | 需求 |
| --- | --- |
| 單件圖片 → 衣物欄位 | 本機 Ollama / `muse-glimmer:30b`，或 Meta / Muse Spark |
| 衣架照片 → 分割、裁切 → 衣物欄位 | Meta SAM 3.1；欄位模型可選本機或 Meta |
| 生活穿搭／合照 → 框選人物 → 可見衣物 | Meta SAM 3.1；欄位模型可選本機或 Meta |
| 辨識區域＋原圖 → 完整衣物展示圖 | Meta Muse Image，帶參考圖重新生成 |
| 免費購買搜尋入口 | 依已確認品牌／款號及可見屬性組成 Google Shopping、Bing Shopping 與網頁搜尋連結；不呼叫模型 |
| 原圖品牌線索 → 已核對購買候選（可選） | Meta Muse Spark；網頁搜尋、實際商品頁及圖片比對 |
| 可選原圖商品索引 → 候選頁面 | Google Vision Web Detection；需另外設定及可用權限，取得頁面後仍須商品核對 |
| 短影片 → 物件追蹤 → 每件衣物一筆紀錄 | Meta SAM 3.1 + ffmpeg；欄位模型可選本機或 Meta |
| AI 產生衣物圖片再分析 | Meta Muse Image；欄位模型可選本機或 Meta |
| 歷史示範、編輯與匯出 | 不需要模型或金鑰 |

預設 Ollama 位址是 `http://127.0.0.1:11434`。啟動 Ollama 並先安裝
帳號／環境有權使用的視覺模型；本專案不會自行下載大型權重。
若使用另一個支援圖片及 JSON schema 的本機模型，可設定 `OLLAMA_MODEL`。

Meta 金鑰由 `MODEL_API_KEY` 環境變數，或 `~/.config/meta/model_api_key` 讀取。
金鑰只留在伺服器，不能填進前端程式或提交至版本庫。
`.env.example` 列出設定範例，程式不會自動讀取 `.env`。

| 環境變數 | 預設值 |
| --- | --- |
| `OLLAMA_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `muse-glimmer:30b` |
| `MODEL_API_KEY` | 讀取上述金鑰檔 |
| `META_API_URL` | `https://api.meta.ai/v1` |
| `REWEAR_DATA_DIR` | 專案下的 `data/` |
| `REWEAR_IMAGE_SEARCH` | `disabled`；可明確設為 `google_vision` |
| `REWEAR_VISION_API_KEY` | 未設定；Google API 金鑰，僅伺服器環境讀取 |
| `REWEAR_VISION_PROJECT` | 未設定；使用既有 Google ADC 時必須指定有權限的配額專案 |

如果以 wheel 安裝而非從專案執行，預設資料目錄改為 macOS 的
`~/Library/Application Support/Rewear`，其他平台為 `~/.local/share/rewear`。

介面的「Meta 已設定」代表找到金鑰，不表示所有模型或額度都已驗證。
衣架、生活穿搭與影片模式會將影像傳至 Meta；單件圖片搭配本機模型則交給設定的 Ollama。
本機模型未連線或未安裝時，介面會停止送出並提供「重新檢查連線」；
若已設定 Meta，可明確按「改用 Meta 雲端分析」。系統不會自動替你切換雲端。
後端也會在建立上傳、生成或重試工作前重新檢查本機模型，避免先付出分割／生成成本才發現無法分析。

Google 原圖索引預設關閉，只有明確設定 `REWEAR_IMAGE_SEARCH=google_vision` 才會在購買查找時
傳送原始衣物裁圖。可使用私密環境內的 `REWEAR_VISION_API_KEY`；若使用既有 Application Default
Credentials，先以 `uv sync --extra dev --extra image-search` 安裝授權套件，再指定
`REWEAR_VISION_PROJECT`。應使用已授權且已啟用 Vision API、具有可用計費／配額的專案；程式不會
建立專案、改變 ADC 配額設定、啟用 API 或切換計費。不要把金鑰寫進前端、網址、範例檔或版本庫。
「已設定」僅表示有提供設定，不代表 API 權限或同款搜尋效果已驗證。
官方設定與回應格式見 [Google Web Detection](https://docs.cloud.google.com/vision/docs/detecting-web)。

### 使用流程

1. 選擇一或多張照片、或最長 30 秒的影片。每批最多 32 檔、每檔 50 MB、整批 256 MB。
2. 選擇單件、多件／影片或生活穿搭模式，以及欄位分析模型。生活穿搭只接受照片；
   合照可逐張框選想整理的人，保留完整原圖。多件／穿搭／影片使用 SAM 分割。
3. 查看分析進度、原圖、分割／追蹤預覽及結果。影片同一個追蹤 ID
   只建立一筆結果，挑選較大的可見畫面進行完整度與欄位判讀。
   生活穿搭預設勾選「生成完整衣物預覽」；「查找購買選項」預設關閉，減少無購買需求時的搜尋與核對費用。
   可取消生成，或自行勾選購買查找並選擇台灣或不限地區；也可整理後從衣物卡片逐件查找。
   分析結束後，處理紀錄及任務詳情仍會顯示生成／搜尋的獨立進度；按「查看衣物與生成進度」開啟該次結果。
   圖片含推測、生成失敗、查詢次數與尚無核對商品都會分開標示。
4. 檢查類別、顏色、品牌、尺寸、材質及瑕疵，修改後標記已確認。
5. 查看「可能是同一件」候選，比對不同來源，選擇主紀錄後確認合併；可隨時取消合併。
   確認同件後，可在詳情的「參考其他照片」再選最多兩張原照一起生成；當前照片決定展示面，
   額外照片補充可見細節。預設仍使用單照，合併不會自行啟動生成。
6. 在衣物詳情直接開啟零模型費的購物搜尋；需要產品內核對時，再按「AI 查找品牌與商品頁」，
   選擇台灣或不限地區。品牌與同款判定只採用原照片，不採用 AI 預覽。
7. 搜尋／篩選清單，下載 JSON 或 Excel 可讀的 UTF-8 BOM CSV。

生活穿搭可以辨識穿在人身上的可見衣物。衣物部分遮擋、但仍能辨識類別時，
會保留可見屬性並列入「穿搭待確認」，確認前不計為可整理衣物。看不清楚的碎片仍需補拍。
框選只指定分析區域，不辨識人物身份；選區若有多人重疊，仍可能包含別人的衣物。
預設生活穿搭先定位各件可見衣物，再逐件以原圖局部呼叫 SAM，減少整個人被合成一件的情況。
只有實際分割遮罩能建立候選；定位或分割失敗會留下警告，不以預估框冒充衣物。
若局部沒有取得遮罩，會再用較寬的原圖範圍尋找一次；小圖放大後的遮罩會映回原圖座標。
上衣重試使用較通用的類別提示，避免「襯衫／毛衣」名稱變動造成漏檢。
若可見衣物仍因局部上下文不足而無法分類，每張照片最多三件可各複核一次較寬的原圖。
複核不提供初次模型答案；只有符合既有部分可見規則的結果才轉入人工確認，不能直接變成已通過。
初次及複核判讀均保留，複核失敗仍保留初次結果。
進階設定的自訂分割提示仍照原文執行。

原圖、框選圖及裁切會保留。預設低成本重建直接檢視原圖與辨識區域，一次產生三張完整輪廓、乾淨背景的
單件展示圖，讓使用者比較並保存其中一張。三張共用一次請求、標準費率估算約 US$0.03；不另外執行付費的
規劃及逐項核對。提示要求三張對不可見構造採保守差異化補全，至少一張不新增帽子、口袋、扣件、Logo 或裝飾；
所有結果仍是未核對的 AI 預覽，原圖會一直並排保留。

明確選用嚴格核對模式時，才會先從原圖確認展示面、可見款式與不確定區域，再生成、逐項比較並視差異重試。
側面、紮衣與一般遮擋也可生成；推測的展示面、衣長及遮住部位會明列；無法確認單件衣物或衣物種類時停止。
生成後分別核對完整預覽與款式忠實度。五項預覽檢查（類別、主要顏色、單件、無人像／額外物件、
輪廓完整）通過後可顯示為「AI 預覽・含推測」；這不等於款式細節或同一商品已確認。
視角、可見細節、顏色花紋、拼接與輪廓全部通過才標為款式已核對；已知字樣、花紋等差異仍明列。
缺少預覽檢查的歷史圖不會自動升格，其他候選保留供比較；詳情並排保留原圖。
生成前會獨立複核原圖細節，並提供原始解析度的袖口、花紋等局部參考。
同一次複核也檢查原圖上、中、下部位的裁片，可補入兩處初次漏掉的細節；最終最多核對五處，
衣身下襬、袖口及小標分開描述，不以其中一處正確代替其他部位。
重新挑選清楚的細節區域，不會解除初次判讀中被遮擋部位的核對義務；看不清的部分仍保留未知。
未確定的袖口、衣長等展示部位不能通過完整款式核對；有來源證據、且確實不展示的另一面可另列為未知。
舊核對報告若沒有記錄這個區別，會改列待核對，圖片仍可查看。
原圖規劃及細節複核各可對格式不完整的回應重試一次，仍受總時限限制，並保存不含憑證的階段診斷。
有具體可修差異時最多重試一次；修正同時參考原圖與前一張候選，僅在各項核對沒有退步且至少一項改善時採用後圖。
重建總時限為 240 秒、各模型請求仍限 120 秒，最多生成兩張；不保證修正一定成功。
修正失敗或變差會保留較早候選及對應判讀。自動核對仍可能誤判，詳情會並排顯示原圖供檢查。
遮住或未入鏡的部分是 AI 推測，不能當成實際衣長、背面、尺寸或標籤的證據。
重建不修改原本衣物欄位、完整度、信心值或人工確認狀態，相似比對也繼續使用原圖。

詳情可以切換完整展示圖、原始照片與辨識區域，並手動生成、重新生成或取消。
舊紀錄與單件／多件模式可手動生成；類別不明的碎片必須先核對並填寫、確認資料。
生成失敗或取消時保留原始分析及上一張展示圖；服務重啟會標記中斷供重試，不自動重送。
重試的最新結果、已採用展示圖與未採用候選各自保存驗證報告，不會混用不同圖片的判定。
多照片重建只接受本人衣櫃中已確認同件的原照，不使用 AI 生成圖片或商品搜尋圖片。
每張生成圖保存自身來源，重試不會改寫舊圖履歷。刪除參考照或變更合併關係會取消相關的進行中工作；
已完成圖仍保留，已移除的來源另行標示，備份不會將刪除的原照找回。
已確認同件不代表所有細節都可見，遮擋、另一面及互相矛盾的資訊仍須核對。
使用方式與限制見[多照片重建](docs/MULTI_PHOTO_RECONSTRUCTION.md)。

每筆具合法原圖的衣物都先顯示免費購物搜尋入口。查詢只使用人工確認的品牌、原圖觀察到的品牌／款號，
以及可見的類別、顏色與圖案；不會把未確認的模型猜測品牌放進搜尋詞。外部連結不代表同款、庫存或配送已核對。

生活穿搭也可在上傳時自行勾選 AI 查找品牌與購買選項，或稍後從衣物卡片／詳情手動啟動：
原始裁圖與上下文交給 Meta 判讀，接著以主色、款式及品牌線索搜尋。
候選不足時最多再以較短的查詢詞搜尋一次，保留兩次搜尋紀錄，再核對實際商品頁。
若已啟用 Google 原圖索引，每件先增加至多一次原圖查詢，只接收含相符圖片的**網頁**，不把單獨
圖片網址當成商品頁。原圖查詢限 20 秒並包含在既有搜尋總時限內；只使用原始裁圖，不使用 AI 預覽。
圖片與文字搜尋各自記錄，圖片查詢失敗仍可保留文字候選；文字查詢失敗也可保留已取得的圖片索引候選。
索引不會把未知品牌或款號填成確定，也不會直接產生核驗通過的購買結果。
目前 Google 真實請求曾因既有配額專案權限不足被拒，尚無成功召回證據；接入口完成不代表此限制已解除。
拼色衣物的首次查詢會保留最多三個已知色名，避免藍黑拼色只剩黑色；較短的第二次查詢仍可補找候選。
可讀的 Logo／領標會列出品牌、可見文字和判讀依據；不完整線索列為可能品牌，無線索就保留無法確認。
AI 重建圖不參與品牌辨識，從 AI 生成圖片建立的紀錄也不開放此查找。
查找結果獨立保存，不會覆寫品牌欄位、人工確認或其他原始資料。

搜尋後最多核對三個實際商品頁及主圖，以原圖逐項比較衣物類型、主色、圖案、袖長、領口與可見細節。
候選優先分散至不同商店；遇到限制存取的網站時，會跳過該店其他網址，在三次商品頁讀取上限內
繼續檢查後面的候選，最多保留五筆核對結果與未核對原因。
只有符合已知特徵、商品頁地區也符合的候選才顯示「相似款」，並附本機保存的商品圖、比對結果與時間。
選擇台灣時，外國或地區不明的頁面不會通過；已標示缺貨、非商品頁或沒有可讀主圖也不會列入。
只有原圖可讀款號與實際商品頁款號相同，且外觀符合，才可能顯示「型號候選」。
不符與無法核對的候選會列出原因；沒有合格商品時保留搜尋入口。舊版結果需重新查找，不能當成已核驗商品。
找不到合格商品時，可下載原始辨識區域或原圖上下文，再開啟 Google Lens 手動上傳搜尋相似商品。
這個入口不會自動傳送照片，也不會下載 AI 預覽作為搜尋證據；生成來源或沒有合法原圖的紀錄不提供此入口。
Lens 找到的款號、商品與庫存仍需核對，不能當成產品內自動搜尋成功。
頁面庫存狀態是查詢當時的宣告，無庫存資訊就顯示未知；結果不保證同款、正品、配送或現在仍可購買。
照片本身無法證明原本在哪裡購買，隱藏標籤或無可見品牌仍維持未知。
可取消、重試；搜尋失敗仍保留已讀到的品牌線索，重新查找期間保留上次結果。

2026-10-22 的較早固定十件實測，完整預覽為 **10/10**，目視可見款式為
**6 符合、3 不符、1 未確定**。三候選低成本流程重新使用該份事前凍結清單後，
完整預覽 **10/10**、最佳候選可見錨點 **9/10**，總計 30 張圖、估算 US$0.30。
其 90% 只描述舊便利測試。2026-10-23 的 15 張測試後來也得到 14/15；最新的固定 100 張測試
取代兩者作為目前品質結果：人工稽核同目標辨識 **90/100**、可重建來源 **87/100**、忠實完整重建
**85/100**。正確辨識目標中的重建為 85/90，但全輸入端到端仍未達 90%。
三張原始照片的自動購買搜尋共執行六次查詢，通過核對 **0/3**，同款 **0 件**。
查到的候選網址、查詢詞及排除原因都可查看。另人工核對到一件 UNIQLO 白色短袖近似款，
與原照版型有差異，未算作自動搜尋或同款成功。詳見
[最新實測紀錄](docs/VERIFICATION.md)、[100 張逐件結果](out/verification/reconstruction/web100-final-manifests-20261023/final-100-photo-report.json)
與[歷史十件完整對照及購買搜尋紀錄](out/verification/preview-shopping-20261022/index.html)。

預設 `garment` 可能漏掉清楚可見的衣物；實測一張合照漏掉黑長褲，重新上傳同一照片、
框選同一人並在進階設定輸入 `pants` 後成功補找。這不是保證所有漏檢都能找回。

相似候選結合類別、顏色、款式、圖案及本機影像特徵，不新增雲端呼叫；
分數不是同件衣物的機率。比較範圍為最新 500 筆可用／待確認分析，預設最多 40 對，
每筆最多 3 對。相同顏色的不同衣物可能很相似，不同角度也可能漏配，必須人工核對。
合併只建立分組、不改寫或刪除來源。主清單以主紀錄呈現獨立衣物；任務詳情與匯出保留全部分析。

看不清楚的品牌、尺寸與材質保留 `null`。模型信心值不能當成準確率；
低信心、有瑕疵或狀況不明會標記需確認。人工確認與原始模型輸出分開保存。
CSV 對可能被試算表當成公式的文字加上前置單引號。

多件照片請拉開衣距，讓每件衣物的完整輪廓入鏡。分割模型只能選取可見的像素，
無法觀察遮擋的部分。完整展示圖由另一個圖片模型重建。應用程式會同時提供分割區域與原圖上下文給欄位模型，
另外評估 `complete / partial / fragment / unclear`；一般模式的不完整結果分到「需補拍」，
不會把布料碎片的猜測款式、顏色或材質當成衣物資料。未完成判讀則標為「尚無法判讀」。
尚未重建時卡片顯示原圖上下文，詳情始終保留完整原圖及辨識區域。完整度合格仍需要人工核對欄位。

判讀也會確認是否只有一件衣物。非衣物、空白或無法確認的照片不會自動收錄；
單件模式拍到多件衣物時，會提示改用「多件衣物」模式或分開拍攝。

人工填寫並確認後可以將結果納入目錄；原始完整度評估與經格式驗證的模型欄位會保留。
模型的 `needs_review` 會依低信心、未知狀況等規則保守正規化，並非原始回應的逐字副本。
CSV 包含 `quality_status / visibility / quality_reasons` 和
`garment_group_id / primary_item_id`，以及獨立的 `reconstruction_status / reconstruction_url /
reconstruction_is_synthetic / reconstruction_note`；品牌查找另有 `discovery_status / brand_candidate /
brand_evidence_status / brand_evidence / brand_confidence / product_code / shopping_status / shopping_market /
shopping_checked_at / shopping_verification_version / shopping_urls / shopping_exclusions`。
CSV 的購買網址只包含新版核驗通過的商品；JSON 保留完整模型證據、歷史結果、重建、查找狀態及 `groups` 分組。
CSV 另外保存最新重建嘗試、主圖與候選圖各自的品質和問題，未經驗證的舊圖標示 `unverified`。
完整預覽另有 `reconstruction_preview_url / reconstruction_preview_status /
reconstruction_preview_inferred_areas / reconstruction_preview_warnings`，只有完整預覽檢查通過的圖才匯出預覽網址。

### 失敗與資料保存

- 分析、重建與購買查找共用最多 64 筆執行中／排隊任務；每類一次執行一件，網站模式由所有帳號共用此額度。
- 每張照片或每段影片最多整理 24 個衣物區域；分析完成時圖片與購買查找可能仍在處理。
- 重啟後將中斷工作標記供重試，不自動重新呼叫模型。
- 每個批次保存逐檔進度和錯誤；無效檔案不影響同批合法檔。重新整理頁面後仍可查看批次。
- 上傳使用批次識別避免網路重送建立重複任務；修改照片或框選則建立新批次。
- 模型逾時或部分失敗會保留已完成的紀錄與裁切圖，畫面顯示實際錯誤。
- 欄位抽取失敗時留下待補資料的影像，不把空白欄位當成辨識成功。
- 取消會停止後續模型呼叫；正在轉碼的有限時長處理結束後才完成取消。
- 重試會建立新分析，保留舊結果；重新啟動會把中斷的任務標記為可重試。
- 整批重試只包含失敗、部分完成或已取消的任務；上傳前被拒絕的檔案需修正後重新選取。
- 資料庫在 `data/wardrobe.sqlite3`，影像在 `data/jobs/`。
- 編輯／刪除前的內容保存在資料庫 `audit` 表；刪除整筆分析會把媒體移到
  `data/.trash/`。因此介面刪除不是安全抹除，備份／清除時請一併處理。
- 更換電腦時，先停止服務，使用 `rewear backup` 與 `rewear restore`；不要只複製執行中的 SQLite 檔案。
  備份不包含已刪除工作的回收檔；網站模式刪除會嘗試永久清理媒體，失敗可在帳號管理重試。

舊版尚未做人為修改／確認的結果，可在停止服務後重新判讀（會呼叫原先選擇的模型）：

```sh
uv run python scripts/reassess_catalogue.py --data-dir data
```

重評先建立 `data/backups/pre-quality-*.sqlite3`，再將目標暫列待判讀；失敗或中斷可重跑。
它保留原圖、舊裁圖及最初模型欄位，並在 `audit` 保存每次修改前的內容；
不會自動重評已人工編輯／確認的資料，也不會重跑 SAM。

「載入歷史示範」會匯入 2026-10-14 已保存的三筆 Glimmer 輸出，不執行推論。
示範圖片是衣架畫面的三個切片，並非已驗證的單件分割結果。素材與來源詳見
[`wardrobe/demo/ATTRIBUTION.md`](wardrobe/demo/ATTRIBUTION.md)。

### 驗證與開發

[可用產品驗收](docs/PRODUCT_READINESS.md)追蹤尚未達成的還原及同款購買要求。

```sh
uv run python -m pytest -q
uv run ruff check wardrobe tests scripts
uv run ruff format --check wardrobe tests scripts
node --check wardrobe/static/app.js
uv build
```

核心 API 與驗收範圍在 [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)，
機器可讀 API schema 位於 `/api/openapi.json`。
測試使用隔離的暫存資料目錄及可控制的模型替身，不會消耗雲端額度；
真實模型與瀏覽器測試的結果另外記錄於 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)。

主要流程的端對端測試 `tests/test_e2e_browser.py` 會啟動真實服務，以無頭 Chromium 走完拍照、
批量匯入、拖放、詳情、購物連結、錯誤與重試、手機版，以及 Chrome 擴充功能從動態牆送出照片；它包含在上方的 pytest 指令中，未安裝
Chromium 時會自動略過（`uv run playwright install chromium` 安裝一次即可）。單獨執行：

```sh
uv run pytest tests/test_e2e_browser.py -q
```

瀏覽器回歸測試會自行啟動暫存服務，結束後刪除測試資料。它需要 Playwright
Chromium，以及 `webapp-testing` 技能附帶的 `network_policy.py`（可用
`--policy-dir` 指定其目錄）：

```sh
uv run playwright install chromium
uv run python scripts/browser_smoke.py
uv run python scripts/extended_browser_smoke.py
uv run python scripts/lifestyle_browser_smoke.py
uv run python scripts/preview_browser_smoke.py --output /tmp/rewear-preview-check
uv run python scripts/multiview_ui_smoke.py --output /tmp/rewear-multiview-check
uv run python scripts/primary_state_ui_smoke.py --output /tmp/rewear-primary-state-check
uv run python scripts/discovery_browser_smoke.py
```

多原照及主照狀態測試的輸出目錄須為新的空路徑，避免覆寫前一次證據。
`reconstruction_browser_smoke.py` 保留早期重建介面紀錄，尚未對應目前保真及預覽文字；
目前預覽與來源履歷回歸使用上列 `preview_browser_smoke.py`、`multiview_ui_smoke.py`。

已啟動應用程式後，可以實際呼叫模型驗證完整流程；此操作會建立新的目錄資料，
選擇雲端模型或多件／影片模式時會使用 Meta API：

```sh
uv run python scripts/verify_live.py samples/commons/rack.jpg --provider meta --mode rack --output out/live-rack.json
```

生活穿搭的真實模型驗證可用 `scripts/lifestyle_model_smoke.py --help` 查看參數。
它讀取已準備的來源矩陣、建立獨立暫存資料庫，保存新推論證據後清除暫存資料；
仍會呼叫 SAM 及選定的欄位模型，此腳本明確關閉後續重建。矩陣中的合照漏檢會如實回報測試失敗。
`scripts/reconstruction_model_smoke.py --help` 提供獨立重建驗證：讀取已保存的真實辨識來源，
經隔離 API 發出新的圖片編輯請求，保留原圖 hash、實際成功／失敗與可直接開啟的對照頁。
`scripts/quality_repair_smoke.py --help` 另外檢查生成後的品質判定與候選隔離，保留未採用的圖。
技術流程通過與圖片忠實度分開記錄；圖片能解碼不代表款式正確。
`scripts/discovery_model_smoke.py --help` 提供獨立品牌／購物搜尋驗證，接受事前定義的
新上傳照片或已保存原圖，呼叫真實 Meta，保留無結果、失敗、來源回應與原始資料不變檢查。
矩陣設 `reconstruct: true` 可驗證同一次上傳的自動重建；`require_verified_candidate: true`
要求至少一件通過實際商品圖比對，避免把全部無結果當成正向購物驗證成功。

已驗證的衣架素材使用預設提示 `garment`。相同素材使用 `shirt` 時 SAM 曾回傳
空結果，因此程式會顯示未找到符合提示的衣物；不會偷偷改寫使用者的提示。

原始 `poc/` 腳本保留供單項實驗，既有 `out/` 結果沒有被覆寫。
應用程式的正式入口是 `wardrobe/`。
生活照的完整範圍與驗收在 [`docs/LIFESTYLE_PLAN.md`](docs/LIFESTYLE_PLAN.md)。

參考：[FastAPI 檔案上傳](https://fastapi.tiangolo.com/tutorial/request-files/)、
[Ollama 結構化輸出](https://docs.ollama.com/capabilities/structured-outputs)、
[Meta SAM 3.1](https://ai.meta.com/blog/segment-anything-model-3/)。
