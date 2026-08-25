# Creator Agent staged rollout runbook

This runbook is for the dark, independently gated Creator Agent slices: V1
conversation/execution, the Stage 2 exact-generation critic, Stage 3 typed craft,
strict `day_vlog`/`single_hero`, and Stage 5 workspace coordination. It does not
authorize publishing, external asset acquisition, training enrollment, or
inferred preferences. Keep those consent boundaries separate.

## Current implementation boundary

- V1, Stage 2, Stage 3, M6, and Stage 5 are implemented behind backend flags and
  default off.
- Stage 4 is implemented behind `MAIN_CREATOR_AGENT_AUTO_ITERATION_ENABLED=false`.
  The route requires explicit per-session opt-in, objective-review thresholds,
  the exact four-action allowlist, exact target pins, and a durable one-cycle cap.
  The frontend helper only submits an explicit opt-in request; enabling the
  backend flag does not enroll a session.
- Migration `0081_creator_agent_sessions` is the V1 state foundation.
  Migrations `0084_creator_workspace_proposals` and
  `0085_creator_workspace_receipts` add Stage 5 persistence. Migration
  `0086_creator_auto_iteration` adds the Stage 4 opt-in and 0..1 automatic
  revision count. Migration `0087_creator_workspace_proposal_processing` adds
  durable relevance claims and retries. Apply all five before enabling any
  Creator Agent capability.

## Flag matrix and dependencies

| Flag | Default | Dependency / safety rule |
|---|---:|---|
| `MAIN_CREATOR_AGENT_ENABLED` | `false` | Master API gate; routes also require a stable non-zero `MAIN_CREATOR_AGENT_ROLLOUT_PERCENT`. |
| `MAIN_CREATOR_AGENT_ROLLOUT_PERCENT` | `0` | Stable user bucket; use `1` for the first cohort and `0` as the route kill switch. |
| `MAIN_CREATOR_AGENT_EXECUTION_ENABLED` | `false` | Requires master; enables confirmed render execution and Stage 3 craft. |
| `MAIN_CREATOR_AGENT_REVIEW_ENABLED` | `false` | Requires master; exposes review state and creator feedback. |
| `MAIN_CREATOR_AGENT_QUALITY_REVIEW_ENABLED` | `false` | Requires review; queues the exact-generation `video_quality_grader` task. |
| `MAIN_CREATOR_AGENT_AUTO_ITERATION_ENABLED` | `false` | Requires master, execution, review, and quality-review flags. Still requires explicit `opt_in=true` per session; server enforces objective thresholds, allowlist, exact pins, and one cycle. |
| `EDIT_FORMAT_DAY_VLOG_ENABLED` | `false` | Requires representative local-render evidence; strict chronology depends on `NARRATIVE_CLIP_ORDER_ENABLED=true`. |
| `EDIT_FORMAT_SINGLE_HERO_ENABLED` | `false` | Requires representative local-render evidence; worker rejects missing/version-mismatched jobs. |
| `MAIN_CREATOR_AGENT_FREEFORM_UPLOADS_ENABLED` | `false` | Enables only proposal intake/classification; approval is still required. |
| `MAIN_CREATOR_AGENT_WORKSPACE_ENABLED` | `false` | Enables coordination receipts and explicit preference signals; requires `0085`. |
| `USER_STYLE_ENABLED` | `false` | Needed only for an explicit workspace `style_edit`; never infer style from proposals or outcomes. |
| `EDIT_TRANSITIONS_ENABLED` | `false` | Independent Stage 3 transition capability. |
| `EDIT_WIDE_LOOKS_ENABLED` | `false` | Independent Stage 3 look capability. |
| `MEDIA_OVERLAYS_ENABLED` | `false` | Independent owner-scoped overlay capability; browser QA required before enablement. |
| `SOUND_EFFECTS_ENABLED` | `false` | Independent licensed/user SFX capability. |
| `SILENCE_CUT_ENABLED` / `RETAKE_CUT_ENABLED` | `false` / `false` | `apply_speech_cut` advertises only when either detector is enabled; retake remains subordinate to silence-cut. |
| `NEXT_PUBLIC_MAIN_CREATOR_AGENT_ENABLED` | `false` | Frontend exposure only; flip after Fly API + worker capability/execution smoke tests. |

Backend settings are read by API and/or worker processes. After a `fly secrets
set`, restart both process groups (or the named API and worker machines) before
testing. Frontend `NEXT_PUBLIC_*` values require a Vercel build; they do not
make a backend route safe.

## Rollout order

1. **Preflight and migration.** Confirm a clean deploy branch, all Creator flags
   off, and the target code present on both API and worker images. Check the
   migration chain and apply it through the normal release command:

   ```bash
   (cd src/apps/api && alembic heads)
   (cd src/apps/api && alembic current)
   (cd src/apps/api && alembic upgrade head)
   ```

   The expected Creator Agent head is `0087`. Do not downgrade `0084`, `0085`,
   `0086`, or `0087` during a feature rollback; proposals, processing claims,
   receipts, opt-in state, and rollback evidence must remain readable.

