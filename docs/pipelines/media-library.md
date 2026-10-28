# Creator media library

KRI-384 adds a creator-owned catalogue behind `MEDIA_LIBRARY_ENABLED=false`.
Collection proposals have a separate `MEDIA_COLLECTION_PROPOSALS_ENABLED=false`
capability. Backend ingest and reuse are additive. iOS exposes **Saved uploads**
inside Add media for footage, visuals/slides, voiceovers and songs when the native
`media_library` creation capability is true. Collection proposals remain a later
slice. Keep both flags off until native evidence and pilot approval are recorded.

New native uploads are catalogued automatically while the library flag is on.
Uploads made before activation are not automatically backfilled. Operators can
adopt retained, provably owned uploads with `scripts.backfill_media_library`
(below); importing them again also uses the verified intake. The picker supports filename
search, pagination, analysis retry, archive, and restoring expired cloud copies
from the exact original. Archive hides future selection and preserves posts.

Videos retain full-quality originals under account-scoped Application Support.
A new post gets an independent media ID and original-file link, with copy fallback.
Originals survive deleting the first post, relaunch, and sign-out. A different
account cannot browse them; confirmed account deletion erases the library's local
copies. Existing post-local originals are migrated lazily using server-authorized
usage and a full-file hash. Another phone must locate the matching original for
device-only videos; proxies never become render sources. Cloud-backed originals
are downloaded and verified when needed. Reuse performs no upload; cloud repair
is an explicit action and uploads only the required proxy/original.

Native proof and rollout checklist: `docs/reviews/kri-384-ios/verification.md`.
After deployment and pilot approval, enable `MEDIA_LIBRARY_ENABLED` on API and
workers and release a new TestFlight build containing the picker. Keep
`MEDIA_COLLECTION_PROPOSALS_ENABLED=false`. Turning the library flag off hides
Saved uploads after capability refresh and returns its routes to 404, while
existing post bindings and original files remain intact.

## Historical uploads

From `src/apps/api`, run `python -m scripts.backfill_media_library --email EMAIL
--report /tmp/library-dry-run.json` for a metadata-only preview. After reviewing
the scoped account, availability, ordering and byte count, repeat with `--apply`.
`--max-bytes` defaults to 1 GiB of attempted downloads per run; a run stops at the
budget or the existing 500-active-asset quota. Repeating it preserves completed
work. The limit applies after exact-byte deduplication. It scans at most 5,000
candidate identities by default (`--max-candidates`, maximum 10,000).

Candidates come from owned plan-item footage/audio and registered visual pools.
Upload events/pool timestamps determine newest-first priority, with post creation
time as the legacy fallback. Old unpinned sources adopt the current generation
only after full-byte verification and probing; a changed existing generation is
skipped. Videos require their stored device-bound proxy/original receipt for the
current phone-only creation flow; older cloud-render video originals are skipped
and remain available on their existing posts. Both the database
consumer and the normalized storage namespace must belong to the requested owner.

Each transaction takes the existing owner/reference fence, rechecks the current
consumer and source, and adds catalogue identities and usage references only.
It never rewrites posts, copies/deletes cloud objects, revives archived media or
dispatches AI analysis. Verified bytes are ready to reuse; compatible facts remain
independent and are read through the existing versioned cache. Native device-only
videos still require their matching full-quality originals on the phone. Existing
thread usage lets the shipped iOS client migrate those originals lazily.

The report separates missing, changed, unsupported, unowned, archived, quota and
transfer-budget cases. Keep account identifiers and detailed reports outside Git.
Verification contract: `docs/reviews/kri-384-backfill/verification.md`.

## Upload and repair

Authenticated iOS clients reserve through `POST /me/media-assets/uploads`, upload
with the returned create-only headers, then finalize through
`POST /me/media-assets/uploads/{reservation_id}/finalize`. Reusing a
`client_upload_id` with the same request preserves the reservation; a changed
request is rejected. Finalized reservations never yield another upload URL.
Registration commits before analysis publication. A maintenance reconciler
recovers outstanding publication receipts and abandoned processing.

New phone videos upload analysis proxies with device-declared original hashes
and byte counts recorded separately. Photos and supported songs upload originals.
Finalization downloads the pinned generation, verifies the full uploaded hash
and length, and checks the actual media. Device declarations never prove the
uploaded-byte identity. No automatic video-original backup is added.

