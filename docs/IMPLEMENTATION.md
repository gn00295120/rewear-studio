# 衣序 Rewear — application contract

The default application is a single-user loopback service. An explicitly enabled
hosted mode adds authenticated, isolated wardrobes; no public deployment target has
been selected. Marketplace transactions are not implemented.
The shipped low-cost path returns three selectable, explicitly unverified images in one
Muse Image request. The latest frozen 100-photo lifestyle regression detected the same
audited target in 90/100 inputs; 87 sources had enough evidence to judge reconstruction,
and 85/100 produced a faithful complete garment after controlled recovery. This is
85/90 among correctly detected targets and 85/87 among reconstructable sources, but the
strict all-input end-to-end result remains 85%. Failed and uncertain outputs were not
auto-published. A final one-call-per-miss SAM probe cost US$0.0225 and recovered zero
strict outcomes, so no further generation spend was incurred. The search-stratified convenience suite is an engineering regression,
not a population-accuracy or same-physical-item claim. Functional completeness and image
fidelity remain separate in `VERIFICATION.md`.

## Acceptance criteria

- Upload an image or short video and follow an asynchronous analysis job.
- Select local Ollama / Muse Glimmer or Meta / Muse Spark for garment extraction.
- Single-item image analysis works without Meta. Rack/video segmentation uses
  SAM 3.1 and reports actual provider failures, never fabricated detections.
- Save sources, crops, overlays, job progress and garment records locally.
- Assess actual garment visibility using both the visible-pixel cutout and original
  scene context. Keep incomplete fragments separate from usable garments and
  suppress speculative attributes while retaining model evidence and source media.
- Review/edit records, search/filter the wardrobe, export JSON/CSV, cancel/retry
  jobs and delete unwanted items or jobs.
- Supply a clearly labeled historical sample import for an offline walkthrough.
- Expose optional Meta image generation followed by the same analysis pipeline.
- Validate media, enforce size/duration limits, retain partial results, recover
  interrupted jobs, and keep credentials server-side.
- Verify backend behavior, real model requests, and desktop/mobile browser flows.
- Batch upload up to 32 files / 256 MB, durable per-file errors/progress, cancellation,
  byte-bound idempotency, and non-destructive retry of unfinished jobs.
- Select a person rectangle in each lifestyle photo, retain the full original, and
  show identifiable worn partial garments in an explicit confirmation queue.
- Suggest cross-photo duplicates using local image features and visible attributes;
  only human confirmation creates reversible groups retaining every observation.
- Reconstruct complete garment display images from cutout and scene references,
  label synthetic content, and keep observed attributes, confidence and review state intact.
  Auto-queue eligible outfit results by default; support opt-out, manual retry and cancellation.
  After a human confirms a same-garment group, optionally select up to two other original
  observations. The current observation controls the display face; automatic jobs remain single-photo.
  Keep source hashes and provenance separately for each retained image.
- Identify visible original-photo brand evidence on demand or automatically after an opted-in outfit upload,
  and search source-backed shopping candidates.
  Keep unknown/possible/observed evidence separate from catalogue fields. Never identify brands from
  synthetic reconstruction or promote shopping candidates to proof of the exact item or original seller.
- Read actual public product pages and compare product images against original photographs before
  showing shopping offers. Reject known color/style/market mismatches and keep unsupported results
  explicitly unverified. Do not promote legacy search-only offers to verified results.
  Prefer distinct merchants; inspect at most five saved candidates with at most three page fetches.
  Skip additional URLs of an already blocked host without consuming a fetch slot.
  Preserve unread candidates and the reason they were not checked.
- Always offer a zero-model-call shopping handoff built from an observed SKU/brand or a
  human-reviewed brand plus visible garment attributes. Keep Google Shopping, Bing Shopping
  and web-search links labelled as external and unverified. Paid AI discovery remains opt-in.
- When no verified offer is available, provide an explicit manual image-search handoff:
  download an eligible original cutout/context from the same job and open the official Google Lens
  UI separately. Never auto-upload, use a synthetic preview or offer this for generated sources.
  External results remain unverified until separately checked; this is not a backend Lens integration.

## Files and ownership