2. **Deploy and restart dark.** Deploy API and worker code with every flag above
   at its default. Restart both process groups, then smoke the health endpoint
   and a normal non-Creator render. A rolling worker must be able to read
   `all_candidates` renderer markers; it must fail closed rather than normalize a
   strict M6 job to montage.

3. **Validate the agent offline, then live.** Run the focused contracts first:

   ```bash
   cd src/apps/api
   pytest tests/agents/test_creator_agent_schema.py \
     tests/test_creator_agent_persistence_schema.py \
     tests/routes/test_creator_agent.py \
     tests/services/test_creator_capabilities.py \
     tests/services/test_creator_sessions.py
   ```

   After replay passes, run the live main-creator eval only with the required
   provider credentials and judge approval:

   ```bash
   NOVA_EVAL_MODE=live pytest tests/evals/test_main_creator_evals.py \
     --eval-mode=live --with-judge
   ```

4. **Conversation canary.** Set `MAIN_CREATOR_AGENT_ENABLED=true` and
   `MAIN_CREATOR_AGENT_ROLLOUT_PERCENT=1`, restart API, and verify one internal
   account can start a session, receive a manifest-grounded question/strategy,
   and see no render before explicit **Render this**. Keep the web flag off until
   this passes.

5. **Execution canary.** Enable
   `MAIN_CREATOR_AGENT_EXECUTION_ENABLED=true` for the same internal cohort,
   restart API and workers, and run one guided and one native/voiceover negative
   case. Confirm the Job, selected variant, and `render_generation_id` all match
   the session receipt. A stale manifest or ownership epoch must return 409.

6. **Stage 2 canary.** Enable `MAIN_CREATOR_AGENT_REVIEW_ENABLED=true` and then
   `MAIN_CREATOR_AGENT_QUALITY_REVIEW_ENABLED=true`, restart both process groups,
   and render one exact ready generation. Verify one stable review task, bounded
   evidence, and a confirmation-gated proposed revision. Exercise a stale generation and a
   grader failure; both must leave the video unchanged and show an unavailable or
   failed receipt.

7. **Stage 4 bounded auto-iteration canary.** Keep the frontend affordance
   unavailable and enable `MAIN_CREATOR_AGENT_AUTO_ITERATION_ENABLED=true` only
   for an internal cohort after the Stage 2 canary is green. For one session,
   submit `opt_in=true` with the current revision and client event id. Verify
   the server rejects missing opt-in, confidence below `0.85`, quality `>= 4`,
   expected improvement below `0.5`, a non-`objective_quality` tag, exhausted
   budget, and every action outside the exact allowlist
   (`transition_fallback`, `caption_legibility`, `remove_optional_treatment`,
   `speech_cut`). For an eligible review,
   verify exactly one compiled command, exact target pins, the stable
   `creator-auto:{session_id}:{target_generation_id}` idempotency key, duplicate
   recovery through the craft receipt, and `automatic_revision_count == 1`.
   Confirm the `last_good` receipt names the prior generation and assembly plan;
   force a craft/render failure and confirm fail-open recovery leaves the current
   video unchanged. Do not expand the cohort until this ledger is complete.

8. **Stage 3 treatment canaries.** Enable only the existing treatment flags needed
   for the cohort. Test each command independently, then test a stale generation,
   duplicate idempotency key, owner mismatch, and queue-publication failure. For
   overlays, verify the asset is owned by the exact PlanItem and that a mixed
   overlay/core bundle is rejected. For speech cuts, verify candidate revision
   fencing and rollback. Keep `make verify-overlays` in the evidence ledger for
   any overlay or overlay-renderer change.

9. **M6 canaries.** With API and workers on the same renderer versions, enable one
   format at a time. Before the flag flip, run the local-render matrix below.
   For `day_vlog`, use representative multi-shot footage whose filming guide has
   at least two shots. For `single_hero`, use a dominant hero plus at least one
   cutaway and verify the 60% hero share. Confirm a missing marker, incompatible
   marker, insufficient media, and disabled flag fail visibly; none may produce a
   montage fallback.

10. **Stage 5 intake, then coordination.** Enable
   `MAIN_CREATOR_AGENT_FREEFORM_UPLOADS_ENABLED` only after migration 0084 and
   the proposal browser flow is verified. Test ready `existing_item`, `new_topic`,
   and `unmatched` proposals; every decision must be explicit and hash-fenced.
   Then enable `MAIN_CREATOR_AGENT_WORKSPACE_ENABLED` after 0085 and verify a
   multi-item receipt, exact generation identities, stale ownership, and polling.
   Enable `USER_STYLE_ENABLED` only if testing an explicit `style_edit`; the note
   and style edit must be visibly creator-authored and idempotent.