A creator can explicitly give a device-only video its full original (KRI-822):
adding it to Visuals, or restoring it from Files for Visuals, uploads the
original on that iPhone. The request sets `attach_original: true` with the
owned `repair_asset_id`, `purpose: cloud_render_source` and no proxy
(`video/mp4` or `video/quicktime`, up to 4 GB). Reserve rejects a byte count
that differs from the asset's `original_byte_count`; finalize rejects any
sha256 other than `original_declared_sha256`. Only then is a new `original`
source added. The asset keeps `original_availability: device_only` and its
stored proxy contract, so proxy repair and phone Footage behave as before; the
available `original` row is what makes it usable in Visuals. The analysis proxy
row is never read as, or turned into, the original. Finalize streams the
pinned generation through sha256 and probes it with ranged reads, so a multi-GB
original is never staged on the API machine's RAM-backed `/tmp`.
Attaching an original never resets the asset to `pending` and never dispatches
library analysis: a device-only video's status, facts and analysis stay on its
`analysis_proxy` (analysis and catalogue facts prefer that role), and the list
serializes the proxy before the original so older clients still bind it for
Footage.

A retry with the same `client_upload_id` (`ios-original-<asset>-<sha>`) returns
the same source. That identity is fixed, so reserve re-opens it with a fresh
target when its old target was cleaned up, or when its finalized original is no
longer in storage (the stale source is marked `missing`); iOS then uploads and
attaches the fresh copy. Phone Footage binds only the analysis proxy of a
device-only video; Visuals bind the original (`project_library_visual` prefers
it when no source id is given). Only the "Cloud copy is unavailable" 409 sends
iOS to repair; other 409s show the server's message.

Saved uploads keeps the newest 500 active catalogue assets per creator. A verified,
unique import atomically archives the oldest active entry by upload/admission time
(`created_at`, then `id` for ties). Reusing an old upload does not reset its age.
Rotation hides it from future library selection; sources, reusable facts, local
originals and existing post references remain intact. Existing posts keep working
under the normal storage retention rules. No physical deletion runs during rotation.

Reservations do not displace saved items. Up to 500 unfinished uploads are allowed
independently of the active catalogue; failed or abandoned imports do not rotate it.
Owner locking serializes admission and duplicate finalization. An attachment that
rolls back also rolls back its archive changes. Duplicate imports and repairs do
not consume another slot. Existing per-file and per-post limits still apply. Compatible imports coalesce
after verifying the canonical cloud copy. Matching reimports can add a fresh
source to an existing catalogue asset after cloud expiry. To repair at capacity,
pass `repair_asset_id`: original bytes must match their verified identity, and a
video proxy must preserve its stored original binding and proxy contract.
Outstanding repair uploads are also bounded.
New selections prefer the newest available source after repair. Existing posts
keep their explicit source generation; old copies become missing only after a
definitive storage check. Embedded song cover art remains supported.

## Selection and reusable facts

`GET /me/media-assets` supports cursor pages, filename search, kind, processing
state and owned collection filters. Detail includes availability, prior usage and
current expiry warnings. The source access URL endpoint signs the exact generation.
Missing or replaced objects become unavailable; transient storage errors remain
retryable. Retry and archive are explicit POST operations on the owned asset.

Thread, visual-pool and editor attachments accept library asset/source IDs.
The server resolves ownership and immutable sources. Each post retains independent
media identifiers, timelines and editor state. Visual projections require
originals; phone footage keeps an original receipt behind its analysis proxy.
Existing native uploads are registered through the same byte verification. A
duplicate consumer binds the canonical path/generation, with an expiry receipt
for the unused uploaded copy.
If duplicate attachment rejects a whole batch, post changes roll back and a
separate owner-serialized transaction records every abandoned physical upload.
Library-ID-only rejections create no upload receipts. Cleanup rechecks existing
post and editor bindings before claiming bytes. Once claimed, database reference
guards also fence legacy post writers; a failed storage deletion stays fenced and
retryable. Renewing an unused upload URL extends its cleanup deadline atomically;
claimed, deleted or already-bound targets cannot receive a fresh PUT URL.
Native visual binding checks generation, full hash and byte count. Proxies cannot
replace originals during rendering.

