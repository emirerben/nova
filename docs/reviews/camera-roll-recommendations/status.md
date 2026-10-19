# Camera-roll recommendations: implementation and evidence

19 October 2026. Collection, retrieval, targeted inspection and recommendation-to-approval code is implemented. The creative-plan continuation adds a specific content angle, audience, reasoning, proposed opening/closing text, editable wording and a named ordinary project. All 12 owned upload originals are now fingerprint-linked on the physical iPhone, and a 1080p native example with the reviewed text exists. Final synchronization/reopen verification is still in progress. Discovery remains off; no production rollout occurred. Human retrieval labels and the account study remain outstanding; synthetic correctness checks do not establish recommendation quality.

Worktree: `/private/tmp/nova-kri-729-camera-roll`; branch `codex/kri-729-camera-roll`.

## Implemented

- Native explicit Photos/saved-upload selection, time/coarse-place grouping, editable groups, removal/addition, account-scoped resume and multiple saved collections. Photos identifiers remain local. Revocation removes inaccessible candidates and updates the server selection. Picker-only providers require reselection after relaunch. Metadata never proves original-file availability.
- Existing catalogue reservation/finalize and original-fingerprint stores handle explicit import. Videos upload timeline-preserving analysis copies; originals stay on the iPhone. Photos use the existing saved-upload contract. Cancellation preserves completed bindings.
- Durable owner/source/version-pinned visual intervals and complete word-timed speech; text search, transcript ranges, semantic reranking and targeted interval inspection. Existing fact leases/cost ledger handle reuse. Results recheck authorization, revision and source generation/size. Existing visibility and overlapping occlusion/unusable-span evidence reaches ranking/story inputs as fallible context; darkness is not automatically rejected.
- Zero to three grounded content ideas with an audience, angle, retention hypothesis, explicit proposed copy, caption and per-shot reasons. Wording edits are durable revisions. Interval preview/replacement, order/removal, original/muted sound and rejection remain available. Review descriptions never become on-screen copy. Offered/exposed/corrected/selected events are distinct. Common approval/execution records remain authoritative for export outcomes.
- Selection attaches existing sources and prepares the common revisioned draft. A server-owned envelope binds the exact story/collection revisions, guided snapshot and compiled hash. Separate expiring approval remains mandatory. Changed evidence cancels approval. The job factory freezes the compiled plan; the existing worker reads it without another creative plan or music match. Existing retry/last-good guards apply.
- Initial story exports require actual-output verification even when ordinary enrollment is off. New receipts pin verifier v2; historical receipts remain readable.
- Offline frozen-label text/semantic comparison and preregistered pilot reports retain failures and unknowns. Reports cannot enable rollout.

## Bounds and initial verified adapter

| Work | Bound |
| --- | --- |
| Metadata | 50 items, batches of 32; local snapshots at most 2 MiB |
| Eligibility | Every added item; no per-item second selection |
| Retrieval | 100 relevant intervals across all <=50 sources; source checks in batches of ten; <=10 returned hits |
| Source check | One request, five-second timeout per object |
| Semantic rank | One attempt, 15s call / 20s deadline, $0.05 reservation |
| Story generation | One attempt, 20s call / 25s deadline, $0.05 reservation |
| Targeted inspection | Three distinct checks/revision; <=20s interval, <=64 MiB source, one upload/call, 45s deadline, $0.05 reservation |
| Story | <=8 complete intervals, <=60s |
| Export | Original-bound videos, rate 1, hard cuts, portrait cover fit, reviewed opening/closing text, original/muted sound, no added music |

Photos/other catalogue sources remain metadata/search material; only eligible original-bound videos are offered as executable stories. This slice does not promote cloud/slides/effects or infer relevance from brightness/energy. Unsupported combinations fail before approval.

Admission also requires an explicit account cohort (`MEDIA_COLLECTION_PROPOSAL_USER_IDS`); empty means nobody, and the global switch remains a rollback gate. The server and native screen default to **cached analysis only**. Cache misses cannot acquire work or call a provider. Only an explicitly enabled paid-analysis cohort can turn that choice off. Upload/analysis still needs its own action and existing cost controls.

## Physical evidence

