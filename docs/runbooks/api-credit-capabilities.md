# Metered API credit capabilities for native Kria

Generated images and owned-image edits use `gpt-image-2.5-sunburst` through the Images API. Narration uses `gpt-realtime-2.1-mini` through Realtime. The existing `_online_eval` judge uses `claude-sonnet-5-5`; it is not a second evaluation runner. Gemini defaults and existing optional text research/localization remain unchanged. Final rendering stays on the iPhone.

## Configuration and admission

Migration `0127_generated_media_operations` adds durable operations. Deploy compatible schema/API/worker/native readers before any separate rollout approval. New settings in `.env.example` are false, empty or zero. Proposed credit allocations do not populate live budgets.

Generation requires `AI_COST_CONTROL_ENABLED`, `GENERATED_MEDIA_LIVE_APPROVED`, a billing organization, an OpenAI key, and `GENERATED_MEDIA_PRICE_VERIFIED_AT` within the last seven days. Positive provider monthly and capability budgets are required. `GENERATED_IMAGES_ENABLED` controls images and edits; `GENERATED_NARRATION_ENABLED` controls narration. `GENERATED_MEDIA_MAX_REQUEST_USD` must cover the request estimate ($2 images, $0.10 narration). The image estimate is a conservative operator-verified reservation, not an independently proved hard provider charge bound.

The judge performs operational trace scoring. It does not enroll traces into the consented comparison corpus, replace human review, approve proposals or regenerate them. Real-request corpus evaluation keeps its existing eligibility and grant checks.

The atomic ledger checks provider monthly budgets and evaluation/image/narration allocations before transport. Unknown liabilities continue counting against caps. Judge execution additionally needs `CREDIT_QUALITY_REVIEW_ENABLED`, `CREDIT_QUALITY_REVIEW_BILLING_VERIFIED`, Anthropic credentials and an approved bounded run. Production attribution uses `CREDIT_QUALITY_REVIEW_APPROVED_RUN_ID` and `CREDIT_QUALITY_REVIEW_MAX_RUN_USD`; nonproduction requires an approved live-eval/provider-smoke context. Runtime/Celery attribution is preserved through the existing judge path.

Native capability advertisement requires a native client, minimum protocol and phone-render eligibility (or native-device-only mode). Images require verified `visualBlocks`. Narration additionally requires phone narration rendering and verified `narrationAudio`. SwiftUI consumes these capabilities. Image edits pin an owned pool source or verified library original and immutable generation. Reference images retain alpha and EXIF orientation while a bounded copy is resized to at most 1536 pixels per edge. Original source bytes remain unchanged. Narration pins the current approved script ID, version and SHA-256.

## Routes and recovery

All paths below start with `/plan-items/{item_id}` and require authenticated ownership plus native protocol admission.

| Method and suffix | Purpose |
| --- | --- |
| POST `/generated-media` | Submit `image`, `image_edit` or `narration` with a persistent `client_event_id`. |
| GET `/generated-media/narration-script` | Read the current approved script identity for narration. |
| GET `/generated-media/by-event/{client_event_id}` | Recover an operation when the submission response was lost. |
| GET `/generated-media/{operation_id}` | Inspect operation state. |
| POST `/generated-media/{operation_id}/retry-storage` | Admit already completed bytes without another provider generation. |
| POST `/generated-media/{operation_id}/accept` | Accept the result; narration rechecks script identity before attachment. |

A unique owner/item/event identity and request digest prevent duplicate purchase and reject changed inputs under the same event. Persist the event before submission; recover by event after interruption. Do not replace an unresolved event with a fresh one. Transport has no automatic paid retries. States are `pending`, `submitting`, `provider_complete`, `ready`, `unknown` and `failed`. Stale submitting operations become unknown after five minutes. An unknown result retains ledger liability and requires reconciliation, not automatic regeneration.

Completed provider bytes are committed before immutable storage admission. A storage failure preserves them for retry; storage identity and downloaded digest must match. Images become owned `PlanItemAsset` rows. Narration enters the existing direct-voiceover ownership path. Titles remain separate editable metadata and are not automatically inserted as canvas overlays. Admission and acceptance do not enqueue final rendering.

Retained pending bytes expire after 24 hours. Maintenance clears expired pending bytes and marks `result_expired`; it does not delete admitted owned objects. Operation rows cascade when their user or item is deleted; deleting an asset sets the operation asset reference to null. Object retention remains governed by existing ownership/retention workflows, not the pending-byte TTL.

## Verification and rollout limits

Use the exact no-live-call commands in [VERIFY.md](../reviews/api-credit-plan/VERIFY.md). Review [WORKFLOW.md](../reviews/api-credit-plan/WORKFLOW.md) and [REVIEW.md](../reviews/api-credit-plan/REVIEW.md) for current evidence and unresolved gates. Simulator fixtures establish client behavior; they do not establish live-provider quality or physical-phone export fidelity.

Fresh holdout enrollment is blocked. The 29 October 2026 13:20 UTC metadata-only inventory found zero approved eligible holdouts and zero approved development cases. Historical inventory had one creator and 173 threads, complete owner mapping and incomplete media mapping. No sealed targets were read. No automatic promotion or live quality winner follows from these checks.

Keep new switches disabled and budgets zero until separate billing, price, live-call and native-output gates are satisfied. Publication is separate from deployment approval; the main branch Fly workflow deploys on push. The integrating parent owns the merge/deployment gate and final consolidated verification.