- Integrator: `wardrobe/config.py`, `schemas.py`, `store.py`, `pipeline.py`,
  `app.py`, `__main__.py`, project configuration, integration tests and README.
- Model worker: `wardrobe/providers.py`, `wardrobe/media.py`, related provider
  tests and existing `poc/` repairs. Do not alter original `out/` results.
- UI worker: `wardrobe/static/` only, including HTML/CSS/JS and local SVG icons.

## HTTP contract

All paths share one origin. Responses are JSON unless a file/export is requested.
Errors have `{ "detail": "human-readable message" }`.

### Hosted authentication and storage

`rewear --hosted --public-origin https://… --hosted-data-dir …` creates a separate
account store and per-account child application with a fixed Store/media root. It
does not migrate or expose the default local catalogue. The root and tenant locks
require a single process; proxy and backup instructions are in `HOSTED_OPERATIONS.md`.

`/login`, static assets, `/healthz`, and unauthenticated auth endpoints are public.
Anonymous `/` redirects to `/login`; protected APIs/media return 401. Cross-account
IDs are looked up only in the current account's store, so another account's record
or image is 404. Protected responses include `Cache-Control: private, no-store`,
`Vary: Cookie` and the server-selected `X-Rewear-Account` (including 204 responses).

All mutation requests require the exact configured `Origin` and `X-Rewear-Request: 1`.
Authenticated mutations also require the current session's `X-CSRF-Token`.
Auth bodies are JSON, maximum 8 KiB. Passwords/recovery codes and CSRF/session values
are never persisted in browser storage. Cookie sessions are HttpOnly/SameSite=Lax,
expire after 8 hours and use Secure plus a `__Host-` name for HTTPS.

- `GET /auth/session` → `{mode:"hosted",authenticated:false}` or
  `{mode:"hosted",authenticated:true,user,csrf_token}`. Local mode returns
  `{mode:"local",authenticated:false}` and provides no account API.
- `POST /auth/register` `{username,password,display_name?}` → authenticated session
  payload plus once-only `recovery_code`, **200**, and session cookie. Account and
  session creation are one transaction; failure cannot leave an account without its recovery code.
- `POST /auth/login` `{username,password}` → session payload and cookie, 200.
- `POST /auth/logout` → 204, revokes the current session and removes its cookie.
- `POST /auth/password` `{current_password,new_password}` → replacement session and
  cookie, 200; revokes all older sessions. Wrong current password does not revoke the valid session.
- `POST /auth/recover` `{username,recovery_code,new_password}` → new once-only recovery
  code, 200; consumes the old code and revokes all sessions. It does not log in automatically.
- `GET /api/storage` → `{mode,limits:{jobs,media_bytes},usage:{jobs,media_bytes,
  reserved_jobs,reserved_bytes},cleanup:{pending_directories,error},notice}`.
- `POST /api/storage/cleanup` → the same snapshot after retrying only committed
  deleted-job directories belonging to this account.
- Hosted `DELETE /api/jobs/{id}` remains 204 and adds `X-Rewear-Cleanup: complete|pending`.
  Pending files continue to count against capacity, including after restart. Local deletion
  retains its original trash behaviour.

Defaults: 1,000 accounts, 16 simultaneously loaded wardrobes, 200 jobs and 2 GiB
media admission per account. Shared global WorkBudget permits 64 pending/running
tasks, one executing task per analysis/reconstruction/discovery kind. References
held by streaming requests and background tasks prevent eviction of an active tenant.
Account-capacity exhaustion uses 429 (saved jobs) or 507 (media); reads, exports,
cancellation, deletion and idempotent replay remain available. This is estimated
admission control, not a filesystem quota; details and exclusions are documented in
`HOSTED_OPERATIONS.md`.

### Wardrobe API

- `GET /api/health` → `{status, local:{available,model,error},
  meta:{configured}, reconstruction:{available,automatic_outfit,model},
  discovery:{available,model,markets}, ffmpeg:bool,
  limits:{upload_mb,video_seconds,max_items,batch_files,batch_upload_mb,max_queue}}`.
  Responses may reuse a ten-second provider-health cache. `?refresh=true` performs a fresh check;
  the five-second deadline includes waiting for the health-check lock.
  A selected local provider is checked again before creating an upload, batch, generated-image
  job or retry. Unavailable or unknown local health returns 503 without creating a job or sending
  the image to Meta. Explicit Meta selection does not wait on local health. Batch replay of the
  same accepted bytes/options remains idempotent while the local provider is unavailable.