Video/image facts use existing analyzers and `MediaAnalysisCache` tiers.
`MediaAssetAnalysis` persists compatible facts beyond temporary cache expiry.
Identity includes creator, verified bytes, kind/role and analyzer, model, prompt
and schema versions. Durable leases coalesce concurrent work and reject late
completions. Transcripts and song beats/lyrics reuse existing transcription and
beat detection; the song seam also supports KRI-425. Library transcription uses
the existing cost ledger with duration pricing and no hidden SDK retries. Cache
hits make no provider call. A live lease remains processing; failed analysis is
retryable. Creative interpretation, corrections and take alignment stay per post.

## Camera-roll collections (KRI-729–732)

The native collection flow reuses catalogue/source identities and local-original bindings.
`PUT/GET /me/media-collections/{id}` saves at most 50 added assets; every member is eligible
without a second selection. Index passes contain at most 32 sources. Photos identifiers
and raw capture metadata remain on device, with correctable event groups and saved progress.
Explicit import uses the existing catalogue ingest contract. No whole-library upload occurs.

`GET /{id}/moments` and transcript-range reads use complete source-timed cached facts.
`POST /{id}/semantic-search`, `/inspect` and `/stories` use bounded shared agents/fact leases.
Candidate selection considers all collection sources, round-robin by each source's relevance;
100 intervals enter a model call. Exact interval selection/approval is checked independently
of that candidate projection. Availability requests run in batches of ten, up to 50 sources.
Partial coverage and uncertainty remain explicit. Still images have no invented video timing.

Unchanged canonical collection saves are no-ops, including identical stale snapshots;
actual competing edits still require the current revision. Collection failures carry
scoped codes (`collection_changed`, `collection_media_unavailable`,
`collection_analysis_unavailable`, `collection_analysis_pending`) so native discovery
never mistakes an unavailable cached analysis for an editor change.

The native collection keeps footage review before idea selection (KRI-853). Optional
direction/research/grouping live in options. Account-scoped snapshots retain the exact
selection, query, explicit partial-analysis choice and last reviewed result;
**View saved ideas** revalidates the selection,
source availability and cached response without new model work. Selection/name/source
changes invalidate the local shortcut. Opening a genuinely newer server collection is
explicit and retains the previous local state as a separate collection. Legacy local
snapshots without saved-idea context remain readable.
Partial-consent reuse requires the same excluded-video IDs, not merely the same count;
changed coverage offers a fresh ready-video choice. Catalogue capacity remains 500;
selected-story rendering keeps its separate bound.

The native Camera roll is a full-screen surface in the projects drawer, not a set of sheets (KRI-944).
Footage is a contact sheet grouped by event; every tile renders one `CameraRollItemState`
(queued, uploading, analyzing, ready, needs retry, failed, excluded) from `GET .../items`, and
**Find ideas**, **Another idea**, delete idea and delete event all work per event (`group`).
Ideas persist per event on the server; the phone caches them per event (`ideasByGroup`) and re-reads
`GET .../stories?group=` after any membership change. A rename changes only the local title, never the
server group key, so it cannot orphan ideas; moving items between events does clear both events' ideas
(the server drops what it can no longer stand behind) and the surface says so. Collections saved before
this kept one `savedIdeas` object; that key is ignored on read, so the collection loads and its ideas come
back from the server. Offline fixtures: `-camera-roll-collection -collection-mixed-states` (and
`-collection-empty`, `-collection-access-notices`); under the real drawer, `-ui-testing-chat
-camera-roll-surface-fixture`.

Each idea carries an additive `content_plan.form` (`guide`, `factual_story`, `atmosphere`,
`review`, `performance`). `FORM_EVIDENCE` in `schemas/media_collections.py` lists what a form
needs: `guide` three distinct source clips, `factual_story` a selected research fact, the rest
nothing. `CameraRollStoriesAgent.parse` requires a known form, drops an idea whose form lacks its
evidence; three surviving ideas that share one form are kept (a narrow request legitimately
yields them) and logged. `STORY_IDEA_FORMS_ENABLED=false` turns all of this off
(rollback lever; procedure in DECISIONS.md "Idea forms"). Saved stories from before the field have no `form` key; `StoryContentPlan`
stays version 1 and omits a null form when serializing, so stored JSON, digests and approval
bindings of those rows are byte-identical. A wording edit keeps the stored form (builds that
predate the field send none back).

