# KRI-459 request preservation rollout

Parent: https://linear.app/kria/issue/KRI-459

This delivery preserves complete request entries and binds new drafts to a
request version and the server's media snapshot. Later chat messages cannot change
an approved request. Source replacement invalidates a pending approval; enriching
analysis for the same source does not. The dispatch transaction checks again so a
replacement between approval and job creation cannot slip through.

The saved binding lives on the existing draft, execution result and Job assembly
record. There is no new context database. A digest detects corrupt saved inputs;
client draft writes cannot replace the server-owned binding. Completed legacy edits
are untouched. Accepted legacy work uses its persisted request; the worker never
reloads a newer brief. Unbound pending work in the enabled cohort needs a new plan.

## Rollout

1. Deploy compatible API and worker readers with KRIA_BRIEF_BINDING_ENABLED=false
   and an empty KRIA_BRIEF_BINDING_USER_IDS. All newly added fields are optional.
2. After every reader is deployed and journey verification passes, separately
   approve a small account allowlist. No flag is changed by this PR.
3. Verify approval, retry, replacement and a later message during rendering.
4. Expand only after real exported-media and physical iPhone checks.

Rollback stops new bindings by clearing the writer flags. Keep these readers: they
must continue to honor already-saved bindings regardless of flag state. Do not roll
back to an older binary that rejects the added draft fields.

## Explicit limits

Main Creator sees up to three whole-entry context batches. Conflicting batch plans
pause. The downstream clip planner accepts a complete request up to 12,000
characters; exceeding its limit preserves the full ledger/chat and asks the creator
which part to work on first. No prefix is silently treated as the complete request.

The connected paths preserve unchecked requirements and ask before a known
requirement-breaking fallback. This does not prove that an exported video fulfills
the request. Model replay, local database tests and simulator checks are reported
separately from paid live-model and physical-device export evidence.

PR #1405 merged as `cf0d99cca` during implementation and is included through
current main. PR #1404 merged as `45d333978`; its alignment code and existing
`NARRATED_CLIP_ALIGNMENT_ENABLED=true` default are inherited from main.
Request-binding writers remain gated separately for the reader-first rollout.
When alignment is unavailable, bound narrated requests ask for recovery rather
than silently assigning clips to equal-duration buckets.

Capabilities continue to come from `services/creator_capabilities.py` and the
existing editor tool registry. Montage supports resolved clip ordering and labels;
Voiceover adds word-timed alignment when enabled; Talking uses its speech/text
operations; Slides uses its slide-specific text/reorder operations. Each operation
is checked against the actual format, renderer and flags, rather than claiming
all four formats support every operation.

## Receipts on editor turns (KRI-529)

A strategy draft (new cut) lists every live requirement, so unjudged ones show as
`partial` / `unchecked` ("still needs an output check"). An editor-operations turn
receipts only the requirements stated in that turn; it no longer re-lists earlier
ones, which put a yellow chip on every unrelated requirement after each chat edit.
iOS draws `verification == "unchecked"` as a neutral "Have a look" chip (KRI-558; "Not
checked yet" before), never the yellow "Partly done" one (this also covers events stored
before the change).

## Checking style asks (KRI-543)

A `style` requirement about existing text can carry `facts.style_intent`, written by the
brief extractor (prompt v5) only for what the creator named:
`{"set": [{"field", "value"}], "target"?: "all_text" | "title" | "labels"}`.

- Fields and values are a closed vocabulary that mirrors `TextElement`: `entrance`
  (none/fade/pop/slide/typewriter), `alignment`, `text_case`, `font_family`, and a literal
  `#RRGGBB` `color`. `brief.normalize_style_intent` validates it; a malformed intent is
  dropped (the requirement stays unchecked), never a schema error. It is not a sticky fact.
- `_check_style` compares it with the saved non-caption text rows (`PlanFacts.text_styles`,
  typed title/label/text by the editor's own `classify`). All eligible rows hold every value
  -> `met`. An explicit `target` with a mismatch -> a judged `partial`. Anything unknown
  (unset font/color, no matching rows, a draft turn) or a mismatch with no `target` ("…to all
  of them") stays unchecked, so "some text field changed" still never counts as met (KRI-524).
- Measure the green rate after deploy: style receipts with `verification == "checked"` versus
  `unchecked` in `scripts/admin.py --prod GET creation-threads/<id>/events`. Invest in target
  resolution only if the anaphoric asks dominate.

## The AI says what it changed (KRI-558)

An editor turn no longer ends in "can't check" / "couldn't verify". `compile_editor_ops`
keeps a before/after diff of the bundle (`services/kria_editor_ops_diff.py`, on
`CompiledEditorDraft.diff`; per lane and field, on effective values, so a legacy
`effect: fade-in` and `animation_phases.entrance == "fade"` are one entrance). Each
requirement of the turn is then bound to it (`kria/editor_receipts.py`):

- A requirement is `met` (`verification: checked`, `stage: applied`) when the diff holds a
  change in the dimension its words/facts name ("font", "smaller", "fade", "align"…), on the
  target it names (title / labels / a text named by its words), in the direction it asks. The
  reason is the change ("Added a fade-in animation to both texts"). Each named dimension is
  its own sub-ask: "Inter font and white" needs both changes.
- A readable value that contradicts the ask (`style_asks.derive_style_ask`: the structured
  intent, the extractor's facts, or the creator's own unambiguous words) still wins and is
  `partial`. A change in the wrong field / text / direction is `partial` or `not_possible`
  and says what changed instead. A requirement that names no dimension may only take the
  entries no other requirement claimed (KRI-524: "some text field changed" never proves it).
- The copilot's `unmet_requests` and `reply_notes` ride on `draft.apply_editor_ops`
  (`unmet_requests`, `notes`) so a declined part is reported as `not_possible`.
- Reply: `Done: <change>.` for one requirement, `- Done / Partly / Couldn't` lines for
  several, then `Also changed: …` for unclaimed changes. An edit that changed nothing falls
  back to the existing "different approach?" question; one that changed something is kept.
- First drafts say nothing about asks only the render can show (no "couldn't verify", no
  "still needs an output check" chips). The render-ready review judges title animation, label
  corner, font/colour and per-clip lengths (`judged_at_render`, `check_text_look`,
  `_check_clip_lengths`) from the finished text lane and timeline; what is still undecided
  is listed under "Have a look at these in the video" with what the video holds.
- Debug: `draft_execution.result["editor_diff"]` holds the diff (first 24 entries) for a turn.

## Editor-turn reply and extraction failures (KRI-534, KRI-536)

- (Superseded by KRI-558 above: an applied edit names its changes.) An editor-operations
  turn used to reply "Updated your edit." plus "I can't check this automatically".
- When requirement extraction fails on a rendered follow-up, the turn is served by the
  edit copilot only if the message is one short text ask (a single sentence with no
  "and"/"also"/comma, no re-plan cue) and the copilot answers with in-place text ops
  alone. The full message is recorded as one unchecked `style`/`global` requirement, so
  the request is preserved. Anything else (compound asks, structural ops, no editor
  target, any error in this path) keeps the recovery reply and leaves the draft as it was.
- Every recovery reply and every degraded turn carries `brief_coverage.cause`
  (`stage`, `error_type`, `cause_type`: class names only, never the error text), readable
  with `python scripts/admin.py [--prod] GET creation-threads/<id>/events`.
  `cause_type: TerminalSchemaError` means the extractor produced invalid output.
