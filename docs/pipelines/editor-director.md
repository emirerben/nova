# Creative Director and Generative Editor

Nova's editor has two distinct AI paths:

- `nova.edit.copilot` uses `EDIT_COPILOT_MODEL` with low thinking and a 20-second
  request timeout for responsive chat-to-operation conversion.
- `nova.edit.director` uses `EDIT_DIRECTOR_MODEL` with high thinking and a
  single 30-second attempt for proactive editorial review. The suggestion
  endpoint falls back to one 20-second `EDIT_DIRECTOR_FALLBACK_MODEL` attempt
  after a timeout, rate limit, unavailable-model error, refusal, or schema
  failure. If a newer snapshot supersedes the primary request, the API returns
  a conflict immediately instead of spending the fallback budget on stale work.

The fleet-wide `GEMINI_MODEL` no longer rewrites an agent's declared model.
Agent-run telemetry records requested/effective model, fallback reason, latency,
token usage, prompt version, and outcome. Director feedback separately records
accepted and dismissed suggestion IDs.

## Suggestion lifecycle

`POST /plan-items/{item_id}/variants/{variant_id}/director/suggestions` accepts
the complete unsaved editor snapshot plus a snapshot revision and up to 30
dismissed suggestion IDs. It returns three to five ranked suggestions. The
endpoint is authenticated, ownership-checked, editability-checked, size-limited
to 20 KB, rate-limited, and gated by `EDIT_DIRECTOR_ENABLED`.

The editor requests its initial review after the complete editor snapshot has
settled, then tracks the full snapshot hash rather than only the undo-history
revision. This includes asynchronously hydrated captions, capabilities, assets,
overlays, and effects. Responses are ignored when either the request ID or the
snapshot hash is stale. The browser aborts and restarts superseded HTTP requests,
and the API serializes Director runs per job so a newer revision does not fan out
concurrent Pro calls behind an older request. An explicit Refresh stays armed
through hydration-driven aborts until a replacement review lands or fails.

Instant suggestions contain one or more operations from the normal copilot
contract. Acceptance validates and stages the complete bundle in memory first.
If any operation is invalid or stale, no part of the bundle is applied.
Successful acceptance creates one editor-history checkpoint; the user still
uses the existing Undo and Save controls.

A returned review remains stable while the user works through its cards, so
accepting one non-overlapping recommendation does not invalidate the rest.
Destructive operations compare a local-only fingerprint of the complete target
entity against the reviewed snapshot before applying. These fingerprints are
not serialized into the API or model prompt. A stale or rejected card triggers
a replacement review instead of mutating a target the user changed meanwhile.

Copilot follow-ups resolve ordinal references against the latest assistant
answer first. For example, “help me with the third one” selects item 3 from the
assistant's numbered diagnosis unless the user explicitly says “text bar 3”,
“clip 3”, “caption 3”, or another current-draft object.

## Effects and transitions

The typed operation contract includes camera-pulse add/patch/remove operations,
visual-block entrance/exit fades, and per-boundary `set_transition`. The
render-safe transition set is deliberately small:

- `cut`
- `crossfade`
- `dip_to_black`
- `flash`

User state stores a transition after its source slot. Assembly translates that
to `transition_in` on the destination slot. Preview layout, beat projection,
playhead duration, and FFmpeg use the same overlap contract: at most 300 ms,
clamped to 30% of each adjacent clip; a non-cut transition is rejected when the
resulting safe duration would be under 100 ms. Backend rendering ignores
persisted transitions unless `EDIT_TRANSITIONS_ENABLED=true`.

## Omni generated assets

Omni is a separate optional renderer, never a structured planner. It is gated by
`OMNI_GENERATED_VIDEO_ENABLED=false` and its matching frontend flag.

Supported actions are:

- generate a 3-10 second 9:16 insert from text and an optional approved reference
  frame;
- restyle one explicitly selected source segment of at most 10 seconds.

Acceptance creates an asynchronous provider interaction using
`EDIT_OMNI_MODEL`. The worker records prompt, source references, provider
interaction ID, model, status, storage path, and normalized duration under
`assembly_plan.omni_generated_assets`. Successful output is normalized through
Nova's H.264/AAC 1080×1920 pipeline, verified, and uploaded. A ready asset stays
unclaimed until the browser confirms its source snapshot is still current and
calls the authenticated `.../omni-assets/{asset_id}/claim` endpoint. Claiming
requires the same bounded draft fingerprint recorded at generation start,
atomically registers the source clip, and exposes one local-draft operation.
Generated inserts use `insert_generated_asset`. Restyles are accepted only when
their source range exactly matches one complete active slot in the submitted
unsaved snapshot and use
`replace_generated_segment`, so the original slot is replaced rather than
duplicated.

Cancellation and failure never change the draft. A cancellation that races with
upload deletes the generated object and does not append it to the source list.
If the user's draft changes while generation or claim is running, the asset is
not inserted; a claim that lost this race is released and its unused storage is
deleted when it is still safe to remove from the candidate pool. Unclaimed
output expires after 24 hours and its storage object is deleted; accepted
provenance remains on the job for debugging.

## Rollout

1. Keep all three flags off and run the English/Turkish Director live eval with
   an independent judge. Pro must average at least 4/5 and beat the Flash
   comparison; Flash must stay structurally valid.
2. Enable `EDIT_DIRECTOR_ENABLED` and `NEXT_PUBLIC_EDIT_DIRECTOR_ENABLED` for
   internal/admin traffic. Monitor latency, fallback rate, schema failures,
   acceptance, and dismissal.
3. Enable `EDIT_TRANSITIONS_ENABLED` and
   `NEXT_PUBLIC_EDIT_TRANSITIONS_ENABLED` after mixed-source render QA.
4. Expand Director suggestions to users after quality and latency gates pass.
5. Run Omni as a separately monitored experiment. Enable the backend first, then
   the frontend flag. Roll back Omni independently without disabling Director or
   the existing copilot.

## Verification

Replay fixtures:

```bash
cd src/apps/api
pytest tests/evals/test_edit_director_evals.py -v
```

Live launch gate (sends the fixtures and prompts to Gemini and the independent
judge):

```bash
cd src/apps/api
NOVA_EVAL_MODE=live pytest tests/evals/test_edit_director_evals.py \
  -v --eval-mode=live --with-judge
```

Changes that touch the shared template orchestration or final transition render
path also require a real-video `make local-render` pass. Record its run ID in the
PR body as `Local test: <run_id>` so the Layer-2 release gate can distinguish
render-verified changes from unit-test-only changes.
