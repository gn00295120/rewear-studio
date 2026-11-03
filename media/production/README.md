# Demo 影片製作流程

本資料夾可完整重建 `submission/rewear-demo*.mp4`。除了「重新錄製」與「重新產生旁白」兩步，其餘都不需要網路或 Meta API。

## 內容

| 路徑 | 說明 |
|---|---|
| `sources/rewear-pov-film.mp4` | 原始街頭 POV 影片（另一位代理製作） |
| `sources/rewear-pov-film-v2.mp4` | 導入重剪版，由 `recut_pov.py` 產生 |
| `take-ext/` | 擴充功能實錄：`feed.webm`（示範動態牆）、`rewear.webm`（Rewear 分頁），以及事件時間 `events.json` |
| `artifacts/` | 同一次真實分析的 SAM 3.1 遮罩、Muse Spark 欄位與 Muse Image 預覽，由 `artifacts.py` 從分析資料匯出 |
| `feed/` | 示範動態牆頁面（Pexels 授權照片） |
| `narration.json` | 旁白原稿：每句的朗讀文字（`say`）、字幕文字（`show`）與在影片中的位置 |
| `voice/` | Kokoro-82M 合成的旁白音檔，由 `tts.py` 產生 |
| `design.py` / `film.py` | 版面元件與逐格合成（numpy 合成後交給 ffmpeg 編碼） |
| `subs.py` | 把旁白排上時間軸，輸出字幕 JSON 與 SRT |
| `music.py` / `mix.py` | 原創 lo-fi 配樂；混音（旁白出現時配樂自動壓低約 10 dB）並標準化到 −16 LUFS |
| `verify/transcribe_check.py` | 用 Whisper 轉寫成品，對照旁白原稿計算字錯率 |

## 重建影片（約 5 分鐘）

在本資料夾執行：

```sh
TIMELINE_ONLY=1 uv run python film.py . build/x.mp4      # 計算時間軸 → build/timeline.json
uv run python subs.py . build/rewear-demo.en.srt          # 字幕 → build/subtitles.json 與 SRT
uv run python film.py . build/video-clean.mp4             # 無字幕畫面
SUBS=build/subtitles.json uv run python film.py . build/video-subs.mp4   # 字幕燒在畫面上
uv run python mix.py . build/video-clean.mp4 build/rewear-demo.mp4 build/rewear-demo.en.srt
uv run python mix.py . build/video-subs.mp4 build/rewear-demo-subtitled.mp4
```

## X 用直式短片

`short.py` 產生 1080×1920、約 28 秒的短片（字幕燒在畫面上、旁白在 `voice-short/`）：

```sh
uv run python short.py build/rewear-short.mp4
```

旁白原稿在 `short_narration.json`，用下面同一個 `tts.py` 指令重新產生（輸出目錄改為 `voice-short`）。

## 重新產生旁白

修改 `narration.json` 後執行下列指令。第一次執行會下載 Kokoro 模型（約 330 MB）。

```sh
uv run --no-project --python 3.12 --with "kokoro>=0.9.4" --with soundfile --with pip python tts.py narration.json voice
```

`subs.py` 會檢查旁白句子是否互相重疊、是否超過片長，有問題時直接報錯。

## 重新錄製擴充功能段

需要即時 Meta API，每次約 4 分鐘，會產生模型費用。

1. 用獨立資料目錄啟動 Rewear：`REWEAR_DATA_DIR=<空資料夾> uv run rewear --port 8781`
2. 啟動示範動態牆：`python3 -m http.server 8792 --bind 127.0.0.1 --directory feed`
3. 錄製：`uv run python record_ext.py http://127.0.0.1:8781/ http://feed.example:8792/ ../../extension take-ext`
4. 匯出分析素材：`uv run python artifacts.py http://127.0.0.1:8781/ <資料夾> artifacts`。產生的路徑是絕對路徑，
   要改成檔名並把檔案放進 `artifacts/`。
5. 錄影時間與事件時間有偏移，需要重新量測並更新 `film.py` 的 `feed_zero`／`rewear_zero`。
   方法是用畫面變化（捲動、詳情開啟）對齊；Playwright 的 webm 長度不可靠，不能用來推算。

注意：Playwright 錄影按 CSS 像素放在畫布左上角，`record_video_size` 必須等於視窗大小。
無頭模式要載入擴充功能，必須使用 `channel="chromium"`。
