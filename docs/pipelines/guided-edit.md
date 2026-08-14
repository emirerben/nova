# Guided edit proposal pipeline

Guided edit separates creative approval from rendering. It is the review contract between all
uploaded plan-item media and the strict story renderer. PR 2 implements planning and approval;
the story assembler consumes the approved Job snapshot in PR 3.

## Product flow

1. The creator uploads main clips and any supporting photos/videos.
2. **Plan edit** asks for direction (`guided_story`, `fast_montage`, or `text_explainer`), goal,
   pace, and target length.
3. `POST /plan-items/{id}/edit-proposal/draft` assigns stable IDs to legacy clip assignments,
   creates a token-fenced attempt, and queues `draft_edit_proposal`.
4. The task waits for existing visual-pool analysis, analyzes every attached clip without current
   metadata, and resolves the immutable storage generation for every source.
5. `EditProposalAgent` sees the complete media set plus creator-written context. It proposes an
   ordered title and story beats. It must use at least seven distinct sources when available and
   may not invent personal experiences.
6. The item page shows combined photo/video thumbnails. Direction, goal, pace, title, order,
   layouts, and thoughts remain editable. AI-written thoughts carry an **AI draft** label.
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
- `status`: `analyzing`, `drafting`, `draft`, `approved`, `stale`, or `failed`.
- `brief`: requested direction, goal, pace, and duration.
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
per minute per client IP.

The mutation contracts are:

- `PATCH /plan-items/{id}/edit-proposal` with `expected_proposal_version` and a complete `snapshot`
  returns the updated item (`200`) or a structured `409` conflict/stale response.
- `POST /plan-items/{id}/edit-proposal/approve` with `expected_proposal_version` returns the approved
  item (`200`) or a structured `409` draft/conflict/stale response.

Media identities and analysis are server-owned. PATCH may change direction, goal, pace, duration,
title, beat order, layouts, and thoughts, but it cannot replace media metadata.

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

Deploy capability first, then the frontend. Do not enable enforcement until the PR 3 strict story
renderer consumes `Job.assembly_plan.guided_edit` and the Corfu preview passes. PR 2 pins the
code-owned `GUIDED_STORY_RENDERER_READY` constant false, so startup rejects enforcement even if an
operator sets the flag accidentally; PR 3 may flip readiness only with strict assembly and receipt
verification in place. Roll back in reverse order. Existing approvals and rendered Jobs remain
readable with every switch off.

Content-plan collection responses expose no proposal snapshots or signed preview URLs. The item
detail and proposal mutation responses carry the full review payload, keeping list reads bounded.

## Verification

- State/CAS/digest tests: `tests/services/test_edit_proposals.py`
- API Generate codes: `tests/routes/test_plan_item_generation.py`
- Source-diversity, distinct-chapter, and observation-only draft guards:
  `tests/agents/test_edit_proposal_agent.py`
- Replay/live+judge travel cases: `tests/evals/test_edit_proposal_evals.py`
- Frontend review flow: `src/__tests__/plan/edit-proposal-card.test.tsx`

Run the focused eval in replay mode:

```bash
cd src/apps/api
pytest tests/evals/test_edit_proposal_evals.py -v
```

Run it against Gemini and the judge before changing the prompt:

```bash
NOVA_EVAL_MODE=live pytest tests/evals/test_edit_proposal_evals.py -v --with-judge --allow-cost
```
