# Creative Director and Generative Editor

Nova's editor has two distinct AI paths:

- `nova.edit.copilot` uses `EDIT_COPILOT_MODEL` with low thinking and a 20-second
  request timeout for responsive chat-to-operation conversion.
- `nova.edit.director` uses `EDIT_DIRECTOR_MODEL` with high thinking and a
  single 30-second attempt for proactive editorial review. A timeout, rate
  limit, refusal, unavailable model, or schema failure ends that explicit
  review; the endpoint never turns one user action into a second paid request.
  If a newer snapshot supersedes the request, the API returns a conflict
  immediately instead of spending on stale work. `EDIT_DIRECTOR_EVAL_COMPARISON_MODEL`
  is used only by the opt-in live quality comparison and is never a runtime fallback.

The fleet-wide `GEMINI_MODEL` no longer rewrites an agent's declared model.
Agent-run telemetry records requested/effective model, latency,
token usage, prompt version, and outcome. Director feedback separately records
accepted and dismissed suggestion IDs. Identical creator/snapshot/model/prompt
reviews are cached for `EDIT_DIRECTOR_CACHE_TTL_DAYS` (90 by default), and each
creator can start at most `EDIT_DIRECTOR_DAILY_PAID_LIMIT` (three by default)
uncached paid reviews per UTC day.

## Complete text appearance edits (KRI-13)

With `TEXT_APPEARANCE_ENABLED=true` (default false), the API advertises
`text_appearance_version: 1` with the matching parser,
caption persistence, and Creator Block runtime. Negotiated clients send the
complete `text_appearance.targets` inventory, built by
`src/lib/edit-copilot/text-appearance.ts`. Each target carries its stable ID,
category, supported fields, effective values, and an opaque draft fingerprint.
Inventory coverage must fit the negotiated snapshot budget; never truncate it
at the ordinary text-row or operation limit.

`patch_text_appearance` accepts `stroke_width` (integer 0–12) and
`shadow_enabled` (boolean). Its selector has `scope: editable_text`,
`quantifier: all`, and either an optional `category` (`text`, `caption`, `motion`)
or explicit `target_ids`. Resolve semantic subsets from supplied component
metadata; selection is only a disambiguator. Missing support is an actual
limitation, not a reason to ask users to select a caption. Read-only targets
remain in the inventory; an unsupported field is harmless only when its
effective value already matches the request.

The backend binds the operation to every selected target. The browser rebuilds
the inventory and checks membership and fingerprints before staging the entire
bundle. Added, removed, or edited targets reject the request atomically.
Already-compliant targets count as covered. Global caption changes update
metadata and preview bars through the same mapper used by manual controls;
explicit cue subsets persist `stroke_width`/`shadow_enabled` on the cue without
changing defaults. Word-pop highlights restore these overrides after ASS style
resets. Creator Block overrides affect glyph strokes and offset text depth,
while retaining shapes and choreography. Runtime v6 accepts saved v2–v5 hashes;
route-trace v1 retains its legacy path.

The existing editor transaction and receipt lifecycle distinguish proposals,
staged changes, Save outcomes, no-ops, and rejection. A request records one
history entry across text, caption metadata, and motion scenes. Existing traces
record the complete resolved target IDs and attached identities; investigate
unsupported/stale/failed results alongside the staged receipt and Save outcome.
Never treat an AI reply as proof that a render completed.

Rollout: deploy API **and workers/runtime first** with
`TEXT_APPEARANCE_ENABLED=false`, then deploy the web client. After all machines
run the compatible build, enable `TEXT_APPEARANCE_ENABLED=true` on Fly and
verify capability advertisement. Disable the flag to stop new bulk proposals. Old clients keep ordinary operations;
new clients cannot emit the bulk operation against an older API. On rollback,
retain the compatible runtime while any saved appearance overrides exist.

Regression gates: `tests/test_edit_copilot.py`,
`tests/pipeline/test_caption_appearance_scope.py`, the `edit-copilot` Jest suites,
`EditorShell-text-appearance.test.tsx`, and `motion-runtime.test.ts`. Add newly
reported scope failures to the Copilot golden corpus. KRI-13's production URL
had no retrievable Copilot trace; its English/Turkish fixtures are explicitly
synthetic, not a captured production snapshot. The mounted editor regression
covers cross-lane Undo/Redo and the Save payload; deterministic production-image
renders cover actual caption and Creator Block output.