- `GET /api/jobs` → `{jobs: Job[]}` (newest first).
- `POST /api/jobs` multipart: `file`, `provider=local|meta`,
  `mode=single|rack|outfit`, `concept=garment`, optional JSON `person_box`,
  `reconstruct=true|false` (text, defaults to true), `discover=true|false` (defaults false),
  `discovery_market=TW|global` (defaults TW) → Job, HTTP 202.
  Auto-discovery is outfit-only; the UI explicitly enables it by default for outfit uploads.
  Person selection is normalized `[left,top,right,bottom]`, outfit images only.
- `POST /api/batches` multipart: repeated `files`, `provider`, `mode`, `concept`,
  optional `selections` JSON array (one rectangle or null per file), `client_id`,
  `reconstruct=true|false`, `discover=true|false`, `discovery_market=TW|global` (same defaults) → Batch (202).
  The replay ID binds filenames, file contents, mode/provider/concept, selection, reconstruction and discovery options;
  reuse with changed inputs is 409. Invalid files have `{index,filename,error}` entries.
- `GET /api/batches` → `{batches: Batch[]}`; `GET /api/batches/{id}` → Batch.
- `POST /api/batches/{id}/cancel` → Batch; cancel active analysis, reconstruction and discovery tasks.
- `POST /api/batches/{id}/retry` → NEW Batch (202); failed/partial/cancelled jobs only,
  preserving source bytes, provider and selection. Active batches are 409.
- `GET /api/jobs/{id}` → Job plus `items: Item[]`.
- `POST /api/jobs/{id}/cancel` → Job.
- `POST /api/jobs/{id}/retry` → a NEW Job (202), keeping old results.
  A generated-image job reuses its saved source when available; a local single-image retry of
  those saved bytes does not require Meta credentials. The job is re-read after any provider
  health wait so a concurrent deletion cannot resurrect it or start image generation.
- `DELETE /api/jobs/{id}` → 204; refuse active analysis jobs (409), stop reconstruction and discovery before moving media.
- `POST /api/demo` → completed Job imported from historical `out/t5_glimmer_local.json`;
  idempotent until the demo job is deleted. Clearly labeled sample, no new inference.
- `POST /api/generate` JSON `{prompt,provider,mode,concept}` → queued Job (202).
  Image generation requires Meta; provider selects the subsequent extraction model.
- `GET /api/items?q=&category=&review=all|pending|reviewed&quality=all|ready|needs_confirmation|needs_retake|unassessed&job_id=`
  → `{items: Item[]}`.
- `PATCH /api/items/{id}` JSON `{record?:partial Garment,reviewed?:bool}` → Item.
- `GET /api/items/{id}` → Item (including current reconstruction and discovery state).
- `POST /api/items/{id}/reconstruction` → Item (202); optional JSON `{reference_item_ids:[id,id]}`.
  Missing/empty body preserves single-photo behaviour. Up to two distinct other IDs must resolve
  to eligible original image/video observations in the same tenant and confirmed garment group.
  No client-supplied paths/URLs or synthetic/shopping references are accepted. Identical selections
  are idempotent while active; changed selections return 409. Invalid shapes/IDs return 422.
  Known category and ready/needs_confirmation or human-reviewed record required (422);
  missing/invalid original references or deleting record 409, provider unavailable 503, full queue 429.
- `POST /api/items/{id}/reconstruction/cancel` → Item; waits for local cleanup, inactive attempt is unchanged.
- `POST /api/items/{id}/discovery` JSON `{market:"TW"|"global"}` (optional, default TW) → Item (202).
  Idempotent while the same item's same-market attempt is active; changed market is 409.
  Requires a known category and ready/needs_confirmation or human-reviewed record, original image references,
  and a non-generated source job (422); unavailable 503, deleting 409, queue full 429.
