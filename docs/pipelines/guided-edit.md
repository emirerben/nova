# Guided edit proposal pipeline

Guided edit separates creative approval from rendering. It is the review contract between all
uploaded plan-item media and the strict story renderer. Planning and approval create the contract;
the story assembler consumes the approved Job snapshot directly.

## Product flow

1. The creator uploads main clips and any supporting photos/videos.
2. **Plan edit** opens an editorial conversation. The creator can describe the result in ordinary
   language (for example, “a reflective diary about the food and old town” or “fast highlights,
   little text”). `EditGuideAgent` reflects what it understood, asks at most one useful follow-up
   at a time, and persists a typed direction, goal, pace, and target length.
3. `POST /plan-items/{id}/edit-proposal/draft` assigns stable IDs to legacy clip assignments,
   creates a token-fenced attempt, and queues `draft_edit_proposal`.
4. The task waits for existing visual-pool analysis, analyzes every attached clip without current
   metadata, and resolves the immutable storage generation for every source.
5. `EditProposalAgent` sees the complete media set plus creator-written context. It proposes an
   ordered title and story beats. It must use at least seven distinct sources when available and
   may not invent personal experiences.
6. The item page shows combined photo/video thumbnails. The creator can continue the same
   conversation with requests such as “put food first,” “make it slower,” or “use less text.”
   Conversational revisions may reorder and rewrite editorial fields, but the server preserves
   every beat's media membership and creator-written thoughts. Direction, goal, pace, title,
   order, layouts, and thoughts also remain manually editable. AI-written thoughts carry an
   **AI draft** label.
7. **Approve plan** saves corrections and approves them using compare-and-swap against
   `expected_proposal_version`.
8. Generate revalidates every storage generation and snapshots the exact approved proposal and
   media identities into `Job.assembly_plan.guided_edit` while holding the established
   Plan → Persona → PlanItem → Job locks.

## Stored envelope

`PlanItem.edit_proposal` is nullable JSONB with `schema_version=1`:

- `proposal_version`: increments on every user-visible state mutation.
- `generation_attempt_id`: prevents an older analysis task from overwriting a newer attempt.
- `media_digest`: SHA-256 of canonical lane, stable ID, object path, storage generation, kind,
  and content hash. Editorial ordering is intentionally excluded.
- `status`: `briefing`, `analyzing`, `drafting`, `draft`, `approved`, `stale`, or `failed`.
- `brief`: requested direction, goal, pace, and duration.
- `conversation`: up to ten durable creator/Kria exchanges, including reply suggestions. The
  thread survives reloads and proposal-generation retries.
- `brief_ready`: Kria's advisory signal that it has enough direction to plan. It never gates the
  creator; **Build this edit plan** is available whenever a conversation reply is not in flight.
- `draft`: the current editable proposal.
- `last_approved`: immutable approval metadata plus the approved snapshot. It is retained when
  media changes so the creator can compare before planning again.
- `failure`: plain-language code, message, and retryability.

The two storage lanes remain independent:

- `clip` refs point to `PlanItem.clip_assignments[*].media_id`.
- `asset` refs point to `PlanItemAsset.id`.

Both become the same proposal-level `MediaRef`; no storage migration or duplicate object is needed.
Pool assets promoted into the clip lane are de-duplicated by object path for story selection.

## Staleness and concurrency

Adding, removing, or replacing a clip or pool asset marks the proposal stale. Changing creator
context does too because the draft may have relied on that context. Staleness retains both draft
and last approval but Generate refuses it.

Draft edits and approvals require `expected_proposal_version`. A stale browser tab receives
`proposal_conflict` instead of silently overwriting newer work. Analysis writes are additionally
fenced by attempt ID, status, media digest, and a second storage-generation check after analysis.
The analysis task also carries the plan ownership epoch, so transferring a plan invalidates queued
work. Repeated **Plan edit** clicks reuse an active attempt, and new attempts are limited to three
per minute per client IP. Conversation turns are limited to twelve per minute per authenticated
creator.

The mutation contracts are:

- `POST /plan-items/{id}/edit-proposal/conversation` with `expected_proposal_version` and a natural
  language `message` returns the updated item (`200`). Before analysis it updates the typed brief.
  During review it may return a revised draft, which always requires approval again.
- `PATCH /plan-items/{id}/edit-proposal` with `expected_proposal_version` and a complete `snapshot`
  returns the updated item (`200`) or a structured `409` conflict/stale response.
- `POST /plan-items/{id}/edit-proposal/approve` with `expected_proposal_version` returns the approved
  item (`200`) or a structured `409` draft/conflict/stale response.

Media identities and analysis are server-owned. PATCH may change direction, goal, pace, duration,
title, beat order, layouts, and thoughts, but it cannot replace media metadata.

The conversation endpoint follows the same trust boundary. Under the item lock it first persists a
short-lived, token-fenced single-flight reservation, then releases the transaction before calling
the model. Duplicate tabs therefore cannot spend a second model call. The final write reloads under
lock and must own both the token and proposal version. Responses expose only safe `thinking` or
`retry required` state—not the token—so a reload disables generic planning while the creator's
words are still being interpreted. An abandoned reservation expires after 90 seconds and can be
reclaimed by resending the direction. Draft revisions must preserve every existing beat ID exactly
once; the route rejoins server-owned media IDs and retains creator-authored thoughts verbatim.

## Conversational rollout