## Suggestion lifecycle

`POST /plan-items/{item_id}/variants/{variant_id}/director/suggestions` accepts
the current unsaved editor snapshot plus a snapshot revision and up to 30
dismissed suggestion IDs. An optional `omni_enabled` capability defaults to
false. The prompt still asks for three to five ranked suggestions across varied
categories, but the API returns every valid non-conflicting card that survives
per-card validation, from one to five, instead of failing the whole review. The
endpoint is authenticated, ownership-checked, editability-checked, size-limited
to the shared negotiated context bound (20 KB on older deployments), rate-limited,
and gated by `EDIT_DIRECTOR_ENABLED`.

Legacy snapshots normally serialize completely. When editor capabilities advertise
`copilot_snapshot_wire_version=1` and trimming alone cannot reach the 18 KB
client budget, the browser sends `wire_compact.version=1` and sparsifies
`timeline`, `motion_catalog`, and `text_bars`. The API expands server-owned
Creator Block defaults, parameters, and controls; treats compact timeline
selectors as summary-backed; and preserves semantic IDs needed for lyric locks
and guided titles. The browser retains the complete in-memory objects and local
fingerprints for stale-target validation. Whole operation families are removed
from `allowed_op_families` before their sections are omitted; core text and clip
families fail closed only as a last resort. A new frontend never emits sparse
rows to an older API that did not advertise v1, so deploy the backend first for
the size fix to take effect.

Text bars may also carry bounded, read-only provenance in negotiated component
context. It contains only primitive source parameters, with arbitrary keys and
values, so Copilot can disambiguate generated elements without a hard-coded
semantic taxonomy. Provenance is descriptive context, never an editable field.

Returned instant cards target mutually compatible edit domains. Director keeps
at most one clip-timeline mutation in a batch because timing, order, removal,
split, and transition edits can stale one another's slot windows. Omni reviews
are homogeneous and contain exactly one asynchronous card; they are never mixed
with instant cards tied to the same source revision.

The editor makes no Director request merely because the editor opened or its
snapshot changed. The creator explicitly selects **Review my edit**. Once that
action starts, the browser tracks the full snapshot hash rather than only the
undo-history revision, including asynchronously hydrated captions, capabilities,
assets, overlays, and effects. Responses are ignored when either the request ID
or snapshot hash is stale. The browser aborts and restarts only that already
requested review against the latest hydrated state, and the API serializes runs
per job so a newer revision does not fan out concurrent Pro calls behind an older
request. **Review again** deliberately starts a new review.

Instant suggestions contain one or more operations from the normal copilot
contract. Acceptance validates and stages the complete bundle in memory first.
If any operation is invalid or stale, no part of the bundle is applied.
Successful acceptance creates one editor-history checkpoint; the user still
uses the existing Undo and Save controls.

After React commits the accepted draft state, the editor pauses playback,
seeks to and selects the first affected text, clip, sound, or overlay, then
scrolls the next actionable recommendation into view. Each acceptance also
leaves an in-session receipt with the exact applied deltas and a replay action.
Receipts carry the editor-history version that created them; Undo, Redo, or any
later edit marks older receipts as changed and disables replay rather than
pretending their original target is still current. If the editor apply callback
fails, Director keeps the recommendation visible and does not record accepted
feedback or create a receipt.

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

## Source-media looks

Copilot and Director can stage `set_look_preset` for a complete clip slot. The
AI contract deliberately exposes only `none` (Original) and
`stadium_diffusion`; Olive Film and Smoky Split-Tone remain human-only choices.
Director recommends Stadium Diffusion conservatively for suitable action,
sports, nightlife, performance, celebration, or atmospheric footage. Chat also
honors an explicit request. A look edit is local, undoable, and included in the
normal Save payload; the existing FFmpeg whole-slot renderer remains
authoritative for both image and video sources. Look changes do not move the
timeline, so they may safely accompany beat- or speech-timed operations.

## Omni generated assets

