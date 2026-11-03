# Rewear Studio — Demo Video Script

> **Final cut (2026-11-03):** `submission/rewear-demo.mp4` (1:41, English voice-over and subtitles).
> Its actual structure and narration are in [`submission/README.md`](submission/README.md); this file is the original storyboard.

**Total Duration:** 2:30–3:00
**Format:** 1920×1080, 30fps
**Tone:** Cinematic product demo, Apple-style clean aesthetics
**Music:** Lo-fi / ambient (light, not distracting)

---

## ACT 1: THE PROBLEM (0:00–0:15)

### Scene 1 — Street Hook (0:00–0:05)
**Visual:** A person walking on a busy city street (POV or over-shoulder). Camera notices someone walking past in a striking outfit — floral dress, layered denim, or sharp streetwear.
**Text overlay (center, white on dark):**
> "You see the perfect outfit."

**Mood:** Warm, golden-hour lighting. Shallow depth of field.

### Scene 2 — The Frustration (0:05–0:10)
**Visual:** Close-up of hands pulling out a phone. Opens Google Image Search, types "floral midi dress blue" — gets irrelevant results. Scrolls through pages of wrong items.
**Text overlay:**
> "Reverse image search? Inaccurate."
> "Manual searching? Too slow."

### Scene 3 — The Solution (0:10–0:15)
**Visual:** Dark screen, cinematic text fade-in:
> **REWEAR STUDIO**
> **See It. Find It. Buy It.**
> *Turn any photo into a shoppable outfit breakdown.*

---

## ACT 2: THE PRODUCT — Scene 1: Street Style → Shop (0:15–1:00)

### Scene 4 — Open the App (0:15–0:20)
**Visual:** Close-up of phone screen. User opens the browser to Rewear Studio. The page loads clean and minimal: eyebrow "SEE IT. FIND IT. BUY IT.", headline "Spot an outfit you love? **Shop every piece.**", and two big buttons — **Take a photo** (dark green) and **Import photos**. Meta AI badge top-right.
**App URL on screen:** start the server with `uv run rewear --lan` and use the `http://<LAN IP>:8765` address it prints (currently `http://192.168.188.126:8765`).
**Note:** The real app is running. Use the actual mobile web UI.

### Scene 5 — Take the Photo (0:20–0:25)
**Visual:** User taps **Take a photo** → the phone camera opens directly → snaps the outfit they just saw → taps "Use Photo". There is no extra confirm step: analysis starts by itself.
**Text overlay (subtle, bottom):**
> "Snap any photo. Street style, selfie, group shot."

### Scene 6 — Meta AI Analyzes (0:25–0:30)
**Visual:** A progress card appears under the buttons: the photo thumbnail with a light scanning line moving down it, the label "ANALYZING WITH META AI", a stage line ("Locating each garment in the photo…" → "Reading details…"), a progress bar and a Cancel link.
**Key moment:** Show the real scanning animation. This takes ~10 seconds.

### Scene 7 — Results Appear (0:30–0:40)
**Visual:** The progress card turns into a results banner — photo thumbnail, "LATEST RESULT · photo.jpg", and **"3 pieces found"**. Below it, one card per garment: full-bleed garment image, category ("TOP", "DRESS"), a plain-English name ("Navy blue top"), its colors, and a **Shop similar ↗** button.
**Camera:** Slow zoom across the cards.
**Text overlay:**
> "AI identifies every garment. Category, color, pattern — extracted automatically."

### Scene 8 — Item Detail (0:40–0:50)
**Visual:** User taps a garment card. On the phone a sheet slides up from the bottom (tap outside it to close); on desktop it opens as a two-column card:
- Large garment image with "Close-up" / "Full photo" toggles
- The name ("Maxi dress"), "Confidence" and "Needs review" chips, then colors · pattern · material
- One big **Shop similar on Google** button, with **Bing Shopping** and **Web Search** beside it ("Free search links — no model call")
- A collapsed "More" row; opening it shows Meta AI exact-match search, the Muse Image "Full Garment Preview" (3 AI candidates, labeled as inference) and editing
**Camera:** Focus on the shopping buttons — this is the money shot.
**Text overlay:**
> "One tap to shop. Free search links — no API cost."

### Scene 9 — Shopping Link (0:50–1:00)
**Visual:** Back on the results, user taps **Shop similar ↗** on a card → Google Shopping opens with results for that piece (e.g., "navy blue top") — real products appear.
**Text overlay:**
> "From photo to product page. In seconds."

---

## ACT 2: Scene 2 — Friend's Post → Buy (1:00–1:25)

### Scene 10 — The Scenario (1:00–1:05)
**Visual:** Cinematic text card on dark background:
> **SCENE 2**
> **Friend's Post → Buy**
> *A friend shares their OOTD. Screenshot and find every piece.*

### Scene 11 — Screenshots, Bulk Import (1:05–1:15)
**Visual:** Phone notification — friends posted outfit photos. User screenshots three of them, opens Rewear Studio and taps **Import photos**, selects all three. Analysis starts immediately: the progress card shows the first photo with a "+2" badge and "1 / 3 photos done". The banner then reads "LATEST RESULT · 3 photos" with every piece found below.
**Key point:** Show it works with ANY photo — not just photos you take yourself — and with many at once.

