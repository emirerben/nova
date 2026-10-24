# KRI-725 PR 1: cloud adapters and the prepared snapshot (failure matrix)

The failure modes below were listed in the lane plan (`.dev/plan.md`) before any code
was written. This file records each one with its handling and the evidence that
checks it. Evidence: `tests/kria/test_kri725_cloud_prepared_postgres.py` (real ASGI
approval, real Postgres, real claim/dispatch/worker). Artifact:
`KRI725_ARTIFACT=.dev/kri-725 pytest tests/kria/test_kri725_cloud_prepared_postgres.py`
writes `report.json`, `slides_positive.mp4` and `frames.png`.

| # | Failure | Handling | Evidence |
|---|---|---|---|
| F1 | Slide draft edited (caption or order) after approve, before claim | `compiled_refs.slide_post` digest, and `slide:i` sources for a reorder, refuse at claim (`prepared_execution_stale`) | `test_slide_post_claim_refuses_a_changed_approval[F1-*]` |
| F2 | Slide photo replaced at the same id/path (new generation) | `slide:i` source fence at claim. At the worker, the pinned `asset_generations` refuse it | `[F2-photo-replaced-before-claim]`, `test_slide_photo_replaced_after_dispatch_is_refused` |
| F3 | Slide draft edited after dispatch, before the worker runs | Worker renders `approved_slide_post`. The MP4's slide order and the caption are the approved ones | `test_slide_post_renders_the_approved_draft_not_a_later_edit[fenced]`; red control `[red-control-live-read]` renders the edit |
| F4 | Snapshot JSON tampered | `parse()` digest mismatch at worker start; Job fails before spend | `test_cloud_worker_starts_only_the_stamped_attempt[F4-*]` |
| F5 | Snapshot invalidated after dispatch | Worker start refuses | `[F5-*]` |
| F6 | Snapshot compiler newer than the worker | `prepared_execution_unsupported` | `[F6-*]` |
| F7 | Late publication of a superseded attempt | Publication fence fails the variant, keeps `video_path` last-good | `test_late_publication_of_a_superseded_attempt_keeps_last_good[upsert,update]` |
| F8 | Job generation differs from the stamp generation | Worker start refuses | `[F8-*]`, plus `[stamp-digest-mismatch]` |
| F9 | Guided proposal replaced between approve and dispatch | Dispatch compares the story with `compiled_refs.prepared_collection_story` -> `proposal_stale` | `test_guided_dispatch_binds_the_approved_compiled_story` (function-level on real rows; no guided Kria approval fixture exists without the camera-roll phone world) |
| F10 | Legacy unstamped cloud Job | No attempt recorded; publication fence is a no-op | `test_legacy_unstamped_cloud_job_is_untouched` |
| F11 | Non-slide snapshot digest moves | No new key unless the approval targets slides | KRI-722 replay assertion in `test_prepared_execution_postgres.py::test_positive_control_*` still passes |
| F12 | Deploy after an accepted attempt | Worker fence checks row integrity/identity only, never live capabilities | `[F12-deploy-after-accept]` |
| F13 | Sticker removed by the retention sweep | Stickers are pinned sources; render stays best effort (missing sticker dropped) | existing `tests/tasks/test_slide_post_render.py` sticker tests |
| F14 | New adapter or decision key without a coverage row | Import-time `ValueError` | `tests/kria/test_prepared_coverage_guard.py` |
| F15 | Stamped cloud output published without its attempt record | The attempt is held by the task context, not the renderer's dict, so a renderer cannot drop it; the publication test publishes a bare result and is still fenced | `test_late_publication_*` |

## Red control

With the app changes removed (`git stash -- src/apps/api/app`), all 17 E2E cases fail.
Inside the suite, `[red-control-live-read]` removes only the Job pin and shows the
worker rendering the post-approval edit (reversed order, edited caption).

## Found while implementing

- Kria's strategy policy does not offer `slides` (`creator_capabilities.format_availability`
  -> `policy_edit_format_unavailable`). The plan assumed a Kria strategy could reach
  `cloud_slides`, and today it cannot. The slide pin is in place and E2E-proven on a
  stamped slides Job, and it takes effect once a Kria approval can reach that adapter.
  Until then, production slide posts dispatch unstamped from the slides route.
- The classic/guided cloud positive controls stop at the worker start fence (their
  renders need paid models). Actual-export proof for cloud stays with KRI-734.

## Not in scope (unmet demand, visible as `gap:` rows)

- Cloud `audio_source_ids` / `original_audio` and classic `order_required` are still
  declined before render with their typed alternatives (`gap:KRI-725` rows).
- Cloud `moments[]` receipts for KRI-723 v2 envelopes.
- The 22 Oct cross-edit capability/default matrix. This PR covers only the
  adapter/approval-identity part of its inventory.
