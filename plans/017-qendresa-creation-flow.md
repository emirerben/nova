# Qendresa creation flow

Status: implemented; awaiting PR review and staged rollout
Owner: Codex autoship
Source: Qendresa's August 19 product feedback and the approved implementation plan in this task

## Problem

Kria currently opens returning creators in an ideas-first workspace. The product must instead make its core promise legible and immediately actionable: upload footage, receive a first cut, and continue in the canonical full-screen editor. Planning stays available but secondary.

## Delivery slices

### Slice 0 — mixed-media regression

- Add a route-level regression proving two videos plus two images are accepted by the standalone generative path and retain their authored order.
- Keep plan-item main-footage validation pre-enqueue and actionable; supporting photos remain overlays/collage inputs.
- Production correlation completed against the August 19 trace: job `807abcac-fd2d-4954-898d-265b57e34ca6` contains exactly two main videos plus two supporting images and failed as `guided_story_render_failed`. The images were correctly classified and compiled. The render crashed because a phone video stored landscape pixels with a `-90°` Display Matrix; the old guided-story path skipped rotation normalization, chose a landscape canvas, then issued an invalid oversize crop after FFmpeg autorotation. This was already fixed on `main` by `4101b3faf`/PR #860, with the production-source regression `test_selected_rotated_video_is_normalized_without_changing_source_receipt`. The new authored-order route regression remains as the request-boundary guard.

### Slice 1 — creation hub and authenticated first cut

- Replace the returning-user workspace header with a creation hub behind `NEXT_PUBLIC_CREATION_HUB_ENABLED`.
- Rename authenticated navigation from Plan to Create.
- Add `/create`, using same-origin `/api/plan/generative-jobs/*` routes for upload, create, and status.
- Upload is the primary interaction. Direction and final voiceover are optional, subordinate controls.
- Add `POST /me/jobs/{job_id}/open-in-editor` with optional title. It requires a caller-owned job, selects the lowest-rank ready variant, idempotently creates one unscheduled `existing_footage` plan item, links both existing foreign keys, and returns `{ plan_item_id, variant_id }`.
- On the first ready variant, `/create` calls the promotion endpoint once and replaces the route with `/plan/items/{item}/edit?variant={variant}`.

### Slice 2 — setup and recovery