### Scene 12 — Search (1:15–1:25)
**Visual:** User taps **All finds** on the results banner → "Your finds" grid with a search bar. Types "blue" — cards filter in real time. Only blue garments remain. While scrolling on the phone, a round camera button floats in the bottom-right corner — one tap snaps the next outfit.
**Text overlay:**
> "Every find, searchable. Find anything again."

---

## ACT 2: Scene 3 — Digital Wardrobe (1:25–1:45)

### Scene 13 — The Scenario (1:25–1:30)
**Visual:** Text card:
> **SCENE 3**
> **Your Wardrobe, Digitized**
> *Import your entire closet. Browse, search, organize.*

### Scene 14 — Browse Collection (1:30–1:37)
**Visual:** Desktop view (switch to laptop/desktop now; the primary button reads "Upload a photo" there and photos can be dragged onto the page). "Your finds" shows the grid of every piece found so far. User scrolls through — each card has the garment image, category, name, colors and **Shop similar**.
**Text overlay:**
> "Every piece cataloged, searchable, and shoppable."

### Scene 15 — History & Tracking (1:37–1:45)
**Visual:** User clicks the "History" tab. Imported batches ("Outfit · 3 files") and each analyzed photo appear with thumbnail, status (Completed / Partial / Failed) and time.
**Text overlay:**
> "Full history. Traceable, exportable, and organized."

---

## ACT 3: THE TECHNOLOGY (1:45–2:10)

### Scene 16 — Powered By (1:45–1:50)
**Visual:** Text card:
> **POWERED BY**
> **Three Meta AI Models**
> *Working together in one pipeline.*

### Scene 17 — The Pipeline (1:50–2:05)
**Visual:** Three cards appear one by one, left to right, connected by arrows:

| Step 1 | → | Step 2 | → | Step 3 |
|--------|---|--------|---|--------|
| **SAM 3.1** | | **Muse Spark 1.3** | | **Muse Image 1.0** |
| Pixel-precise garment segmentation. Not bounding boxes — actual pixel masks. | | Structured attribute extraction: category, color, pattern, brand clues. | | Reference-guided full garment preview generation. ~$0.03/item. | |

**Color coding:** SAM = blue (#0064e0), Spark = orange (#ff6b00), Muse = purple (#6c4dcd)

**Text overlay or narration:**
> "SAM segments. Spark understands. Muse generates."

### Scene 18 — Why Meta (2:05–2:10)
**Visual:** Text card:
> **WHY THIS MATTERS FOR META**
> *"Every photo in your feed contains shopping intent."*
> *Native product discovery — powered by Meta AI — at scale.*

---

## ACT 4: CLOSING (2:10–2:30)

### Scene 19 — Final Tagline (2:10–2:20)
**Visual:** Slow fade to dark. Centered gold text:
> **See It. Find It. Buy It.**
> From any photo to any store.

### Scene 20 — Credits (2:20–2:30)
**Visual:**
> **Rewear Studio**
> Built with Meta AI
> Meta Global Hackathon 2026
> Team: [your team name]

---

## TECHNICAL NOTES FOR VIDEO GENERATION

### App Screenshots / Screen Recordings Needed
Run `uv run rewear --lan`; it prints the phone URL (currently `http://192.168.188.126:8765`). Desktop: `http://127.0.0.1:8765`.
Mobile-optimized (390×844 viewport works perfectly).

**Live analysis works** (Meta billing active since 2026-11-03): one outfit photo returns its pieces in about 15–30 seconds, and the Muse Image previews replace the cards' photos about 40–70 seconds later. For a shot that needs results instantly, tap **See a sample result** on an empty "Your finds" (pre-analyzed sample, no model call).

**Key screens to capture:**
1. **Landing page** — headline "Spot an outfit you love? Shop every piece." + **Take a photo** / **Import photos**
2. **Analysis** — progress card with the photo, scanning line and stage text (starts automatically after the photo)
3. **Results** — "N pieces found" banner + garment cards with **Shop similar**
4. **Item detail** — slide-up sheet: garment image, name, one-tap "Shop similar on Google" (+ Bing / Web), collapsed "More"
5. **Search** — "All finds" → type "blue" → real-time filtering
6. **History tab** — batches and photos with statuses
7. **Mobile view** — same app on phone-sized viewport, fully responsive

### Available Lifestyle Photos (for street scenes)
- `out/verification/lifestyle/fixtures/web-sample-20261021/floral-dress-1600.jpg` — woman in floral dress, street setting
- `out/verification/lifestyle/fixtures/blue-hoodie-portrait.jpg` — blue hoodie portrait
- `out/verification/lifestyle/fixtures/web-sample-20261021/denim-street-1600.jpg` — denim street style
- `out/verification/lifestyle/fixtures/web-sample-20261021/friends-walking.jpg` — friends walking together
- `out/verification/lifestyle/fixtures/summer-full.jpg` — summer full-body outfit

### Brand Colors
- Primary dark: `#25463C` (deep forest green)
- Accent gold: `#C4956A`
- Background: `#FCF9F3` (warm off-white)
- Text: `#2a3a2e`
- Muted green: `#8a9a80`

### Fonts
- Headlines: Iowan Old Style / Palatino / Georgia (serif)
- Body: Avenir Next / system sans-serif

### Existing Generated Assets
- `media/archive/demo-video.mp4` — 2:01 screen recording of the **previous** UI (upload box + "Analyze Outfit"); re-record app footage with the current UI rather than reusing it
- `media/archive/demo-subtitles.srt` — English subtitle track
- Title card PNGs in scratchpad (intro, tech pipeline, outro)