Creative-plan continuation: the owner-approved matcher checked only the 50 previously selected Photos entries. It linked all 12 owned upload originals (356,970,620 bytes total), requiring exact SHA-256 and byte count. Originals remained on the iPhone. The real signed-session HTTP journey uses an isolated local copy of this account, a dedicated database/queue and local object store, restricted to the exact approved private Mac/iPhone addresses. The production AVFoundation exporter produced a 10.1s H.264/AAC 1080×1920 example from three fingerprint-pinned 4K originals, including the corrected opening text and proposed closing text. The complete upload verification/reopen check is pending; this does not establish a completed pilot. Details and failure controls: [creative project flow](creative-project-flow.md).

The creator selected 53 assets on an iPhone 13 Pro: 24 photos, 29 videos. Metadata resolved in 33.72ms, 32-item passes, 72.1 MiB process peak. No media/provider read/upload occurred. This established the initial metadata ceiling, not original availability or complete product performance. [Aggregate benchmark](metadata-benchmark.json).

The production AVFoundation exporter rendered a frozen 14s synthetic story on the physical phone, using 155–162s windows from two long sources. Independent checks passed picture/timing/source audio and rejected wrong windows, muted sound and reversed order. Correct export took 3.16s, without uploads/model calls. [Physical proof and repeat commands](physical-export.md).

This exposed a verifier defect: FFmpeg's stereo-to-mono power fold added sqrt(2) gain against AVFoundation's amplitude-preserving stereo export of a mono source. Verifier v2 compares channel amplitudes directly, retaining existing tolerances. Real-media controls reject a muted channel and doubled sound and cover silent sources.

The creator has added 50 items (22 photos, 28 videos) in the native metadata-only collection flow. All 50 metadata records resolved, with zero media/provider reads/uploads; initial real-flow metadata work took 1.13s and process peak was 107.5 MiB. Relaunch after the no-second-selection update retained all 50 items and resolved all metadata again in 634ms (64.6 MiB process peak), with zero media/provider reads/uploads. Aggregate reopen evidence is [device-collection-resume.json](device-collection-resume.json). Manual group correction and the limited/revoked-permission matrix still need hardware evidence.

A fresh collection edit/resume diagnostic is installed separately as `com.kria.app.camerarollqa`. `-camera-roll-device-collection` disables upload/analysis and exports only aggregate counts. Device reopen retained all 50 items; manual group correction and permission-state evidence remain pending. Earlier benchmark counts do not prove that product journey.

## Own-upload preview continuation

Twelve existing video analysis copies belonging to the creator’s account were read through exact-generation access and checked against their stored SHA-256/byte receipts (4,679,379 bytes). All twelve had eligible cached visual facts. Seven had timed moments; five had no indexed intervals. No production data/flags changed and no new visual analysis or publication occurred.

Two story-generation requests in an isolated development database settled at **$0.004802**, under the approved shared $2 cap. The existing guided snapshot/phone compiler and production AVFoundation exporter on macOS rendered 10.1s and about 8s examples. Independent source-window/picture/audio checks accepted both (16 and 12 checks). These use analysis copies, with lower detail after portrait cropping; they do not establish full-quality original availability or a physical signed-in recommendation/approval/export journey. The iPhone was disconnected during the initial run. After reconnection, the same frozen recipes also rendered on the physical iPhone in 1.94s and 1.55s. Independent checks accepted both physical outputs (16 and 12 checks), with zero additional provider calls/uploads. These still use the same analysis copies, without a verified link to full-quality Photos originals. The initial unattached launch did not finalize; a foreground console-attached retry completed both, so unattended/background behavior is not established. [Safe aggregate](own-upload-previews.json); source footage, prompts/results, signed URLs, private snapshots and MP4s remain ignored locally.

This real-input run exposed and reproduced three defects: distinct questions collided with the durable source-fact uniqueness constraint; fractional endpoints could disagree with the 30fps draft schedule; and the native collection refused useful suggestions whenever any clip lacked timed moments. Request-scoped version slots now preserve legacy cache reads and the three-inspection cap; the prepared length is fixed on the shared frame grid before approval; partial coverage is shown without blocking usable footage. HTTP/real-PostgreSQL and native UI red controls fail before these fixes.