- Present plan-item setup progressively: footage, direction, audio choice, optional Kria planning, Generate.
- Keep main footage and supporting visuals as separate roles. Main photos are auto-routed only when a compatible collage mode is selected; otherwise block before enqueue with repair copy.
- Add direction-audio transcription that writes `PlanItem.notes` and never `voiceover_gcs_path`.
- During the API-first rollout, plan-item voiceover attachment accepts metadata-validated
  legacy paths (including the old anonymous recorder's synthetic direct prefix) only while
  `GENERATIVE_DIRECT_VOICEOVER_STRICT_ENABLED=false`; attachment selects `audio_mode=voiceover`.
- Add one safe failure mapper shared by create, plan-item zero-variant, and library surfaces. Preserve user inputs on retry.

### Slice 3 — manual draft, hidden until verified

- Reuse `PlanItem`, `Job`, and `EditorShell`; no project model or second editor.
- Create an unscheduled `existing_footage` item plus a linked draft job, attach uploaded media in authored order, and seed one manual variant.
- Open `EditorShell` with its existing signed-source virtual preview. First Save/export produces the initial server render.
- Drafts are resumable and excluded from the finished library until exported.
- Keep the creation-hub `Edit myself` action hidden unless the manual-editor feature flag and all acceptance gates are green.

## Architecture

```text
/plan creation hub
  |-- Make a video with Kria --> /create
  |      upload/create/status --> /api/plan/generative-jobs/* --> owned Job
  |      first ready variant --> /api/me/jobs/:id/open-in-editor
  |                                  |-- existing ContentPlan
  |                                  |-- unscheduled PlanItem(existing_footage)
  |                                  `-- Job <-> PlanItem link
  |      route replace --------> /plan/items/:id/edit?variant=:id --> EditorShell
  |
  |-- Plan content ------------> #ideas (existing IdeasHome)
  `-- Edit myself (flagged) ----> manual draft Job + PlanItem --> same EditorShell
```

Lock order for promotion remains `ContentPlan -> Persona -> PlanItem(s) -> Job`. Ownership failures return 404. A job without a ready variant returns stable 409. Existing links win idempotently.

## Editor timeline contract

1. Duration source: `buildVirtualTimeline` remains canonical for ordered clips, overlaps, and total preview duration; the server variant/assembly plan remains canonical for render compilation.
2. Ripple policy: this feature adds no insertion or ripple behavior. Existing text, visual, motion, SFX, overlay, and continuous-music rules remain unchanged.
3. Scrub bounds: `VirtualTimeline.totalDurationS` continues to clamp virtual preview, ruler, playhead, keyboard seeking, and inverse mapping.
4. Resize behavior: unchanged; manual drafts use existing clip handles, rounding, overlap caps, and validation.
5. Undo behavior: existing `useEditorHistory` snapshot boundaries remain authoritative; draft initialization adds no pointer-level mutations.
6. Preview/render parity: seed authored clip order into the same slot representation consumed by `buildVirtualTimeline` and the server compiler; run `make verify-editor-timeline` and add a manual-draft fixture regression.

## Failure modes

| Failure | Required behavior |
| --- | --- |
| Cross-user job promotion | 404 without existence leak |
| Duplicate promotion or refresh | Return the existing item and ready variant; no duplicate row |
| Job still running or all variants failed | Stable 409 and safe repair copy |
| Invalid main media | Reject before enqueue and name the affected input/repair |
| Upload/storage interruption | Keep successful uploads and retry only failed files |
| Encoder/timeout | Retry render without re-uploading or clearing direction/audio mode |
| Unknown failure | Generic recovery and copyable job reference |
| Manual draft has no rendered output | Signed-source virtual preview; draft absent from finished library |
| Unsupported HEVC/HEIC | Explicit proxying state or actionable unsupported-media error |

## Test coverage

```text
BACKEND
[open-in-editor]
  |-- owner + ready variants -> lowest-rank ready + unscheduled item
  |-- duplicate request ------> same item
  |-- cross-user -------------> 404
  |-- unfinished/zero-ready --> stable 409
  `-- metadata ---------------> clips, edit format, links copied
[mixed media]
  `-- video, video, image, image accepted in order or preflight rejected
[direction audio]
  |-- transcript -> notes
  `-- voiceover path unchanged
[manual draft]
  |-- ordered media + draft response
  |-- resume same draft
  |-- excluded from library
  `-- first export transitions to finished output

FRONTEND
[/plan]
  |-- hub primary/secondary hierarchy
  |-- Ideas anchor
  `-- manual action hidden by default
[/create]
  |-- signed-in same-origin upload/create/status
  |-- direction/voiceover optional
  |-- ready -> idempotent promotion -> editor navigation
  |-- failure taxonomy + retry state preservation
  `-- mobile/keyboard/zoom/touch-target structure
[EditorShell]
  `-- draft virtual-source preview uses canonical timeline
```

## Rollout

- Deploy the API first with `GENERATIVE_DIRECT_VOICEOVER_STRICT_ENABLED=false` so
  the old Vercel VoiceRecorder keeps its metadata-validated `voiceover-uploads/*`
  compatibility, including its synthetic-user direct prefix.
- Deploy Vercel with the owned direct voiceover uploader while the creation hub remains off.
- Enable `GENERATIVE_DIRECT_VOICEOVER_STRICT_ENABLED=true` on Fly; signed-in create
  requests then require `voiceover-uploads/direct/{user_id}/` exactly.
- Enable `NEXT_PUBLIC_CREATION_HUB_ENABLED` only after strict Fly validation is live.
- `NEXT_PUBLIC_MANUAL_EDITOR_ENABLED` remains false until mobile Safari, HEVC/HEIC proxying, resume, virtual preview, export, and editor timeline checks pass.
- Retest with Qendresa after each slice.

## Verification evidence

- Backend focused regression set: 516 passed, 1 skipped. The skipped test is the
  real two-session manual-draft concurrency case, which requires the CI Postgres service.
- Backend full suite on the final implementation: 9,706 passed, 72 skipped, 2 xfailed.
- Frontend full suite: 249 suites and 3,151 tests passed.
- TypeScript typecheck passed. Frontend lint passed with only pre-existing warnings.
- Editor timeline verification passed for the canonical virtual timeline and manual-draft
  source preview gates; desktop and phone browser QA passed for `/plan` and `/create`.
- Final adversarial review found three rollout/ownership gaps; each now has a focused
  regression, and the clean re-review passed.

## GSTACK REVIEW REPORT

| Run | Status | Findings |
| --- | --- | --- |
| Engineering scope | PASS | Existing models, uploads, proxy, render jobs, and EditorShell cover the core flow; no new project/editor model is justified. |
| Architecture | PASS | Idempotent promotion and strict ownership keep the new entry path inside existing boundaries. |
| Code quality | PASS | Shared clients and failure copy are required; parallel editor or recovery implementations are prohibited. |
| Tests | PASS | Ownership, idempotency, mixed-media ordering, retry preservation, draft lifecycle, and timeline parity are explicit. |
| Performance | PASS | Direct signed uploads and current polling are reused; no media bytes pass through Next.js. |

VERDICT: APPROVED FOR IMPLEMENTATION

NO UNRESOLVED DECISIONS