11. **Frontend exposure and expansion.** Set
    `NEXT_PUBLIC_MAIN_CREATOR_AGENT_ENABLED=true` only after the backend canary
    ledger is green, build the web app, and use the browser QA checklist below.
    Expand the stable rollout percent in small cohorts while watching route 409/404
    rates, review failures, craft enqueue failures, M6 policy failures, workspace
    stale receipts, and ordinary render health.

## Mixed-worker safety

- Deploy code and migrations before flipping any backend flag. Keep all flags off
  while old workers drain.
- M6 API job creation stamps `day_vlog_renderer_version=1` or
  `single_hero_renderer_version=1`; workers reject absent or incompatible values.
  Never repair a mismatch by coercing the job to montage—retry it after the
  compatible worker is live.
- Stage 2 claims and persists only the exact creator/session/PlanItem/Job/variant/
  generation tuple. A newer render must make the old review stale.
- Stage 4 claims the same exact tuple plus manifest/context hashes, session
  revision, ownership epoch, and generation idempotency key. The 0086 columns
  default to opt-out and zero, so old workers ignore the new state safely while
  the migration is applied. A prepared craft receipt is recovered idempotently;
  a successful cycle records the prior generation and assembly plan in the
  `last_good` rollback receipt. Never replay an auto command against a new
  generation or manually attach its receipt to another session.
- Workspace proposals and receipts carry ownership epochs. A plan ownership change
  makes pending work stale; do not manually reattach a receipt to a new item.
- Keep API and worker flags synchronized. A frontend flag is not a substitute for
  backend capability resolution.

## Rollback and kill switches

For an incident, stop new exposure first, then stop mutation, then stop the master:

```bash
fly secrets set \
  MAIN_CREATOR_AGENT_AUTO_ITERATION_ENABLED=false \
  MAIN_CREATOR_AGENT_WORKSPACE_ENABLED=false \
  MAIN_CREATOR_AGENT_FREEFORM_UPLOADS_ENABLED=false \
  MAIN_CREATOR_AGENT_QUALITY_REVIEW_ENABLED=false \
  MAIN_CREATOR_AGENT_REVIEW_ENABLED=false \
  MAIN_CREATOR_AGENT_EXECUTION_ENABLED=false \
  MAIN_CREATOR_AGENT_ROLLOUT_PERCENT=0 \
  MAIN_CREATOR_AGENT_ENABLED=false \
  --app nova-video
fly machine restart <api-machine-id>
fly machine restart <worker-machine-id>
```

The `NEXT_PUBLIC_*` setting belongs in Vercel, not Fly; rebuild the web app to
remove the panel. Format and treatment flags can be turned off independently if
one renderer lane is unhealthy. Turning off a flag prevents new capability use;
persisted sessions, proposals, preferences, and receipts remain readable for
reconciliation. Turning off the Stage 4 flag blocks new auto-iteration requests,
while an already queued craft follows its exact receipt and pin checks. Do not
delete rows or downgrade migrations as an incident response. Re-enable in the
forward order only after the failed canary is understood.

## Mandatory verification ledger

Record command output, cohort/account, UTC timestamp, commit/image, and artifact
paths for every row. A green unit suite is not a substitute for the human rows.

| Check | Required evidence | Status / owner / timestamp |
|---|---|---|
| Migration | `alembic current` and `alembic heads` show `0087`; schema test sees 0084 proposal, 0085 receipt, 0086 opt-in/count state, and 0087 processing claims | |
| Replay evals | Focused Creator Agent/schema/capability/session/workspace tests pass | |
| Live eval | `test_main_creator_evals.py --eval-mode=live --with-judge` passes with approved credentials | |
| Stage 2 exact render | Ready Job/variant/generation receipt; review evidence and stale-target case | |
| Stage 4 bounded auto-iteration | Explicit opt-in; threshold skips; one allowlisted command; exact pins; duplicate recovery; one-cycle cap; prior-generation rollback receipt; fail-open craft/render case | |
| Stage 3 craft | Caption, transition, look, SFX, overlay, and speech-cut receipts; enqueue rollback case | |
| Day-vlog local render | `make local-render MODE=generative EDIT_FORMAT=day_vlog CLIPS="<clip-a> <clip-b>"`; inspect MP4 with `ffprobe` and a human | |
| Single-hero local render | `make local-render MODE=generative EDIT_FORMAT=single_hero CLIPS="<hero> <cutaway>"`; inspect hero dominance and duration | |
| Overlay verification | `make verify-overlays`; read `.overlay-verify/report.json` and inspect `.overlay-verify/montage.png` | |
| Browser QA | Use the `/browse` skill against the canary: session → Render this → review → craft/workspace receipts; verify keyboard/focus, stale/error copy, and no pre-confirm render | |
| Rollback | Flags off + both process groups restarted; old receipts still poll and no new render is minted | |

For every visual output, record whether the input was production-like and whether
the output was inspected on the target browser/device. Do not mark local-render,
overlay, or browser rows complete from tests alone.
