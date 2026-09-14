# Unified creation and editor chat (KRI-22)

The `/plan` conversation is the only creator AI interface. A direct
`/plan/items/:id/edit?variant=...` link resolves its owning conversation through
`POST /creation-threads/for-editor`. Real embedded editor frames keep the existing
route. Old items receive an owner-checked association; a second request reuses it.

## Execution and persistence

The workspace sends typed `kria:editor-chat:v1` commands to its own editor frame.
Both ends check the same origin, window identity and connection. Commands bind to
an editor mount, item, variant, generation and draft revision. The live editor
builds the existing copilot snapshot, including unsaved changes and current
selection. It validates and applies through the same atomic operation applier and
history used by manual editing. A stale or disconnected editor never falls back
to a creation rerender. Retransmission of a command does not repeat execution.

Previewable operations stage a dirty, undoable draft. Save/export remains the
render boundary. Server-only layout/effect changes and reviewed speech changes
require confirmation in the main conversation. Layout/effect endpoints check the
confirmed job and baseline under the job lock. Generated assets retain the
existing cost confirmation, cancellation and insertion checks.

`POST /creation-threads/:id/editor-events` appends bounded browser-acknowledged
conversation messages. It accepts both creation runtimes, validates the owning
item/variant and deduplicates event IDs. It cannot approve, render, or write an
editor draft. History persistence retries never replay an editor command.
Runtime-v2's server draft executor also accepts draft-only tool groups; a render
approval is created only for a group that explicitly requests rendering.

## Runtime-1 uploaded Visuals removal

Legacy `POST /creation-threads/:id/messages` recognizes English removal requests
such as “Remove the visual overlays I added” for the selected variant (or the sole
variant). `creation_editor_actions.py` resolves stable IDs through EditCopilot,
then uses `compile_editor_ops` and the canonical atomic Save validators. The
request directly saves and queues the edit; it does not create a planner proposal.

Only supported, user-origin `visual_blocks` of kind `media`, with no linked text,
are removable. Existing editor capabilities and a base video are required;
lyrics variants and legacy `media_overlays` are excluded. Text, captions, names,
scores, footage, audio and other Visuals retain their saved values. Ambiguous,
unsupported and empty-operation results report that nothing changed. Planner
strategy messages describe proposals, never completed edits.

The user event, desired edit and saved receipt commit together after ownership,
selection, item/job and generation checks. “Saved” does not mean “rendered”. An
enqueue failure reports saved-but-not-rendered only if that exact generation is
still rendering; stale attempts cannot report failure for a newer edit.

Native runtime-1 prompt refresh reads the selected variant's editor authority,
even while another variant supplies playable output. A clean editor accepts the
new baseline; unsaved local changes remain intact and show a conflict. Unchanged
raw authority does not create a false conflict, including after manual Save or
conflict rebase. Refresh failures have their own banner and preserve local edits.

The removal timeline contract is:

- **Duration:** canonical guided revision segments/duration and
  `visual_block_variant_duration` remain unchanged.
- **Ripple:** removing a Visuals layer does not ripple any remaining lane,
  including music; ordering and timing remain intact.
- **Scrubbing:** native canonical duration and scrub bounds remain unchanged.
- **Resizing:** no resize behavior or handle semantics change.
- **Save/history:** server Save establishes a new generation baseline; native
  dirty edits conflict instead of being silently overwritten.
- **Parity:** `test_creation_editor_actions_integration.py` exercises real guided
  Save preparation; `testLegacyVisualRemovalRefreshKeepsSelectedRenderingVariant`
  in `NativeEditorSessionTests.swift` checks native refresh and lane preservation.
  These fixtures mock model/network/database boundaries, not production renders.

## Timeline contract

- **Duration:** the existing virtual timeline and editor apply context own ordered
  entries, overlaps and output duration. The bridge does no timeline arithmetic.
- **Ripple:** existing operation appliers retain stable base-time lanes and their
  current insertion/retiming policies, including continuous music exclusion.
- **Scrubbing:** the editor's existing virtual-preview duration and inverse
  output-to-base mapping continue to clamp all playback and seeking.
- **Resizing:** existing operation validators own handle semantics, limits and
  rounding. Chat does not introduce alternative timing rules.
- **Undo:** one validated operation bundle records one existing editor history
  snapshot. The main-chat undo action is bound to that exact history revision.
- **Parity:** the existing atomic-applier, virtual-timeline and editor-commit
  fixtures remain the timing/render contract. The shared-chat tests additionally
  assert that local operations never dispatch a render before Save/export.

## Verification

Run the focused editor-chat, copilot/director and creation-workspace Jest tests;
the creation editor-conversation API tests; `make verify-kria`; and
`make verify-editor-timeline`. The real-Postgres runtime test covers both a
completed draft-only turn and an explicitly approved render. Browser verification
covers a manual change followed by consecutive chat edits, undo, explicit Save,
responsive Chat/Editor tabs and direct links. Hook tests cover confirmation/decline
and review/suggestion execution.

Deploy the API before the web build. Existing clients can omit the new optional
server-action generation preconditions. Keep existing editor capability flags;
this change does not require enabling runtime v2 or changing render flags.

### Acceptance matrix

| Capability or boundary | Regression evidence |
| --- | --- |
| Text content, style, position; captions; clip timing/order; music; SFX; overlays; transitions; motion; carousel | `src/apps/web/src/__tests__/edit-copilot/apply-ops.test.ts` and the remaining `edit-copilot/` suites exercise the existing atomic applier, unchanged by transport. |
| Questions, clarification, unsupported/no-effect responses, disabled capabilities and invalid mixed bundles | Copilot applier and hook suites reject partial/unsupported mutations. |
| Server layout/effect acknowledgement; reviewed speech; generated assets/cost confirmation | `useEditCopilot.test.tsx`, `useEditDirector.test.tsx`, `EditorShell-render-turn-reply.test.tsx`, bridge confirmation tests and the generation precondition API tests. |
| Manual edit → AI edit → second AI edit → undo → Save | `e2e/editor-chat.spec.ts` uses the real workspace and EditorShell with deterministic HTTP responses; verifies snapshots, no pre-Save render calls, and exactly one pinned Save. |
| Direct links and mobile Chat/Editor tabs | The second `editor-chat.spec.ts` case preserves item, variant and unsaved draft. |
| Duplicate delivery, project/variant/generation/revision changes, iframe reload | `src/__tests__/editor-chat/{bridge,workspace-bridge}.test.tsx`. |
| Receipt deduplication, offline retry, reload recovery and thread isolation | `src/__tests__/editor-chat/conversation.test.tsx` and `tests/routes/test_editor_conversation.py`. |
| Visual suggestion parity | The main chat uses the same matcher hook and undo-recorded acceptance callback. The editor's `poolOnly` presentation retains uploads without AI actions. |

Browser fixtures replace model and render HTTP responses; they do not claim a
paid live-model or production-render evaluation. The original KRI-22 browser transport did not change prompts. Runtime-1 Visuals
removal changes both editor-copilot and main-creator prompts and requires their
versioned live evals before landing, in addition to offline regression gates.