- `POST /api/items/{id}/discovery/cancel` → Item; waits for local task cleanup, retaining previous results.
- `DELETE /api/items/{id}` → 204; blocks new reconstruction/discovery and waits for both before deleting.
- `GET /api/duplicates` → `{suggestions:[{id,item_ids,score,reasons,human_required}],notice}`.
  Local heuristic, not a probability. Newest 500 eligible records, cross-job only,
  existing-group pairs excluded, default 40 suggestions / maximum 3 per item.
- `GET /api/garment-groups` → `{groups:[{id,primary_item_id,item_ids,created_at,updated_at}]}`.
- `POST /api/garment-groups` JSON `{item_ids,primary_item_id}` → Group (201).
  Atomically unions existing groups; at most 128 members. Requires ready or
  needs_confirmation input records, never marks them reviewed or rewrites them.
- `DELETE /api/garment-groups/{id}` → 204; removes membership only, audits previous state.
  Group changes and source deletion first cancel and await affected cross-job reconstruction tasks.
  Finished images retain their own source history even if a reference is later deleted.
- `GET /api/export?format=json|csv&job_id=` → downloadable file, all items or one job.
  JSON includes `groups` and each item's `reconstruction,discovery`; CSV includes `garment_group_id,primary_item_id`,
  `reconstruction_status,reconstruction_url,reconstruction_is_synthetic,reconstruction_note`,
  `reconstruction_preview_url,reconstruction_preview_status,reconstruction_preview_inferred_areas,
  reconstruction_preview_warnings`, and
  `discovery_status,brand_candidate,brand_evidence_status,brand_evidence,brand_confidence,product_code,
  shopping_status,shopping_market,shopping_checked_at,shopping_verification_version,shopping_urls,
  shopping_exclusions`. Shopping URLs include only version 2 verified offers; JSON preserves legacy data.
  All text is formula-escaped. A job-filtered
  export keeps full membership IDs of intersecting groups but only that job's item payloads.
- `GET /media/{job_id}/{filename}` → saved image/video, scoped to that job.

Job fields: `id, filename, media_type, provider, mode, concept, status, stage,
progress, created_at, updated_at, source_url, preview_url, overlay_url,
item_count, usable_count, retake_count, confirmation_count, unassessed_count, warnings[], error, is_demo`.
Outfit jobs add `selection_url,selection_bbox` when selected, and persist normalized
`options.person_box`; `options.reconstruct` is persisted as a boolean for all uploads and retries.
Automatic reconstruction applies only to eligible outfit results with a configured provider.
Batch jobs add `batch_id,batch_index`; retries identify `retry_of`.
`item_count` includes all analysis regions, not a count of complete garments.
Counts and quality summaries are derived from current records after review/deletion.
Statuses: `queued,running,completed,partial,failed,cancelled`.

Item fields: `id,job_id,created_at,image_url,cutout_url,source_url,original_url,frame,object_id,bbox,
record,model_record,quality,reviewed,provenance,error,visual_signature,reconstruction?,discovery?`.
`image_url` is scene context for segmented results; `cutout_url` preserves the
visible-pixel mask crop; `source_url` is the actual normalized segmentation source
(selected rectangle when present), and `original_url` retains the full normalized image.
`quality` stores status (`ready/needs_confirmation/needs_retake/unassessed`), effective and model
visibility, reasons and geometry metrics. Version 2 added `model_target`:
`one_garment / multiple_garments / no_garment / uncertain`. Only a confirmed single
clothing target with a complete view is usable by default. Version 3 adds
`capture_mode=outfit`: single, partial, known-category garments with confidence >=0.5
and no sparse-edge failure retain visible attributes as needs_confirmation, never
as usable until reviewed. Versions 1 and 2 remain readable without rewriting user data.
Human confirmation overrides catalogue eligibility, never the underlying quality evidence.
Garment fields match `poc/garment_schema.json`; confidence is 0..1;
unreadable fields stay null. Low confidence requires review. `reviewed` records
human confirmation separately from original model confidence.
`model_record` preserves validated model fields before quality suppression, with
`needs_review` conservatively normalized; it is not raw model response text.
Reassessment preserves this initial record and separately saves `reassessed_model_record`.