Idea copy is written for a stranger scrolling a feed (KRI-1004). The prompt prefers guide,
review, factual and performance ideas over `atmosphere`, makes the opening a concrete hook and
keeps first-person day recaps out of captions. It honours the event's title and notes
(`creator_provided_event_context`): a named place goes into each idea, and a stated aim such as
"why people should visit" allows why-visit ideas built on features the footage shows. The
prompt starts with the `grounded-hook` runtime skill; an edit to that skill needs a
`camera_roll_stories` `prompt_version` bump, because the idea cache is keyed on the version.
After the ideas parse, `CameraRollStoriesAgent.run` sends their title, opening, closing and
caption to `CameraRollCopyEditAgent` (`app/agents/camera_roll_copy_edit.py`), one extra Flash
call. That model quotes stock travel wording ("Experience ...", "full of charm", "perfect for
a peaceful getaway") and rewrites the line from the idea's own footage descriptions and the
creator's words. Code applies an edit only if its quote is really in the line and
gone from the rewrite, and the rewrite fits the field; hook and payoff follow the new wording.
Any failure (timeout, bad JSON, budget, shared deadline) keeps the draft ideas. Researched ideas
skip the pass, because their two review calls share the request deadline after it. The edit
prompt's identity is part of the idea cache digest. Measure voice
changes with `tests/evals/test_camera_roll_voice_evals.py` (DECISIONS.md "Camera-roll ideas
are written for a stranger").

An idea may carry up to three `content_plan.open_questions` (`id`, `question`, `answer`,
`dismissed`): facts only the creator knows. The question text is server-owned. The creator
answers or skips through the existing `PATCH .../stories/{id}` wording edit (a new story
revision); `resolve_open_questions` merges only `answer`/`dismissed` by id, rejects unknown ids,
and keeps the stored list when a build that predates the field sends none. `select_story`
answers 409 `story_open_questions` while any question is neither answered nor skipped, but only to builds
declaring `X-Kria-Client-Protocol` >= `OPEN_QUESTIONS_MIN_CLIENT_PROTOCOL` (22); older
builds cannot answer and build the neutral-copy idea (replays of an already-accepted selection
are not re-checked). The prepared story, its digest and planning diagnostics omit
`open_questions`: answers stay on the story row only. Legacy rows have no key and build as
before. `story_content_digest` and the research review prompt ignore the list, so answering
never stales a research review. `CAMERA_ROLL_STORY_EXTRAS_ENABLED=false` stores new ideas without
`form`/`open_questions` and stops the gate. Rollback of the deploy that introduced these keys:
set it false first (api + worker, restart), then strip the keys from existing rows with
`UPDATE media_collection_stories SET story_json = jsonb_set(story_json, '{content_plan}',
(story_json->'content_plan') - 'form' - 'open_questions') WHERE story_json->'content_plan'
?| array['form','open_questions'];` (let in-flight prepared turns drain first), then redeploy
the old image. The card's length is `duration_s`, summed from the story's
moments on every read. Builds below the protocol also get `open_questions` stripped from
responses (`prepare_story_snapshot` strips it from the prepared payload for everyone).

`POST .../stories` may add `suggested_research_topic` (omitted when null): a public place or
landmark named in the creator's query, else repeated in their clip descriptions
(`research_topic_suggestion.py`, regex only, no model, capitalised names, 4+ characters; absent
when the request already carries a `research_topic`). It is computed on every request, cached
hit or not, and only prefills the "Research an angle" field through the "Add background facts"
chip. Public search still receives exactly the `research_topic` the creator sends. The idea card
labels the footage section "From your footage" and research as "Background facts" with tappable
source links; the research review gates are unchanged.

Ideas are per event (KRI-944). `POST .../stories` takes `group`: evidence is limited to the
members of that group, the story persists with `story_json.group`, and `GET .../stories?group=`
lists one event's ideas. A non-empty `exclude_story_ids` means "another idea": it adds ONE idea
beside the existing ones and drops anything with the same wording or the exact same moment set
(the agent also receives them as `already_offered`). `DELETE .../stories/{id}` soft-deletes
(`story_json.deleted_at`, restorable for `RESTORE_WINDOW`) and writes a `not_for_me` judgment
through `record_deleted_idea` when personalization is on; deleted ideas are never re-offered
for their event even when it is off. `POST .../stories/{id}/restore` undoes it.
`DELETE .../groups` removes an event's members (revision bumps) and soft-deletes its ideas.
Ideas are pinned to a collection revision, so a write that leaves an event's members identical
re-stamps that event's ideas (`carry_forward_stories`); only rows from the revision being left
move. No migration: group and deletion live in `story_json`.

Photos (KRI-944). `index_collection` turns a photo's image analysis into ONE still moment held
for `STILL_HOLD_S` (3 s; `start_s=0`, `evidence.media="still"`, `time_basis="still_hold"`), stamped
with the image index spec (the hold is part of it). A photo whose analysis recorded nothing usable
gets no moment: `metadata_json.moment_index` marks it, the items route reports
`excluded(no_usable_moment)`, and it never holds back the rest of its event. The story agent
sees such a candidate as `media: "still"` and its prompt gains the stills guidance only then
(video-only requests keep their exact payload). **Ideas may cite photos only while
`media_moments.stills_buildable()`**: the phone writer already renders a pinned still beside video
cuts (asset-lane, `stillImages`), but a prepared story's export is verified against its sources
and `actual_export_verification` reads original videos only, so a photo story would stop at
`CreatorRenderContractError`. `VERIFIES_STILL_PICTURES` stays false until the verifier gets a
still branch; `tests/pipeline/test_photo_story_gate.py` pins that coupling. Until then photos are
analysed and shown, never offered. The preparation step also needs one more piece before it can
accept them: a library photo reaches the render only as a ready Visuals-pool row of the item
(`POST /plan-items/{id}/assets` with `library_asset_id`), since thread attach rejects images.

Stories support review, exact-interval replacement/order/removal, original/muted audio,
rejection and selection. Selection prepares the common revisioned draft, with a server-owned
story/collection/compiler binding. Separate expiring approval freezes the existing guided
execution plan. Only the verified original-bound, rate-1, hard-cut video adapter is offered;
no new renderer, captions, music or cloud/slides promotion. Initial output must pass actual
picture/timing/audio checks; retries and last-good artifacts use the shared guards.

Admission requires `MEDIA_LIBRARY_ENABLED`, `MEDIA_COLLECTION_PROPOSALS_ENABLED`, native
authentication and explicit membership in `MEDIA_COLLECTION_PROPOSAL_USER_IDS`. Empty means
nobody. Turning the switch off hides discovery and refuses new collection requests without
modifying historical posts. `MEDIA_COLLECTION_CACHE_ONLY=true` is the default: cache misses
cannot acquire model work, even if a client requests it. The current authorized pilot is one
creator account, with up to $2 total model spend authorized only for the real test. No cohort or production flags were changed.

Collection membership/facts do not pin cloud copies or alter retention. Device diagnostics,
repeatable export proof, offline evaluation contracts and remaining human evidence are in
[implementation status](../reviews/camera-roll-recommendations/status.md).

## Retention and erasure

Archive hides future selection and preserves editable consumers. Catalogue
membership and facts do not pin cloud copies; editable-post references do.
Inactive copies receive warnings after 83 days and become eligible after 90 days
and an actual seven-day warning lead. Catalogue records/facts survive expiry.
Confirmed account deletion removes captured post projections before owner/source
cascades and retains existing durable erasure outboxes. Immediate primary-root
cleanup is best effort; delayed verified sweeps cover both owned roots and late
uploads. Sign-out/local-original behavior belongs to the iOS slice.

## Evidence

Real PostgreSQL tests cover automatic rotation at 500, concurrent admission, rollback,
duplicate finalization, ownership,
cleanup claims, repair, durable facts/version invalidation and shared-post erasure.
Reuse tests record provider counts and check independent edits, archived-source
binding, original hash rejection and two chat bindings without uploads.
Providers/storage are controlled fixtures. Preserve fingerprinted test, migration,
contract and native result bundles. Fixture counts do not replace physical-device
reuse/export before pilot activation.