`GUIDED_EDIT_CONVERSATION_ENABLED` is a separate API-read writer gate. Keep it false while this
release rolls across every API and worker: the web continues to show the existing direction, goal,
pace, and duration form. After every backend reader understands the `briefing` status, enable the
flag and restart the API. `PlanItem.guided_edit_conversation_available` then switches the already
deployed web UI to conversation without a second build. Rolling the flag back restores the form;
existing conversation history remains readable and is preserved by later planning attempts.

Do not roll the application binary back past `0.33.3.0` while `briefing` rows exist. The reviewed
compatibility conversion is in `docs/runbooks/conversational-edit-rollback.md`.

When enforcement is enabled, Generate returns one explicit 409 code:

- `proposal_required`
- `proposal_draft`
- `proposal_stale`
- `proposal_analyzing`

The same checks run in the synchronous dispatch helper, so direct or delayed task delivery cannot
bypass the route.

## Rollout

All switches default false:

1. `GUIDED_EDIT_CAPABILITY_ENABLED` exposes proposal and visual-pool APIs (API restart).
2. `NEXT_PUBLIC_GUIDED_EDIT_ENABLED` exposes the item-page flow (Vercel rebuild).
3. `GUIDED_EDIT_ENFORCEMENT_ENABLED` requires an approval at Generate (API + worker restart).

After merge, deploy the API/worker and frontend with all three switches still off. Then download the
authorized Corfu inputs read-only into temporary storage, render them through the production Docker
image without production writes, review the MP4, contact sheet, decision trace, and strict receipt,
and delete the scratch inputs. Only after that preview passes should rollout enable capability, then
the frontend, then enforcement. The code-owned `GUIDED_STORY_RENDERER_READY` pin is true only because
guided Jobs now render from their approved snapshot and verify stage receipts before publication;
startup still rejects enforcement without capability. Roll back in reverse order. Existing approvals
and rendered Jobs remain readable with every switch off.

## Strict story rendering

When capability is enabled, a current approved proposal is snapshotted even before enforcement is
enabled. Enforcement controls only whether Generate rejects an item without current approval. This
prevents staged rollout from sending an already-approved story through the legacy montage path.

The worker pins `guided_story_execution_plan` before FFmpeg work. It contains the compiler version,
proposal version and digest, ordered source windows, direction/pace policy, exact music object
path/generation and window (or explicit no-match), typography identity, and approved text. Redelivery
reuses this plan rather than rematching a changed music library.

Compiler version 2 gives every approved moment its direction-specific minimum, caps each video at
its real usable duration (including any transition overlap), and redistributes the rest of the beat
to surrounding photos or longer videos. It still preserves the approved beat and total duration and
fails before FFmpeg only when the complete selected set cannot fill the requested time. Persisted
version 1 plans keep their original equal-share timing on redelivery; validation recompiles them with
the version 1 rules so a rolling deploy cannot reinterpret queued work.

The renderer exact-generation downloads every source selected by a beat. Unselected catalog media
remains authorized but is not required in the output. Photos and videos become sequential full-screen
or supporting-card moments. The downloaded bytes stay untouched for the source receipt; every photo
is separately decoded, EXIF-corrected, and normalized to a render-safe JPEG or alpha-preserving PNG
before FFmpeg applies its still-image loop. This is required for HEIC/HEIF, whose dedicated FFmpeg
demuxer rejects the image2-only loop option. Photos receive a subtle zoom, relaxed/balanced story directions use
duration-compensated crossfades, and fast montages use hard cuts. Video source audio is muted and the
finished base receives either the pinned track or silent stereo AAC. The approved title and thoughts
become editable TextElements on a clean text-free base. A text-only edit reburns from that base and
refreshes its rendered-alpha evidence; other legacy editor operations fail closed instead of rebuilding
the story as a montage. Approved title/thought IDs must remain present exactly once with non-blank text;
changing wording is allowed, silently deleting an approved layer is not.

Ready status requires a verified `render_receipt`: exact beat/media IDs, per-moment FFmpeg evidence,
per-text rendered-alpha bounds inside the canvas, duration, 1080×1920 H.264 video, AAC audio, and the
exact uploaded base/output object generations. Live render attempts use a row-locked heartbeat lease;
duplicate deliveries neither render concurrently nor mark the owning task finished. Any missing
approved layer fails with a guided-story reason. There is no montage fallback.

Content-plan collection responses expose no proposal snapshots or signed preview URLs. The item
detail and proposal mutation responses carry the full review payload, keeping list reads bounded.

## Verification

- State/CAS/digest tests: `tests/services/test_edit_proposals.py`
- API Generate codes: `tests/routes/test_plan_item_generation.py`
- Source-diversity, distinct-chapter, and observation-only draft guards:
  `tests/agents/test_edit_proposal_agent.py`
- Replay/live+judge travel cases: `tests/evals/test_edit_proposal_evals.py`
- Frontend review flow: `src/__tests__/plan/edit-proposal-card.test.tsx`
- Conversation agent/evals: `tests/agents/test_edit_guide_agent.py` and
  `tests/evals/test_edit_guide_evals.py`
- Strict compiler/fault injection: `tests/pipeline/test_guided_story.py`
- Real mixed-media FFmpeg render: `tests/pipeline/test_guided_story_ffmpeg.py`

Run the focused eval in replay mode:

```bash
cd src/apps/api
pytest tests/evals/test_edit_proposal_evals.py -v
```

Run it against Gemini and the judge before changing the prompt:

```bash
NOVA_EVAL_MODE=live pytest tests/evals/test_edit_proposal_evals.py -v --with-judge --allow-cost
```