`reconstruction` contains `id,status,image_url,error,model,is_synthetic,note,created_at,updated_at,
reference_urls,reference_record,reference_item_ids,reference_manifest,preview_url,preview_validation`.
Each retained image also has its own `image_reference_manifest`, `preview_reference_manifest` or
`candidate_reference_manifest`, and its bound validation plan contains the same manifest. The UI
never substitutes a later attempt's sources for an earlier image. Typed named sources include
`cutout`, optional `context`, and up to `extra_1,extra_2`, with item/job IDs, server-resolved media URLs,
raw-file and normalized-image hashes, and a group membership hash. They are exported with JSON.
Missing historical sources are disclosed; backup permits a missing manifest source only if its
job no longer exists in that same store. Current-job media and generated outputs remain required.
Status includes
`queued,running,completed,needs_review,rejected,failed,cancelled`.
Generated files are separate `jobs/{job_id}/reconstructions/{attempt_id}.png` images.
Retries preserve the prior image until a new image is validated and committed. Updating this state never
changes original observations or feature vectors. Strict accepted images require a validated fidelity
report. A separate preview requires explicit category, coarse color, single garment, no person/extras,
and full-outline checks. Side views and unknown hems may generate inferred previews but retain strict
uncertainty and visible mismatches. `preview_validation` is the full report bound to that image;
legacy reports without preview evidence cannot qualify. The UI prioritizes strict accepted image,
then validated preview with inference badge, then original evidence, and
offers explicit original/context/cutout tabs; duplicate comparisons continue using source evidence.
The independent queue allows 64 attempts and one active model call. Attempt-ID guarded updates prevent
stale output or deleted records from reappearing. Startup marks interrupted attempts failed, clears their
uncommitted output and retains previous images; it never resends a model call automatically.
Initial source planning selects at most three detail regions. The same independent source-only review
also receives upper/middle/lower crops made from original pixels and may add at most two named regions.
The refined plan and candidate comparison cover up to five regions; the initial three-region plan and
its uncertainty obligations remain intact. Body hem, sleeve cuffs, collar and small tabs are separate
evidence locations. There is still one source-detail request, at most two generated images and a
240-second attempt budget. No source survey or detail crop is synthetically generated.
Image corrections receive only typed failed check/feature identifiers and indexes of clear,
failed original regions. Free-form audit correction text remains in the saved report but is not
executed as an edit instruction. Existing ambiguity, full-image re-audit, two-image cap and
non-regression selection stay binding. Open garment fronts reveal the neutral background;
the original wearer's inner shirt, jewellery and body are excluded from the reconstruction.

`discovery` contains `id,status,stage,market,error,model,created_at,updated_at,reference_record,result`.
Status is `queued,running,completed,partial,failed,cancelled`; active stages are `identifying,searching,verifying`.
`result` holds `identity,shopping,market,identified_at,reference_record,source_urls,search_links`.
Result market and saved fields belong to that result, even while a retry uses different inputs.
Identity includes `status:observed|possible|unknown,brand,evidence_type,visible_text,evidence,confidence,
product_code,search_terms,visual_profile`. The nullable visual profile records visible type/category,
primary fabric color, pattern, sleeve length, neckline and description separately from brand evidence.
Unknown clears brand/text/code and confidence; label/wordmark observations
require transcribed text, and untranscribed product codes are removed. Confidence is a model score,
not measured accuracy. The original `record.brand`, model record, quality and review state never change.

