# Rewear Studio — 提交資料夾

Meta Global Hackathon 2026 需要上傳或填寫的檔案都在這裡。

| 檔案 | 用途 |
|---|---|
| `rewear-demo.mp4` | **主要 Demo 影片**（1:41，1920×1080，30 fps；已上傳 <https://youtu.be/lm5mTxP44jc>）。英文旁白，畫面乾淨；內含可開關的英文字幕軌 |
| `rewear-demo-subtitled.mp4` | 同一支影片，英文字幕直接燒在畫面上。適合靜音自動播放的社群貼文，或無法另掛字幕的平台 |
| `rewear-demo.en.srt` | 英文字幕檔。上傳 YouTube 後到「字幕」加入，評審可以開關 |
| `rewear-chrome-extension.zip` | Chrome 擴充功能「Shop this look」。解壓後在 `chrome://extensions` 開啟開發人員模式，選「載入未封裝項目」 |

## 提交檢查清單

1. 把 `rewear-demo.mp4` 上傳到 YouTube（公開或不公開皆可），再上傳 `rewear-demo.en.srt` 作為英文字幕。
2. 在 Cerebral Valley 提交頁填入（截止：美東 11/4 21:00＝台灣 11/5 10:00）：影片網址、公開 Repository URL、團隊資料（細節見 [`docs/HACKATHON_HANDOFF.md`](../docs/HACKATHON_HANDOFF.md)）。
3. 若評審要試用擴充功能，附上 `rewear-chrome-extension.zip`，並說明需先在本機執行 `uv run rewear`。

## 評審試用網站

已部署在 <https://rewear.soraing.com>（GCP VM ＋ Cloudflare Tunnel，不開放註冊）。密碼用這個指令從鑰匙圈取出：
`security find-generic-password -s rewear.soraing.com -a judge -w`。在提交表單的測試說明欄填入：

```text
Live demo: https://rewear.soraing.com
Judge login: judge / <password>   (shared account; the site doesn't allow changing it)
Tip: on a phone tap "Take a photo"; on a laptop use "Upload a photo" or "Import photos" with any street-style photo.
Analysis takes about 30–90 seconds per photo; each account has a daily allowance.
Chrome extension: load rewear-chrome-extension.zip (unpacked), set its address to https://rewear.soraing.com/
in the extension popup, sign in once, then hover any outfit photo and click "Shop this look".
```

帳號密碼只寫在提交表單，不要寫進這個資料夾或 repo。建立者的電腦上另有 `SUBMISSION_FORM.md`（每個欄位可直接貼上的英文內容，含評審密碼），已列在 `.gitignore`，不會上傳。

## 影片內容

| 時間 | 段落 | 旁白（英文） |
|---|---|---|
| 0:00–0:19 | 街頭 POV：拍照 → Meta AI 掃描（2× 速）→ 1 件洋裝 → 購物 | See an outfit you love? Just snap it. Meta AI takes it from there… |
| 0:19–0:23 | 轉場：靈感其實在動態牆裡 | But most of the looks you love aren't on the street. They're in your feed. |
| 0:23–0:55 | Chrome 擴充功能實錄：示範動態牆 → Shop this look → Rewear 找出 8 件 → Muse Image 預覽 → 購物連結（等待 20× 速） | So we built Rewear for Chrome… Eight pieces, across all three outfits… |
| 0:54–1:14 | 運作方式：該次分析真實的 SAM 3.1 遮罩、Muse Spark 1.3 欄位、Muse Image 1.0 預覽 | Under the hood, three Meta models work together… |
| 1:14–1:24 | 商業價值 | Every photo in a feed is shopping intent… |
| 1:24–1:34 | 誠實設計與測試數據 | It's honest by design, and it's tested… |
| 1:34–1:41 | 結尾 | Rewear Studio. See it. Find it. Buy it. |

完整旁白逐句與時間見 `rewear-demo.en.srt`；旁白原稿在 [`media/production/narration.json`](../media/production/narration.json)。

## 影片中的事實與素材來源

- 擴充功能段是**實錄**：真實的 SAM 3.1、Muse Spark 1.3 與 Muse Image 1.0 約 100 秒找出 8 件，預覽約再 100 秒完成。
  加速的等待段都在畫面上標示倍速。
- 動態牆「Moments」是示範頁面，不是 Facebook／Instagram。照片使用 Pexels 授權，頁面上有標示攝影者。
  擴充功能尚未在真實 Facebook／Instagram 實測，見 `docs/HACKATHON_HANDOFF.md` 的「目前不可宣稱的內容」。
- 開頭街頭 POV 畫面取自 `media/production/sources/rewear-pov-film-v2.mp4`（另一位代理製作的影片的導入重剪版）。
- 旁白由 Kokoro-82M（Apache-2.0，本機執行）合成，聲音為 `af_heart`。配樂與音效都是用程式合成的原創內容。
- 影片的測試數據頁只用活動期間的成果：實錄三人合照約 100 秒找出 8 件、30 張街拍重測、1,612 項自動化測試（含 12 項真實瀏覽器端對端）。10/23 前置準備的 100 張基準（90/100、85/100）只寫在 README，影片不引用。

重新產生影片的方法見 [`media/production/README.md`](../media/production/README.md)。