Omni is a separate optional renderer, never a structured planner. It is gated by
`OMNI_GENERATED_VIDEO_ENABLED=false` and its matching frontend flag. Director
may emit Omni cards only when the server flag and the requesting client's
`omni_enabled` capability are both true, so mixed-version rollouts cannot return
an Omni-only review that the browser must hide.

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

Omni is also restricted to `AI_USAGE_ENVIRONMENT=lab`. Starting an asset requires
the browser to confirm a server-verifiable cost estimate and requires the
operator-owned `AI_OMNI_LAB_TEST_RUN_ID`, `AI_OMNI_LAB_RUN_MAX_COST_USD`, and
`AI_OMNI_LAB_RESERVATION_APPROVED` envelope. A repeated in-flight confirmation
reuses the same request signature instead of starting a second paid generation.

## Rollout

1. Keep all three flags off and run the English/Turkish Director replay judge,
   then the attributed live provider eval. The protected live workflow does not
   run the Anthropic judge because that spend is outside the Google ledger.
2. Enable `EDIT_DIRECTOR_ENABLED` and `NEXT_PUBLIC_EDIT_DIRECTOR_ENABLED` for
   internal/admin traffic. Monitor latency, cache hits, budget refusals, schema
   failures, acceptance, and dismissal.
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

Paid live provider gate. Select `edit_director` in the protected `Agent evals`
workflow, or use the equivalent attributed local command:

```bash
cd src/apps/api
NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development \
pytest tests/evals/test_edit_director_evals.py -v --eval-mode=live \
  --usage-purpose=live_eval --test-run-id=director-YYYYMMDD \
  --max-cost-usd=2 --approve-reservation
```

Run `pytest tests/evals/test_edit_director_evals.py -v --with-judge` separately
in replay mode for the rubric score. The Pro-vs-Flash live comparison test also
requires `--with-judge`; it is not part of the standard protected workflow and
must not be treated as covered by the Google-only $2 cap.

Changes that touch the shared template orchestration or final transition render
path also require a real-video `make local-render` pass. Record its run ID in the
PR body as `Local test: <run_id>` so the Layer-2 release gate can distinguish
render-verified changes from unit-test-only changes.

## Complete component context

When the API advertises `copilot_snapshot_max_bytes=524288`, the editor uses
that negotiated limit with a 2 KiB request reserve and sends
`component_context_version=1`. Copilot and Director share the backend bound.
Older APIs retain the legacy 18 KB client/20 KB server contract described above.

The new contract retains every supplied text, caption, overlay, visual block,
motion block, camera effect, and sound placement, including read-only lanes.
Only `allowed_op_families` grants mutation capability. The browser may compact
the immutable motion catalog or remove historical orientation summaries; it
never silently drops current components to fit the negotiated budget. An
oversized draft reports a context-size error before sending a partial request.

Component metadata carries open-ended semantic roles, source text, creator
notes, analyzed descriptions/OCR, stable asset IDs, and explicit group/source
links. Media visual blocks also expose their placement, transforms, shots,
backgrounds, and sync anchors. Asset storage paths, signed URLs, raw analysis
payloads, and mutation fingerprints do not enter this context. Guided timeline
media descriptions come from the exact approved source generation; adding this
read-only metadata leaves revision hashes, source digests, and render inputs
unchanged. Existing asset analysis supplies visual meaning; absent descriptions
remain unknown rather than being inferred from opaque IDs.

Chat includes the selected component and playhead position. Selection resolves
to the current authoritative index, with caption selections mapped to cue
indices. Explicit user references take precedence. Director excludes selection
and playhead from both its request and revision so navigation does not trigger
new paid reviews. Transient asset-fetch status is also excluded from the revision,
so a background refresh cannot cancel an in-flight generated clip; changed asset
content still invalidates suggestions. Opening Kria also loads asset descriptions independently of
lane write permissions; `asset_context_status` distinguishes loading, ready,
and unavailable descriptions.

The supported operation vocabulary, atomic application, stale-field checks,
source-synced timing constraints, and Save/Undo behavior still apply. Context
visibility does not imply a new renderer capability. Regression coverage spans
arbitrary label families, tail components beyond legacy caps, exact-generation
source context, selection, read-only inspection, and unknown-image clarification.
Live component fixtures assert effective target changes, allowing repeated
unchanged fields but rejecting changes to unrelated components.