Shopping contains `version,status:searched|no_results|unavailable,query,offers,sources,searched_at,notice,
checked_candidates`. Version 1 stores legacy search-only evidence, never rendered as verified offers.
Optional `search_attempts` retains the bounded precise/broader query outcomes. Automatic discovery
only enqueues eligible original-photo items without existing discovery state; retries retain explicit
upload options, and no service restart automatically repeats paid work.
Version 2 considers at most five saved candidates, with at most three actual product-page fetches.
Blocked-host skips do not consume another fetch slot; every skipped or budget-limited candidate
retains a reason. Verified offers still require actual product pages and images.
Offer fields are `title,url,merchant,match,reason,verification`; title comes from the retrieved product page.
Verification includes `status:verified,page_url,page_checked_at,market:in_scope,market_evidence,
availability:in_stock|unknown,comparison,image_url,product_code`. Comparison records `match|mismatch|
uncertain|not_applicable` for category, primary_color, pattern, sleeve_length, neckline and design_details.
Known attributes must match; uncertainty cannot pass. Original category and primary color are required.
`model_candidate` requires a complete readable original SKU matching the actual page SKU plus visual
agreement; all other accepted offers are `similar`. Neither proves the exact item or authenticity.
`checked_candidates` stores `title,url,status:accepted|rejected|unverified,reasons[]`; exclusions are visible.
Stock is page-declared only. Out-of-stock results are excluded; missing availability is explicit unknown.
TW requires a Taiwan product locale or tightly bound current Shopline merchant/product evidence:
matching current path, product SKU/ID, product owner, merchant ID and storefront host, merchant country TW,
and both merchant and product currency TWD. Currency or request-country hints alone do not qualify.
Explicit foreign locale takes precedence; otherwise unknown market is excluded.
This establishes the storefront claim, not shipping or checkout availability. Global accepts public locales.
Search sources remain `title,url,snippet`; search links are labeled query entry points, not verified products.

Discovery supports manual requests and opted-in outfit uploads, with its own 64-attempt queue and
one active operation. Brand inference receives
original image references; search receives text, while verification compares the original and product images.
Reconstruction and shopping files, including symlink targets, and generated source jobs are rejected as
original evidence. Product images are saved under `jobs/{job_id}/shopping/{attempt_id}/{1..3}.png|jpg`
only after validation. Cancelled, stale or interrupted attempts remove uncommitted images; prior results
retain their own images. Search failure after identification retains a partial result;
early failure/cancellation retains the prior result. Attempt-ID guarded updates coexist with edits and
reconstruction. Startup marks interrupted work failed without new API calls. Deletions guard both
supplemental queues until database/media removal or rollback completes.

## Provider/media contract (async)

`Providers(settings)` exposes:
- `health() -> dict` (local availability; Meta configured; do not expose keys).
- `extract(path: Path, provider: str) -> dict` for legacy direct extraction.
- `inspect(path: Path, provider: str, *, context_path: Path|None=None, wearing:bool=False)
  -> {garment,target,visibility,reason}` for the application pipeline. All four
  fields are required. Target is `one_garment/multiple_garments/no_garment/uncertain`;
  visibility is `complete/partial/fragment/unclear`, independent of attribute confidence.
  Any target other than `one_garment` needs a reason and cannot become usable merely
  because its outline is complete. For segmented inputs, surrounding neighbours
  do not change the selected target into multiple garments.
- `segment(path: Path, concept: str, video: bool=False) -> list` of normalized SAM dictionaries
  with `kind,object_id,frame,left,top,right,bottom` or `mask,bounds` fields.
  Video frames are zero-based integers; image frames are null.
- `generate(prompt: str, destination: Path) -> None`.
- `reconstruct(cutout_path: Path, context_path: Path|None, record: dict, destination: Path, *,
  source_bundle: ReconstructionSourceBundle|None=None) -> dict` (validated reconstruction report).
  Sends actual image references to Meta `/images/edits` using `muse-image-1.0`, without text-only fallback.
  Requests a complete single garment on a plain background. Hidden shape/pattern details remain inference;
  reference text and catalogue hints are untrusted, and must not override the generation instructions.
  Sources remain unchanged; output is validated, bounded and atomically saved to a distinct path.
  The optional server-owned bundle is read once, hash-checked, and used consistently by planning,
  ROI extraction, generation and global/regional review. Extra source IDs are explicitly named;
  adding references cannot silently replace the primary context. The same-source hashes and group
  membership are rechecked before committing output. Old four-argument providers remain compatible
  with the default single-photo path. See `MULTI_PHOTO_RECONSTRUCTION.md` for the bounded real control.
- `identify_brand(cutout_path:Path, context_path:Path|None, record:dict) -> BrandIdentity`.
  Muse Spark 1.3 strict structured vision output, using only category/subtype/colors/pattern hints,
  never the existing brand field. Brand claims require original visible label, wordmark or logo evidence.