The collection now uses the existing Sunlit tokens, Inter/Fraunces type, shared thumbnail loaders and Butter primary action. A compact grid replaces per-item metadata forms. Group editing and analysis details sit behind options; one action confirms an upload only when necessary. New ideas show the concise angle, reasoning and proposed copy; legacy story arc detail is collapsed. Thumbnails and precise preview/replacement controls remain available. The earlier DEBUG-only offline host uses intercepted requests; the new hardware journey uses genuine local mobile sessions and real HTTP handlers.

## Verification

Latest own-upload/design continuation: 157 backend/HTTP/migration compatibility checks passed, 1,244 free agent replays passed (15 documented skips), and the final native unit gate executed 1,860 tests (25 skipped, zero failures; `test-results/ios/unit.wmqETh/unit.xcresult`). Three final UI journeys pass in `.dev/camera-roll-final-delivery-ui.xcresult`: partial coverage, collection correction/removal/resume, and story replacement/sound/rejection. The final pre-PR gate passes all 1,043 mapped reliability checks plus scoped lint/typecheck, drift and release metadata checks (`.dev/camera-roll-delivery-preship.log`).

- API regression/free replay: **1,821 passed, 15 skipped**, `.dev/camera-roll-final-regression.log`. No optional paid live evaluation. The final source-quality/prompt update also passes 1,217 checks and 15 skips in `.dev/quality-reuse-and-budget.log`.
- Real PostgreSQL selection → draft → separate approval → job factory → frozen worker-plan read; changed-collection cancellation, owner/source fences and $0 cache controls.
- Final main-integrated API/HTTP/free-replay gate: 1,312 passed, 15 skipped; `.dev/camera-roll-final-merged-api.log`. Replay skips are live/judge-only cases, inapplicable positive-fixture assertions, non-standalone ground-truth inputs, and three missing historical ground-truth/transcript sidecars. No camera-roll contract case is skipped.
- Native unit gate after final main integration: 1,860 executed, 25 skipped, zero failures; `test-results/ios/unit.ARJe4f/unit.xcresult`.
- Native collection correction/removal/relaunch and story replacement/sound/rejection UI passed; `.dev/story-review-6.xcresult` and `.dev/story-review-7.xcresult`. The no-second-selection update also passes `.dev/all-added-content-ui.xcresult` (five state checks and two UI journeys).
- `make verify-editor-timeline`: 209 web tests, 28 desktop and 42 mobile browser cases, 582 backend cases passed; `.dev/editor-timeline-gate.log`.
- Actual-media verification/completion/publication: 44 passed before the combined run.
- Frozen worker plan hardening: 239 passed; later mutable SFX/music/label inputs cannot change approved choices. Final main-integrated pre-PR gates: 1,043 passed; scoped lint/typecheck, release metadata ownership and drift checks also pass (`.dev/camera-roll-final-merged-preship.log`). Agent contract replay: five passed, no model calls.
- Migration 0122 forward/backward and authored-choice downgrade guard verified. Migration 0123 fresh upgrade, empty downgrade/re-upgrade and reviewed-story preservation guard passed in a newly created/disposed scratch database; `.dev/schema-roundtrip.log`.

Repeat the feature checks:

```bash
bash scripts/check-api.sh test tests/integration/test_media_collections.py tests/integration/test_camera_roll_execution.py tests/integration/test_camera_roll_pilot_cli.py tests/integration/test_camera_roll_retrieval_eval_cli.py -q
KRIA_IOS_TEST_MODE=unit KRIA_SIMULATOR_ID=<dedicated-simulator-id> make ios-verify
```

## Pilot and release decision

The creator limited the pilot to **their account** and subsequently authorized model calls when needed for a real test, under the discussed **$2 total ceiling**. No other creator may be enrolled or contacted. `single_creator` reports cannot qualify for wider rollout. Production remains default-off/cached-only. A separate development run used an atomic $2 cap; two calls settled at $0.004802.

The creator has received two private proxy-quality example renders; this is not a signed-in device pilot or independently reviewed fulfillment study. No human-labelled holdout exists yet. Do not claim 90% recall, story coherence or pilot conversion. Decision: **hold** pending the account study and independent output review. The broader ten-creator hypothesis remains untested; the narrower user-authorized study replaces participant recruitment for this task. [Protocol](pilot-protocol.md).
