# Rewear — Shop this look（Chrome 擴充功能）

瀏覽社群或任何網頁時，滑鼠移到穿搭照片上會出現 **Shop this look**；按下後只把這一張照片交給你自己的
Rewear Studio，分析頁會自動開啟並開始找出每一件衣物與購物連結。也可以在照片上按右鍵選
「Shop this look with Rewear」。

## 安裝（未上架，開發者模式）

1. 先啟動 Rewear：`uv run rewear`（預設 <http://127.0.0.1:8765/>）。
2. Chrome 開啟 `chrome://extensions`，打開右上角「開發人員模式」。
3. 按「載入未封裝項目」，選擇本專案的 `extension/` 資料夾。
4. 按工具列的 Rewear 圖示可確認連線狀態；Rewear 不在預設位址時，在彈出視窗修改位址並儲存。

## 運作方式與隱私

- 只有使用者按下按鈕（或右鍵選單）才會動作；不會在背景掃描動態牆、不會自動分析任何照片。
- 擴充功能先抓取這張照片的原始檔；抓不到時才擷取畫面上該照片的範圍。只傳送照片本身與網站名稱
  （檔名如 `photo-from-instagram.com.jpg`），不傳送網址、帳號或貼文內容。
- 照片交給 Rewear 分頁後，由 Rewear 頁面自己上傳，與使用者手動選照片走同一條路徑；伺服器原本的
  同源保護不需要放寬。
- 需要 `<all_urls>` 權限，才能在任何網站顯示按鈕並讀取被點選的照片。

在 Facebook、Instagram 等平台使用時，請遵守該平台條款並尊重照片中人物：這是一次一張、由使用者
手動觸發的工具，不可用來大量收集他人照片。

## 測試

`tests/test_e2e_browser.py` 以無頭 Chromium 載入此擴充功能：在照片上覆蓋透明圖層的動態牆頁面
（社群網站常見結構）懸停並點選，確認 Rewear 分頁開啟並找出每件衣物、第二張照片沿用同一分頁，
以及 Rewear 未啟動時顯示清楚的錯誤。需要完整版 Chromium（`uv run playwright install chromium`）。