- `search_shopping(identity:dict, record:dict, market="TW") -> ShoppingResult`.
  Muse Spark 1.3 Responses web search, with raw search results requested. An offer URL must be present
  in completed search results; ungrounded generated URLs are excluded. Citations can prioritize evidence,
  but are not required because the provider can omit them in strict JSON output. Sources are limited to
  selected offers and actual cited hits; raw titles/snippets are not claimed to be verified page contents.
  Only public HTTPS links are eligible; these are internal candidates until `verify_shopping` completes.
- `verify_shopping(identity,record,shopping,cutout_path,context_path,destination,market="TW") -> ShoppingResult`.
  Fetches up to three actual product pages/images and uses strict Spark image comparisons. Rejected and
  unreadable results keep specific reasons, and a single candidate failure does not drop other results.
  The entire verification stage is bounded by request_timeout; cancellation propagates through cleanup.
- `product_evidence.fetch_product(url,market="TW") -> dict` retrieves public HTTPS metadata and static images.
  Validated public IPs are pinned per request with original TLS SNI/Host; redirects revalidate DNS.
  No stored cookies, authentication or proxy environment is used. Page/image bodies, decompression,
  pixel count, redirects and total time are bounded. Login, challenge and rate-limit responses stop reading.
  JSON-LD Product/Offer or explicit product OG metadata is required; search/category pages are rejected.
  No search execution yields unavailable; completed search without grounded candidates yields no_results.
  Provider reference: [Meta search grounding](https://dev.meta.ai/docs/search-grounding) and
  [structured output](https://dev.meta.ai/docs/structured-output); actual response behavior is recorded in
  the discovery verification reports, including strict JSON without citation annotations.
- `close() -> None`.
Use `ProviderError` with a safe message for expected upstream failures. Finite
timeouts and cancellation are required. Never substitute cached inference.

Settings supplies `data_dir:Path`, `ollama_url:str`, `ollama_model:str`,
`meta_url:str`, `api_key:str|None`, `request_timeout:float`,
`max_upload_mb:int`, `max_video_seconds:int`, `max_items:int`, `max_queue:int`,
`max_batch_files:int`, `max_batch_upload_mb:int`.

`media.py` exposes synchronous helpers (called with asyncio.to_thread):
- `prepare_image(source,destination) -> dict(width,height)`; EXIF normalization,
  RGB JPEG, bounded pixels, max dimension 1600, reject malformed images.
- `crop_selection(source,person_box,destination) -> dict(width,height,bbox,normalized_box)`;
  selection refers to the EXIF-normalized preview, no source overwrite.
- `prepare_video(source,destination,max_seconds) -> dict(width,height,fps,frames,duration)`;
  ffprobe validation; reject overlong clips; normalize to <=640×360, 5 fps, H264.
- `video_frame(source,destination,frame,fps) -> None`.
- `crop_records(image:Path, records:list, destination_dir:Path, frame:int|None=None)
  -> list[dict(path:Path,context_path:Path,metrics:dict,object_id:str,bbox:list[int],frame:int|None)]`;
  clipped coordinates, dedup, optional masks, max returned count enforced by pipeline.
- `context_crop(image_path,bbox,out_path) -> Path`: scene crop with 20% padding
  and a thin target box; source pixels remain intact in the original file.
- `crop_metrics(image_path,bbox) -> dict`: geometry for older jobs; missing mask
  metrics stay null rather than being guessed from a JPEG.
- `render_overlay(image:Path,records:list,destination:Path,frame:int|None=None) -> None`.
- `render_video_overlay(source:Path,records:list,destination:Path,metadata:dict) -> None`.
Use `MediaError` for safe validation errors. Intermediate files stay inside job
directory. Never shell=True; ffmpeg/ffprobe must have bounded runtimes.

New jobs persist full normalized SAM records, including masks, for reproducible
processing. A sparse mask (<35% of its box) touching the original frame edge
conservatively contradicts a model's `complete` classification. Narrow shapes
alone never trigger rejection. This is a heuristic, not a measured accuracy claim.
